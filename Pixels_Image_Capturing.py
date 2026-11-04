import os
import sys
import re
import asyncio
import aiohttp
import pandas as pd
from tqdm import tqdm
from urllib.parse import quote
from PIL import Image
from io import BytesIO

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================
# USER INPUT / ENV RESOLUTION
# ==========================

script_dir = os.path.dirname(os.path.abspath(__file__))

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
if not PEXELS_API_KEY:
    if sys.stdin.isatty():
        PEXELS_API_KEY = input("Enter your Pexels API Key:\n").strip()
    else:
        print("API Key is required. Set PEXELS_API_KEY environment variable or in .env")
        sys.exit(1)

if not PEXELS_API_KEY:
    print("API Key is required.")
    sys.exit(1)

default_csv = os.path.join(script_dir, "countries_places.csv")
env_csv = os.getenv("CSV_FILE")

if env_csv and os.path.isfile(env_csv):
    CSV_FILE = env_csv
elif not sys.stdin.isatty():
    CSV_FILE = default_csv if os.path.isfile(default_csv) else "countries_places.csv"
else:
    while True:
        CSV_FILE = input(
            f"\nEnter full path of your CSV file (Press Enter for '{default_csv}'):\n"
        ).strip().strip('"')

        if not CSV_FILE and os.path.isfile(default_csv):
            CSV_FILE = default_csv
            break
        elif os.path.isfile(CSV_FILE):
            print(f"\nFile found: {CSV_FILE}\n")
            break
        else:
            print("File not found. Please check and try again.\n")

default_folder = os.path.join(script_dir, "country_images")
env_folder = os.getenv("DOWNLOAD_FOLDER")

if env_folder:
    DOWNLOAD_FOLDER = env_folder
elif not sys.stdin.isatty():
    DOWNLOAD_FOLDER = default_folder
else:
    DOWNLOAD_FOLDER = input(
        f"\nEnter download folder path (press Enter for '{default_folder}'):\n"
    ).strip().strip('"')

    if not DOWNLOAD_FOLDER:
        DOWNLOAD_FOLDER = default_folder

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ==========================
# LOAD CSV
# ==========================

try:
    df = pd.read_csv(CSV_FILE)

    if "Country" not in df.columns or "Place" not in df.columns:
        raise Exception("CSV must contain 'Country' and 'Place' headers")

    print(f"CSV loaded successfully. Total rows: {len(df)}\n")

except Exception as e:
    print("Error loading CSV:", e)
    sys.exit(1)

# ==========================
# CONFIG
# ==========================

CONCURRENT_REQUESTS = int(os.getenv("CONCURRENT_REQUESTS", "10"))
RETRY_LIMIT = int(os.getenv("RETRY_LIMIT", "3"))
MAX_WIDTH = int(os.getenv("MAX_WIDTH", "1920"))
WEBP_QUALITY = int(os.getenv("WEBP_QUALITY", "85"))

semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

# ==========================
# FETCH IMAGE URL
# ==========================

async def fetch_image_url(session, query):
    url = f"https://api.pexels.com/v1/search?query={quote(query)}&per_page=1&orientation=landscape"

    headers = {
        "Authorization": PEXELS_API_KEY
    }

    for attempt in range(RETRY_LIMIT):
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
                    return None
                await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)

    return None

# ==========================
# DOWNLOAD + CONVERT TO WEBP
# ==========================

async def download_and_convert(session, image_url, filepath):
    for _ in range(RETRY_LIMIT):
        try:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    image_bytes = await response.read()

                    img = Image.open(BytesIO(image_bytes))

                    # Normalize color modes safely for WebP
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        img = img.convert("RGBA")
                    elif img.mode != "RGB":
                        img = img.convert("RGB")

                    # Resize if wider than MAX_WIDTH
                    if img.width > MAX_WIDTH:
                        ratio = MAX_WIDTH / img.width
                        new_height = max(1, int(img.height * ratio))
                        resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                        img = img.resize((MAX_WIDTH, new_height), resample_filter)

                    # Save as WebP
                    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
                    img.save(filepath, "WEBP", quality=WEBP_QUALITY, method=6)

                    return True
                await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)

    return False

# ==========================
# PROCESS EACH ROW
# ==========================

def sanitize_name(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()
    return cleaned.replace(" ", "_")

async def process_row(session, country, place):
    async with semaphore:
        safe_country = sanitize_name(country)
        safe_place = sanitize_name(place)

        filename = f"{safe_country}_{safe_place}.webp"
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)

        # Skip if already exists
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return "Skipped"

        query = f"{place}, {country}"

        image_url = await fetch_image_url(session, query)

        if image_url:
            success = await download_and_convert(session, image_url, filepath)
            return "Downloaded" if success else "Failed"
        else:
            return "No Image"

# ==========================
# MAIN EXECUTION
# ==========================

async def main():
    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:

        tasks = []
        for _, row in df.iterrows():
            country = str(row["Country"]).strip()
            place = str(row["Place"]).strip()
            tasks.append(process_row(session, country, place))

        results = []
        for f in tqdm(asyncio.as_completed(tasks), total=len(tasks)):
            result = await f
            results.append(result)

        print("\n========== SUMMARY ==========")
        print("Downloaded:", results.count("Downloaded"))
        print("Skipped:", results.count("Skipped"))
        print("No Image:", results.count("No Image"))
        print("Failed:", results.count("Failed"))
        print("=============================")

# ==========================

if __name__ == "__main__":
    asyncio.run(main())
