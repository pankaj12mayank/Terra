"""
Bulk Country & Places Image Downloader
Downloads high-resolution images via Pexels and/or Pixabay APIs,
converts them to optimized WebP format, and handles retries, rate limits,
and duplicate skipping.
"""

import os
import sys
import re
import asyncio
import argparse
from io import BytesIO
from urllib.parse import quote
from typing import Optional, Dict, Any, List

import aiohttp
import pandas as pd
from PIL import Image
from tqdm import tqdm

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_CSV = "countries_places.csv"
DEFAULT_FOLDER = "country_images"
DEFAULT_CONCURRENT = 10
DEFAULT_RETRY = 3
DEFAULT_MAX_WIDTH = 1920
DEFAULT_WEBP_QUALITY = 85

INVALID_FILENAME_CHARS = re.compile(r'[\\/*?:"<>|]')


def sanitize_filename_part(name: str) -> str:
    """Sanitize country or place name for safe filesystem storage."""
    cleaned = INVALID_FILENAME_CHARS.sub("", str(name)).strip()
    return cleaned.replace(" ", "_")


def build_output_filename(country: str, place: str) -> str:
    """Build standardized webp filename for a country and place."""
    safe_country = sanitize_filename_part(country)
    safe_place = sanitize_filename_part(place)
    return f"{safe_country}_{safe_place}.webp"


def convert_and_optimize_image(
    image_bytes: bytes,
    output_path: str,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_WEBP_QUALITY
) -> bool:
    """Convert raw image bytes to optimized WebP with resizing and mode normalization."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            # Handle alpha and color modes safely for WebP
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Resize if wider than max_width maintaining aspect ratio
            if img.width > max_width:
                ratio = max_width / float(img.width)
                new_height = max(1, int(img.height * ratio))
                resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                img = img.resize((max_width, new_height), resample_filter)

            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            img.save(output_path, "WEBP", quality=quality, method=6)
            return True
    except Exception:
        return False


async def fetch_pexels_image_url(
    session: aiohttp.ClientSession,
    query: str,
    api_key: str,
    retry_limit: int = DEFAULT_RETRY
) -> Optional[str]:
    """Query Pexels API for the highest resolution image URL."""
    if not api_key:
        return None

    url = f"https://api.pexels.com/v1/search?query={quote(query)}&per_page=1&orientation=landscape"
    headers = {"Authorization": api_key}

    for attempt in range(retry_limit):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    photos = data.get("photos", [])
                    if photos and "src" in photos[0]:
                        return photos[0]["src"].get("original") or photos[0]["src"].get("large2x")
                elif response.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                elif response.status in (401, 403):
                    # Invalid or unauthorized key - no point retrying
                    return None
                await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)

    return None


async def fetch_pixabay_image_url(
    session: aiohttp.ClientSession,
    query: str,
    api_key: str,
    retry_limit: int = DEFAULT_RETRY
) -> Optional[str]:
    """Query Pixabay API for the highest resolution image URL."""
    if not api_key:
        return None

    url = "https://pixabay.com/api/"
    params = {
        "key": api_key,
        "q": query,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": 3,
        "safesearch": "true"
    }

    for attempt in range(retry_limit):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    hits = data.get("hits", [])
                    if hits:
                        return hits[0].get("largeImageURL") or hits[0].get("fullHDURL") or hits[0].get("webformatURL")
                elif response.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                elif response.status in (400, 401):
                    return None
                await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)

    return None


async def download_image_bytes(
    session: aiohttp.ClientSession,
    image_url: str,
    retry_limit: int = DEFAULT_RETRY
) -> Optional[bytes]:
    """Download raw image bytes with retry logic."""
    for _ in range(retry_limit):
        try:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    return await response.read()
                await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)
    return None


async def process_item(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    country: str,
    place: str,
    download_folder: str,
    source_mode: str,
    pexels_key: Optional[str],
    pixabay_key: Optional[str],
    max_width: int,
    quality: int,
    retry_limit: int,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Process a single country/place pair."""
    async with semaphore:
        filename = build_output_filename(country, place)
        filepath = os.path.join(download_folder, filename)

        # Check existing
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return {"country": country, "place": place, "status": "Skipped", "file": filepath}

        if dry_run:
            return {"country": country, "place": place, "status": "Pending (Dry-Run)", "file": filepath}

        query = f"{place}, {country}"
        image_url = None
        used_source = None

        if source_mode in ("pexels", "auto") and pexels_key:
            image_url = await fetch_pexels_image_url(session, query, pexels_key, retry_limit)
            if image_url:
                used_source = "Pexels"

        if not image_url and source_mode in ("pixabay", "auto") and pixabay_key:
            image_url = await fetch_pixabay_image_url(session, query, pixabay_key, retry_limit)
            if image_url:
                used_source = "Pixabay"

        if not image_url:
            return {"country": country, "place": place, "status": "No Image", "file": filepath}

        image_bytes = await download_image_bytes(session, image_url, retry_limit)
        if not image_bytes:
            return {"country": country, "place": place, "status": "Failed", "file": filepath, "source": used_source}

        success = convert_and_optimize_image(image_bytes, filepath, max_width=max_width, quality=quality)
        if success:
            return {"country": country, "place": place, "status": "Downloaded", "file": filepath, "source": used_source}
        else:
            return {"country": country, "place": place, "status": "Failed", "file": filepath, "source": used_source}


