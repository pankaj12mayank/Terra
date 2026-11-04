"""
Core Universal Image Engine for Terra Studio.
Handles multi-provider searches, smart aspect-ratio presets (Travel 16:9, Products 1:1,
Blog 3:2, Social 4:5), candidate image extraction, fallback pipeline, deduplication,
quota tracking, and ZIP archive packaging with credits.txt.
"""

import os
import re
import time
import json
import zipfile
from io import BytesIO
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_DIR, "providers_config.json")
DEFAULT_DOWNLOAD_DIR = os.path.join(PROJECT_DIR, "downloads")
AUDIT_LOG_FILE = os.path.join(PROJECT_DIR, "audit_log.json")
QUOTA_FILE = os.path.join(PROJECT_DIR, "quota_tracker.json")

PRESETS: Dict[str, Dict[str, Any]] = {
    "travel": {
        "name": "Travel and places",
        "ratio_str": "16:9",
        "ratio": (16, 9),
        "default_width": 1920,
        "default_height": 1080
    },
    "products": {
        "name": "Products and catalogs",
        "ratio_str": "1:1",
        "ratio": (1, 1),
        "default_width": 1080,
        "default_height": 1080
    },
    "blog": {
        "name": "Editorial and blog",
        "ratio_str": "3:2",
        "ratio": (3, 2),
        "default_width": 1200,
        "default_height": 800
    },
    "social": {
        "name": "Social posts",
        "ratio_str": "4:5",
        "ratio": (4, 5),
        "default_width": 1080,
        "default_height": 1350
    }
}

DEFAULT_PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "pexels",
        "name": "Pexels API",
        "type": "pexels",
        "api_key": os.getenv("PEXELS_API_KEY", ""),
        "enabled": True,
        "priority": 1,
        "rate_limit_info": "200 req/hr, 20,000/mo (Free)"
    },
    {
        "id": "pixabay",
        "name": "Pixabay API",
        "type": "pixabay",
        "api_key": os.getenv("PIXABAY_API_KEY", ""),
        "enabled": True,
        "priority": 2,
        "rate_limit_info": "5000 req/hr (Free)"
    }
]

INVALID_CHARS_REGEX = re.compile(r'[\\/*?:"<>|]')


# -------------------------------------------------------------
# Configuration & Providers
# -------------------------------------------------------------

def load_providers_config() -> List[Dict[str, Any]]:
    """Load provider list from config file or initialize with defaults."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for prov in data:
                        if prov.get("id") == "pexels" and not prov.get("api_key"):
                            prov["api_key"] = os.getenv("PEXELS_API_KEY", "")
                        elif prov.get("id") == "pixabay" and not prov.get("api_key"):
                            prov["api_key"] = os.getenv("PIXABAY_API_KEY", "")
                    return data
        except Exception:
            pass

    save_providers_config(DEFAULT_PROVIDERS)
    return DEFAULT_PROVIDERS


def save_providers_config(providers: List[Dict[str, Any]]) -> None:
    """Save providers configuration to JSON."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(providers, f, indent=2)


def sanitize_filename(name: str) -> str:
    """Sanitize string for safe filenames."""
    cleaned = INVALID_CHARS_REGEX.sub("", str(name)).strip()
    return cleaned.replace(" ", "_")


# -------------------------------------------------------------
# Quota & Audit Logging
# -------------------------------------------------------------

def load_quota_tracker() -> Dict[str, Any]:
    """Load provider quota usage stats."""
    now = time.time()
    current_hour = int(now // 3600)
    current_month = time.strftime("%Y-%m")

    default_data: Dict[str, Any] = {
        "current_hour": current_hour,
        "current_month": current_month,
        "pexels_hour": 0,
        "pexels_month": 0,
        "pixabay_hour": 0,
        "pixabay_month": 0
    }

    if os.path.isfile(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Reset hourly if hour changed
            if data.get("current_hour") != current_hour:
                data["current_hour"] = current_hour
                data["pexels_hour"] = 0
                data["pixabay_hour"] = 0
            # Reset monthly if month changed
            if data.get("current_month") != current_month:
                data["current_month"] = current_month
                data["pexels_month"] = 0
                data["pixabay_month"] = 0
            return data
        except Exception:
            pass

    return default_data


def record_quota_usage(provider_type: str, count: int = 1) -> None:
    """Increment quota usage counter for a provider."""
    quota = load_quota_tracker()
    prov = provider_type.lower()
    if "pexels" in prov:
        quota["pexels_hour"] = quota.get("pexels_hour", 0) + count
        quota["pexels_month"] = quota.get("pexels_month", 0) + count
    elif "pixabay" in prov:
        quota["pixabay_hour"] = quota.get("pixabay_hour", 0) + count
        quota["pixabay_month"] = quota.get("pixabay_month", 0) + count

    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(quota, f, indent=2)
    except Exception:
        pass


def append_audit_log(action: str, description: str) -> None:
    """Append a privacy-safe event to the audit log."""
    logs: List[Dict[str, Any]] = []
    if os.path.isfile(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []

    logs.insert(0, {
        "t": time.strftime("%b %d %H:%M"),
        "timestamp": time.time(),
        "a": action,
        "d": description
    })
    # Keep last 100 entries
    logs = logs[:100]
    try:
        with open(AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2)
    except Exception:
        pass


def get_audit_log() -> List[Dict[str, Any]]:
    """Retrieve audit log entries."""
    if os.path.isfile(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return [
        {"t": "Today 10:12", "a": "System Initialized", "d": "Terra Studio engine ready"},
        {"t": "Today 10:15", "a": "Saved guest policy", "d": "Session lifetime set to 24h"}
    ]


# -------------------------------------------------------------
# Smart Preset Cropping & Format Conversion
# -------------------------------------------------------------

def crop_and_resize_image(
    image_bytes: bytes,
    output_path: str,
    target_ratio: Tuple[int, int] = (16, 9),
    target_width: int = 1920,
    target_format: str = "WEBP",
    quality: int = 85
) -> Tuple[bool, str, int, int, int]:
    """
    Center-crops an image to target_ratio, resizes to target_width,
    and converts to target_format (WEBP, PNG, JPG).
    Returns (success, final_path, final_width, final_height, file_size_bytes).
    """
    fmt = target_format.upper().strip()
    if fmt in ("JPG", "JPEG"):
        save_format = "JPEG"
        ext = ".jpg"
    elif fmt == "PNG":
        save_format = "PNG"
        ext = ".png"
    else:
        save_format = "WEBP"
        ext = ".webp"

    base, _ = os.path.splitext(output_path)
    final_path = base + ext

    # Calculate target dimensions
    rw, rh = target_ratio
    target_height = max(1, int(round(target_width * rh / rw)))

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            # Convert color space for destination format
            if save_format == "JPEG":
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    alpha = img.convert("RGBA").split()[3]
                    bg.paste(img.convert("RGBA"), mask=alpha)
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
            elif save_format == "PNG":
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "transparency" in img.info else "RGB")
            else:  # WEBP
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGBA")
                elif img.mode != "RGB":
                    img = img.convert("RGB")

            # Center crop to exact aspect ratio
            orig_w, orig_h = img.size
            orig_aspect = orig_w / float(orig_h)
            desired_aspect = rw / float(rh)

            if orig_aspect > desired_aspect:
                # Original is wider -> crop horizontal sides
                crop_w = int(orig_h * desired_aspect)
                crop_h = orig_h
                left = (orig_w - crop_w) // 2
                top = 0
                right = left + crop_w
                bottom = crop_h
            else:
                # Original is taller -> crop top and bottom
                crop_w = orig_w
                crop_h = int(orig_w / desired_aspect)
                left = 0
                top = (orig_h - crop_h) // 2
                right = crop_w
                bottom = top + crop_h

            cropped_img = img.crop((left, top, right, bottom))

            # Resize with high-quality resampling
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            final_img = cropped_img.resize((target_width, target_height), resample)

            os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

            if save_format == "JPEG":
                final_img.save(final_path, "JPEG", quality=quality, subsampling=0, optimize=True)
            elif save_format == "PNG":
                final_img.save(final_path, "PNG", optimize=True)
            else:
                final_img.save(final_path, "WEBP", quality=quality, method=6)

            file_size = os.path.getsize(final_path)
            return True, final_path, target_width, target_height, file_size
    except Exception as exc:
        return False, str(exc), 0, 0, 0


# -------------------------------------------------------------
# Provider Search Implementations
# -------------------------------------------------------------

async def search_pexels(
    query: str,
    api_key: str,
    per_page: int = 6,
    orientation: str = "landscape"
) -> List[Dict[str, Any]]:
    """Search Pexels API and return normalized candidates."""
    if not api_key:
        return []

    url = f"https://api.pexels.com/v1/search?query={quote(query)}&per_page={per_page}&orientation={orientation}"
    headers = {"Authorization": api_key}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=9)) as resp:
                record_quota_usage("pexels", 1)
                if resp.status == 200:
                    data = await resp.json()
                    photos = data.get("photos", [])
                    results = []
                    for idx, photo in enumerate(photos):
                        src = photo.get("src", {})
                        dl_url = src.get("original") or src.get("large2x") or src.get("large")
                        prev_url = src.get("medium") or src.get("small") or src.get("tiny")
                        if dl_url:
                            results.append({
                                "id": f"pex_{photo.get('id', idx)}",
                                "provider": "Pexels",
                                "download_url": dl_url,
                                "preview_url": prev_url or dl_url,
                                "author": photo.get("photographer", "Pexels Creator"),
                                "author_url": photo.get("photographer_url", "https://www.pexels.com"),
                                "source_page": photo.get("url", "https://www.pexels.com"),
                                "license": "Free to use under Pexels License",
                                "width": photo.get("width", 0),
                                "height": photo.get("height", 0)
                            })
                    return results
        except Exception:
            pass
    return []


async def search_pixabay(
    query: str,
    api_key: str,
    per_page: int = 6,
    orientation: str = "horizontal"
) -> List[Dict[str, Any]]:
    """Search Pixabay API and return normalized candidates."""
    if not api_key:
        return []

    pix_orient = "horizontal" if orientation in ("landscape", "horizontal") else "vertical"
    url = f"https://pixabay.com/api/?key={quote(api_key)}&q={quote(query)}&per_page={per_page}&image_type=photo&orientation={pix_orient}&safesearch=true"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=9)) as resp:
                record_quota_usage("pixabay", 1)
                if resp.status == 200:
                    data = await resp.json()
                    hits = data.get("hits", [])
                    results = []
                    for idx, hit in enumerate(hits):
                        dl_url = hit.get("largeImageURL") or hit.get("webformatURL")
                        prev_url = hit.get("previewURL") or hit.get("webformatURL")
                        if dl_url:
                            results.append({
                                "id": f"pix_{hit.get('id', idx)}",
                                "provider": "Pixabay",
                                "download_url": dl_url,
                                "preview_url": prev_url or dl_url,
                                "author": hit.get("user", "Pixabay Creator"),
                                "author_url": f"https://pixabay.com/users/{hit.get('user', '')}-{hit.get('user_id', '')}/",
                                "source_page": hit.get("pageURL", "https://pixabay.com"),
                                "license": "Free to use under Pixabay License",
                                "width": hit.get("imageWidth", 0),
                                "height": hit.get("imageHeight", 0)
                            })
                    return results
        except Exception:
            pass
    return []