async def run_downloader(
    csv_path: str,
    download_folder: str = DEFAULT_FOLDER,
    source_mode: str = "auto",
    pexels_key: Optional[str] = None,
    pixabay_key: Optional[str] = None,
    concurrency: int = DEFAULT_CONCURRENT,
    retry_limit: int = DEFAULT_RETRY,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_WEBP_QUALITY,
    limit: Optional[int] = None,
    dry_run: bool = False
) -> List[Dict[str, Any]]:
    """Run bulk download for all rows in the CSV."""
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path)
    if "Country" not in df.columns or "Place" not in df.columns:
        raise ValueError("CSV must contain 'Country' and 'Place' header columns.")

    if limit and limit > 0:
        df = df.head(limit)

    os.makedirs(download_folder, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)

    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            process_item(
                session=session,
                semaphore=semaphore,
                country=str(row["Country"]).strip(),
                place=str(row["Place"]).strip(),
                download_folder=download_folder,
                source_mode=source_mode,
                pexels_key=pexels_key,
                pixabay_key=pixabay_key,
                max_width=max_width,
                quality=quality,
                retry_limit=retry_limit,
                dry_run=dry_run
            )
            for _, row in df.iterrows()
        ]

        results = []
        for future in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Processing Places"):
            result = await future
            results.append(result)

    return results


def print_summary(results: List[Dict[str, Any]]) -> None:
    """Print tabular execution summary."""
    statuses = [r["status"] for r in results]
    print("\n" + "=" * 32)
    print("        EXECUTION SUMMARY       ")
    print("=" * 32)
    print(f"Total Rows:     {len(results)}")
    print(f"Downloaded:     {statuses.count('Downloaded')}")
    print(f"Skipped:        {statuses.count('Skipped')}")
    print(f"No Image Found: {statuses.count('No Image')}")
    print(f"Failed:         {statuses.count('Failed')}")
    if statuses.count("Pending (Dry-Run)") > 0:
        print(f"Pending (Dry):  {statuses.count('Pending (Dry-Run)')}")
    print("=" * 32 + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk download country/place images via Pexels and Pixabay APIs into WebP format."
    )
    parser.add_argument("--csv", default=None, help=f"Path to CSV file (default: {DEFAULT_CSV})")
    parser.add_argument("--output", default=None, help=f"Download directory (default: {DEFAULT_FOLDER})")
    parser.add_argument(
        "--source",
        choices=["auto", "pexels", "pixabay"],
        default="auto",
        help="API source to use: auto (Pexels with Pixabay fallback), pexels, or pixabay (default: auto)"
    )
    parser.add_argument("--pexels-key", default=None, help="Pexels API Key (or set PEXELS_API_KEY env var)")
    parser.add_argument("--pixabay-key", default=None, help="Pixabay API Key (or set PIXABAY_API_KEY env var)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENT, help=f"Concurrent requests (default: {DEFAULT_CONCURRENT})")
    parser.add_argument("--retry", type=int, default=DEFAULT_RETRY, help=f"Retry attempts (default: {DEFAULT_RETRY})")
    parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH, help=f"Max image width in px (default: {DEFAULT_MAX_WIDTH})")
    parser.add_argument("--quality", type=int, default=DEFAULT_WEBP_QUALITY, help=f"WebP quality 1-100 (default: {DEFAULT_WEBP_QUALITY})")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows to process")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without making external API downloads")
    parser.add_argument("--interactive", action="store_true", help="Force interactive prompt mode")
    return parser.parse_args()