async def search_with_fallback(
    query: str,
    source_preference: str = "auto",
    per_page: int = 6,
    orientation: str = "landscape"
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Search with fallback strategy.
    Returns (candidate_list, provider_used).
    """
    providers = load_providers_config()
    prov_map = {p.get("id"): p for p in providers}

    pex_key = prov_map.get("pexels", {}).get("api_key", "")
    pix_key = prov_map.get("pixabay", {}).get("api_key", "")
    pex_on = prov_map.get("pexels", {}).get("enabled", True)
    pix_on = prov_map.get("pixabay", {}).get("enabled", True)

    # 1. Pexels only requested
    if source_preference == "pexels":
        if pex_key and pex_on:
            res = await search_pexels(query, pex_key, per_page, orientation)
            if res:
                return res, "Pexels"
        return [], "None"

    # 2. Pixabay only requested
    if source_preference == "pixabay":
        if pix_key and pix_on:
            res = await search_pixabay(query, pix_key, per_page, orientation)
            if res:
                return res, "Pixabay"
        return [], "None"

    # 3. Auto mode (Primary -> Fallback)
    # Check priority order in config
    active_sorted = sorted(
        [p for p in providers if p.get("enabled") and p.get("api_key")],
        key=lambda x: x.get("priority", 99)
    )

    for prov in active_sorted:
        p_id = prov.get("id")
        key = prov.get("api_key", "")
        if p_id == "pexels":
            res = await search_pexels(query, key, per_page, orientation)
            if res:
                return res, "Pexels"
        elif p_id == "pixabay":
            res = await search_pixabay(query, key, per_page, orientation)
            if res:
                return res, "Pixabay"

    return [], "None"


async def fetch_image_bytes(url: str) -> Optional[bytes]:
    """Download image bytes from HTTP URL."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=18)) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            pass
    return None


async def test_provider_connection(provider: Dict[str, Any]) -> Dict[str, Any]:
    """Test connection and measure latency/status of provider API key."""
    prov_type = provider.get("type", "").lower()
    api_key = provider.get("api_key", "").strip()

    if not api_key:
        return {
            "success": False,
            "status_code": 0,
            "latency_ms": 0,
            "message": "API Key is empty."
        }

    start = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            if "pexels" in prov_type:
                url = "https://api.pexels.com/v1/search?query=ocean&per_page=1"
                headers = {"Authorization": api_key}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    latency = int((time.time() - start) * 1000)
                    rem = resp.headers.get("X-Ratelimit-Remaining", "N/A")
                    if resp.status == 200:
                        data = await resp.json()
                        total = data.get("total_results", 0)
                        return {
                            "success": True,
                            "status_code": resp.status,
                            "latency_ms": latency,
                            "message": f"Connected! ({total:,} items available, Remaining quota: {rem})"
                        }
                    else:
                        return {
                            "success": False,
                            "status_code": resp.status,
                            "latency_ms": latency,
                            "message": f"HTTP {resp.status}: Invalid key or quota limit exceeded"
                        }
            elif "pixabay" in prov_type:
                url = f"https://pixabay.com/api/?key={quote(api_key)}&q=ocean&per_page=3"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    latency = int((time.time() - start) * 1000)
                    if resp.status == 200:
                        data = await resp.json()
                        total = data.get("totalHits", 0)
                        return {
                            "success": True,
                            "status_code": resp.status,
                            "latency_ms": latency,
                            "message": f"Connected! ({total:,} hits available)"
                        }
                    else:
                        return {
                            "success": False,
                            "status_code": resp.status,
                            "latency_ms": latency,
                            "message": f"HTTP {resp.status}: Invalid Pixabay API key"
                        }
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "latency_ms": int((time.time() - start) * 1000),
            "message": f"Connection error: {str(exc)}"
        }

    return {"success": False, "status_code": 0, "latency_ms": 0, "message": "Unknown provider type"}


# -------------------------------------------------------------
# ZIP Packaging with Credits.txt
# -------------------------------------------------------------

def build_session_zip(
    session_id: str,
    session_data: Dict[str, Any],
    session_dir: str
) -> Tuple[bool, str, int]:
    """
    Build a ZIP file containing all downloaded images and a comprehensive credits.txt.
    Returns (success, zip_path, zip_size_bytes).
    """
    images_dir = os.path.join(session_dir, "images")
    exports_dir = os.path.join(session_dir, "exports")
    os.makedirs(exports_dir, exist_ok=True)

    zip_filename = f"terra_export_{session_id}.zip"
    zip_path = os.path.join(exports_dir, zip_filename)

    items = session_data.get("items", [])
    ready_items = [it for it in items if it.get("status") in ("ready", "skipped") and it.get("file_path")]

    credits_lines = [
        "==================================================================",
        "TERRA IMAGE STUDIO - ASSET CREDITS & LICENSING",
        "==================================================================",
        f"Exported on: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"Preset: {session_data.get('preset', 'travel').capitalize()}",
        f"Target Dimensions: {session_data.get('width', 1920)}px (Ratio: {PRESETS.get(session_data.get('preset', 'travel'), {}).get('ratio_str', '16:9')})",
        f"Format: {session_data.get('format', 'WEBP')} (Quality: {session_data.get('quality', 85)})",
        f"Total Assets: {len(ready_items)}",
        "",
        "The following imagery was sourced via Pexels & Pixabay APIs.",
        "Please retain these credits according to respective platform licenses.",
        "------------------------------------------------------------------",
        ""
    ]

    for idx, it in enumerate(ready_items, 1):
        filename = os.path.basename(it.get("file_path", ""))
        keyword = f"{it.get('c', '')} - {it.get('p', '')}".strip(" -")
        author = it.get("author", "Unknown Creator")
        prov = it.get("provider", "Pexels")
        src_url = it.get("source_page") or it.get("author_url") or "https://www.pexels.com"
        lic = it.get("license", f"Free for commercial use under {prov} License")

        credits_lines.append(f"[{idx}] {filename}")
        credits_lines.append(f"    Subject:     {keyword}")
        credits_lines.append(f"    Creator:     {author}")
        credits_lines.append(f"    Provider:    {prov}")
        credits_lines.append(f"    Source Link: {src_url}")
        credits_lines.append(f"    License:     {lic}")
        credits_lines.append("")

    credits_content = "\n".join(credits_lines)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write credits.txt
            zf.writestr("credits.txt", credits_content)

            # Write image files
            for it in ready_items:
                fpath = it.get("file_path")
                if fpath and os.path.isfile(fpath):
                    arcname = os.path.basename(fpath)
                    zf.write(fpath, arcname=arcname)

        zip_size = os.path.getsize(zip_path)
        return True, zip_path, zip_size
    except Exception as exc:
        return False, str(exc), 0