def resolve_configuration(args: argparse.Namespace) -> Dict[str, Any]:
    """Resolve CLI args, environment variables, or interactive prompts."""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # CSV path resolution
    csv_file = args.csv or os.getenv("CSV_FILE")
    if csv_file:
        if not os.path.isabs(csv_file):
            script_candidate = os.path.join(script_dir, csv_file)
            if os.path.isfile(script_candidate):
                csv_file = script_candidate
    else:
        candidate = os.path.join(script_dir, DEFAULT_CSV)
        if os.path.isfile(candidate):
            csv_file = candidate
        elif os.path.isfile(DEFAULT_CSV):
            csv_file = DEFAULT_CSV

    # Output folder resolution
    download_folder = args.output or os.getenv("DOWNLOAD_FOLDER")
    if download_folder:
        if not os.path.isabs(download_folder):
            script_candidate = os.path.join(script_dir, download_folder)
            download_folder = script_candidate
    else:
        download_folder = os.path.join(script_dir, DEFAULT_FOLDER)

    # API Keys resolution
    pexels_key = args.pexels_key or os.getenv("PEXELS_API_KEY")
    pixabay_key = args.pixabay_key or os.getenv("PIXABAY_API_KEY")

    # Interactive prompt if needed and requested or if no keys provided at all and not in dry-run
    is_interactive = args.interactive or (not dry_run_check(args) and not pexels_key and not pixabay_key and sys.stdin.isatty())

    if is_interactive:
        print("\n--- Image Downloader Setup ---")
        if not pexels_key:
            pkey = input("Enter Pexels API Key (press Enter to skip): ").strip()
            if pkey:
                pexels_key = pkey
        if not pixabay_key:
            pxkey = input("Enter Pixabay API Key (press Enter to skip): ").strip()
            if pxkey:
                pixabay_key = pxkey

        if not csv_file or not os.path.isfile(csv_file):
            while True:
                path_in = input(f"Enter CSV file path [Default: {DEFAULT_CSV}]: ").strip().strip('"')
                if not path_in:
                    path_in = os.path.join(script_dir, DEFAULT_CSV)
                if os.path.isfile(path_in):
                    csv_file = path_in
                    break
                print(f"File not found: {path_in}. Please try again.")

        folder_in = input(f"Enter download folder [Default: {download_folder}]: ").strip().strip('"')
        if folder_in:
            download_folder = folder_in

    return {
        "csv_path": csv_file,
        "download_folder": download_folder,
        "source_mode": args.source,
        "pexels_key": pexels_key,
        "pixabay_key": pixabay_key,
        "concurrency": args.concurrency,
        "retry_limit": args.retry,
        "max_width": args.max_width,
        "quality": args.quality,
        "limit": args.limit,
        "dry_run": args.dry_run
    }


def dry_run_check(args: argparse.Namespace) -> bool:
    return bool(args.dry_run)


def main() -> None:
    args = parse_args()
    config = resolve_configuration(args)

    if not config["dry_run"] and not config["pexels_key"] and not config["pixabay_key"]:
        print(
            "ERROR: At least one API Key (Pexels or Pixabay) is required.\n"
            "Provide keys via --pexels-key / --pixabay-key CLI flags, or create a .env file.\n"
            "See .env.example for details."
        )
        sys.exit(1)

    if not config["csv_path"] or not os.path.isfile(config["csv_path"]):
        print(f"ERROR: CSV file not found at: {config['csv_path']}")
        sys.exit(1)

    print(f"Using CSV: {config['csv_path']}")
    print(f"Output Directory: {config['download_folder']}")
    print(f"Source Mode: {config['source_mode']}")
    print(f"Pexels Key Configured: {'Yes' if config['pexels_key'] else 'No'}")
    print(f"Pixabay Key Configured: {'Yes' if config['pixabay_key'] else 'No'}")
    if config["dry_run"]:
        print("Mode: DRY-RUN (no files will be downloaded)")
    print()

    results = asyncio.run(run_downloader(
        csv_path=config["csv_path"],
        download_folder=config["download_folder"],
        source_mode=config["source_mode"],
        pexels_key=config["pexels_key"],
        pixabay_key=config["pixabay_key"],
        concurrency=config["concurrency"],
        retry_limit=config["retry_limit"],
        max_width=config["max_width"],
        quality=config["quality"],
        limit=config["limit"],
        dry_run=config["dry_run"]
    ))

    print_summary(results)


if __name__ == "__main__":
    main()

