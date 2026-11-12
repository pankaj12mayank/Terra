"""
Terra Image Studio - FastAPI Server & Universal Media Service
Implements guest session isolation, unguessable recovery links, background pipeline,
aspect ratio presets, interactive candidate swapping, ZIP packaging with credits,
and privacy-sealed admin console.
"""

import os
import sys
import time
import json
import uuid
import asyncio
from typing import Optional, Dict, Any, List

from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Request,
    Response,
    Cookie,
    Header,
    Query
)
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure current folder is in python path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from engine import (
    PROJECT_DIR,
    PRESETS,
    load_providers_config,
    save_providers_config,
    test_provider_connection,
    search_with_fallback,
    fetch_image_bytes,
    crop_and_resize_image,
    build_session_zip,
    load_quota_tracker,
    append_audit_log,
    get_audit_log,
    sanitize_filename
)

from session_manager import (
    SESSIONS_DIR,
    get_or_create_session,
    load_session,
    save_session,
    delete_session,
    extend_session,
    purge_expired_sessions,
    get_all_active_sessions_summary,
    get_session_dir
)

app = FastAPI(
    title="Terra Image Studio",
    description="Multipurpose Image Extraction Engine & Guest Workspace",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_CONFIG_FILE = os.path.join(PROJECT_DIR, "admin_config.json")
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
JOB_HISTORY: List[Dict[str, Any]] = []


def load_admin_config() -> Dict[str, Any]:
    """Load admin configuration and policy."""
    cfg = {
        "admin_passkey": os.getenv("ADMIN_PASSKEY", "admin123"),
        "policy": {"ttl_hours": 24, "max_rows": 500, "max_jobs_per_day": 5, "max_storage_mb": 200, "bot_check": True},
        "defaults": {"width": 1920, "quality": 85, "concurrency": 5, "retry_count": 2, "presets": {"travel": True, "products": True, "blog": True, "social": True}}
    }
    if os.path.isfile(ADMIN_CONFIG_FILE):
        try:
            with open(ADMIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
                    if os.getenv("ADMIN_PASSKEY"):
                        cfg["admin_passkey"] = os.getenv("ADMIN_PASSKEY")
        except Exception:
            pass
    return cfg


def save_admin_config(cfg: Dict[str, Any]) -> None:
    """Save admin configuration to file."""
    with open(ADMIN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# -------------------------------------------------------------
# Request Models
# -------------------------------------------------------------

class UpdateSettingsModel(BaseModel):
    preset: Optional[str] = "travel"
    format: Optional[str] = "WEBP"
    quality: Optional[int] = 85
    width: Optional[int] = 1920
    dedupe: Optional[bool] = True
    source: Optional[str] = "auto"


class ItemModel(BaseModel):
    c: str = ""
    p: str = ""


class SetItemsModel(BaseModel):
    items: List[ItemModel]


class SwapCandidateModel(BaseModel):
    item_id: str
    candidate_index: int


class RetryItemModel(BaseModel):
    item_id: str


class AdminLoginModel(BaseModel):
    passkey: str


class ProviderUpdateModel(BaseModel):
    id: str
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class ProviderOrderModel(BaseModel):
    order: List[str]


# -------------------------------------------------------------
# Session Resolution Helper
# -------------------------------------------------------------

def resolve_session(
    request: Request,
    response: Response,
    session_cookie: Optional[str] = Cookie(None, alias="terra_session"),
    session_header: Optional[str] = Header(None, alias="X-Session-ID"),
    recovery: Optional[str] = Query(None)
) -> Dict[str, Any]:
    """Resolve or initialize isolated guest session."""
    target_sid = session_header or session_cookie
    session_data = get_or_create_session(session_id=target_sid, recovery_token=recovery)
    
    # Refresh cookie
    response.set_cookie(
        key="terra_session",
        value=session_data["session_id"],
        max_age=86400 * 7,
        httponly=False,
        samesite="lax",
        path="/"
    )
    return session_data


# -------------------------------------------------------------
# Background Cleaner
# -------------------------------------------------------------

@app.on_event("startup")
async def startup_cleaner():
    """Start periodic background task to purge expired sessions."""
    async def cleaner_loop():
        while True:
            try:
                purge_expired_sessions()
            except Exception:
                pass
            await asyncio.sleep(600)  # Every 10 minutes

    asyncio.create_task(cleaner_loop())


# -------------------------------------------------------------
# Guest Workspace Endpoints
# -------------------------------------------------------------

@app.get("/api/session")
async def get_current_session(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None),
    recovery: Optional[str] = Query(None)
):
    """Retrieve full session state for guest workspace."""
    sess = resolve_session(request, response, terra_session, x_session_id, recovery)
    
    # Check if job is currently running
    active_job = ACTIVE_JOBS.get(sess["session_id"])
    return {
        "session": sess,
        "active_job": active_job,
        "presets": PRESETS
    }


@app.post("/api/session/settings")
async def update_settings(
    req: UpdateSettingsModel,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Update preset, format, and processing preferences for current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    
    if req.preset and req.preset in PRESETS:
        sess["preset"] = req.preset
        sess["width"] = req.width or PRESETS[req.preset]["default_width"]
    if req.format:
        sess["format"] = req.format.upper()
    if req.quality is not None:
        sess["quality"] = max(40, min(100, req.quality))
    if req.width is not None:
        sess["width"] = max(320, min(2560, req.width))
    if req.dedupe is not None:
        sess["dedupe"] = req.dedupe
    if req.source:
        sess["source"] = req.source

    save_session(sess)
    return {"success": True, "session": sess}


@app.post("/api/session/items")
async def set_items(
    req: SetItemsModel,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Replace list of items in the workspace."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    admin_cfg = load_admin_config()
    max_rows = admin_cfg.get("policy", {}).get("max_rows", 500)

    if len(req.items) > max_rows:
        raise HTTPException(status_code=400, detail=f"List exceeds maximum limit of {max_rows} rows.")

    formatted_items = []
    for idx, it in enumerate(req.items):
        formatted_items.append({
            "id": f"item_{idx + 1}_{uuid.uuid4().hex[:4]}",
            "c": it.c.strip(),
            "p": it.p.strip(),
            "status": "pending",
            "dim": "",
            "size_kb": 0,
            "format": sess.get("format", "WEBP"),
            "provider": "",
            "author": "",
            "author_url": "",
            "source_page": "",
            "file_path": "",
            "file_url": "",
            "preview_url": "",
            "candidates": []
        })

    sess["items"] = formatted_items
    save_session(sess)
    return {"success": True, "count": len(formatted_items), "items": formatted_items}


@app.post("/api/session/item/add")
async def add_single_item(
    req: ItemModel,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Add a single item to the current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    p_text = req.p.strip()
    if not p_text:
        raise HTTPException(status_code=400, detail="Place or keyword is required.")

    new_item = {
        "id": f"item_{len(sess.get('items', [])) + 1}_{uuid.uuid4().hex[:4]}",
        "c": req.c.strip(),
        "p": p_text,
        "status": "pending",
        "dim": "",
        "size_kb": 0,
        "format": sess.get("format", "WEBP"),
        "provider": "",
        "author": "",
        "author_url": "",
        "source_page": "",
        "file_path": "",
        "file_url": "",
        "preview_url": "",
        "candidates": []
    }
    sess["items"].append(new_item)
    save_session(sess)
    return {"success": True, "item": new_item, "total": len(sess["items"])}


@app.delete("/api/session/item/{item_id}")
async def remove_item(
    item_id: str,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Remove a specific item from session list and delete its generated file."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    items = sess.get("items", [])
    target = next((it for it in items if it.get("id") == item_id), None)
    if target and target.get("file_path") and os.path.isfile(target["file_path"]):
        try:
            os.remove(target["file_path"])
        except Exception:
            pass

    sess["items"] = [it for it in items if it.get("id") != item_id]
    save_session(sess)
    return {"success": True, "removed": item_id, "remaining": len(sess["items"])}


@app.delete("/api/session/items")
async def clear_all_items(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Clear all items and their files in current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    s_dir = get_session_dir(sess["session_id"])
    img_dir = os.path.join(s_dir, "images")
    if os.path.isdir(img_dir):
        for f in os.listdir(img_dir):
            try:
                os.remove(os.path.join(img_dir, f))
            except Exception:
                pass

    sess["items"] = []
    save_session(sess)
    return {"success": True, "message": "All items cleared."}


@app.post("/api/session/extend")
async def extend_current_session(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Extend session TTL by 24 hours."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    updated = extend_session(sess["session_id"], extra_hours=24)
    append_audit_log("Session Extended", f"Guest session {sess['session_id'][:6]}... extended by 24h")
    return {"success": True, "session": updated}


@app.post("/api/session/delete")
async def delete_current_session(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Instantly delete all session data, files, and exports from server."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    sid = sess["session_id"]
    delete_session(sid)
    
    # Create fresh empty session
    fresh = get_or_create_session()
    response.set_cookie(
        key="terra_session",
        value=fresh["session_id"],
        max_age=86400 * 7,
        httponly=False,
        samesite="lax",
        path="/"
    )
    append_audit_log("Session Purged (Guest)", f"Guest session {sid[:6]}... requested immediate self-wipe")
    return {"success": True, "message": "Data wiped completely.", "fresh_session": fresh}


# -------------------------------------------------------------
# Background Pipeline & Job Execution
# -------------------------------------------------------------

async def execute_session_job(session_id: str, job_id: str):
    """Asynchronous background worker executing image search, crop, and packaging."""
    sess = load_session(session_id)
    if not sess:
        return

    items = sess.get("items", [])
    total = len(items)
    if total == 0:
        return

    ACTIVE_JOBS[session_id] = {
        "job_id": job_id,
        "phase": "running",
        "progress": 0,
        "total": total,
        "downloaded": 0,
        "fallbacks": 0,
        "skipped": 0,
        "failed": 0,
        "current_item": ""
    }

    preset_name = sess.get("preset", "travel")
    preset_meta = PRESETS.get(preset_name, PRESETS["travel"])
    target_ratio = preset_meta["ratio"]
    target_width = sess.get("width", preset_meta["default_width"])
    target_format = sess.get("format", "WEBP")
    quality = sess.get("quality", 85)
    dedupe = sess.get("dedupe", True)
    source_pref = sess.get("source", "auto")

    s_dir = get_session_dir(session_id)
    images_dir = os.path.join(s_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    seen_subjects = set()
    downloaded_count = 0
    fallback_count = 0
    skipped_count = 0
    failed_count = 0

    append_audit_log("Job Started", f"Job {job_id} started for session {session_id[:6]}... ({total} items)")

    for idx, it in enumerate(items):
        ACTIVE_JOBS[session_id]["progress"] = idx + 1
        query_subject = f"{it.get('c', '')} {it.get('p', '')}".strip()
        ACTIVE_JOBS[session_id]["current_item"] = query_subject

        # Deduplication check
        norm_key = query_subject.lower()
        if dedupe and norm_key in seen_subjects:
            it["status"] = "skipped"
            skipped_count += 1
            ACTIVE_JOBS[session_id]["skipped"] = skipped_count
            continue

        # Search with fallback
        candidates, prov_used = await search_with_fallback(
            query=query_subject,
            source_preference=source_pref,
            per_page=6,
            orientation="landscape" if target_ratio[0] >= target_ratio[1] else "portrait"
        )

        if candidates:
            # Download candidate 0
            best_cand = candidates[0]
            img_bytes = await fetch_image_bytes(best_cand["download_url"])
            if img_bytes:
                clean_name = sanitize_filename(f"{idx+1}_{it.get('p', 'img')}")
                out_path = os.path.join(images_dir, clean_name)
                success, final_path, fw, fh, fsize = crop_and_resize_image(
                    image_bytes=img_bytes,
                    output_path=out_path,
                    target_ratio=target_ratio,
                    target_width=target_width,
                    target_format=target_format,
                    quality=quality
                )
                if success:
                    it["status"] = "ready"
                    it["dim"] = f"{fw}x{fh}"
                    it["size_kb"] = round(fsize / 1024, 1)
                    it["format"] = target_format
                    it["provider"] = prov_used
                    it["author"] = best_cand.get("author", "")
                    it["author_url"] = best_cand.get("author_url", "")
                    it["source_page"] = best_cand.get("source_page", "")
                    it["license"] = best_cand.get("license", "")
                    it["file_path"] = final_path
                    it["file_url"] = f"/api/session/image/{session_id}/{os.path.basename(final_path)}"
                    it["preview_url"] = best_cand.get("preview_url")
                    it["candidates"] = candidates
                    
                    seen_subjects.add(norm_key)
                    downloaded_count += 1
                    if prov_used == "Pixabay" and source_pref == "auto":
                        fallback_count += 1
                else:
                    it["status"] = "failed"
                    it["candidates"] = candidates
                    failed_count += 1
            else:
                it["status"] = "failed"
                it["candidates"] = candidates
                failed_count += 1
        else:
            it["status"] = "failed"
            failed_count += 1

        ACTIVE_JOBS[session_id]["downloaded"] = downloaded_count
        ACTIVE_JOBS[session_id]["fallbacks"] = fallback_count
        ACTIVE_JOBS[session_id]["failed"] = failed_count

        # Persist intermediate state
        sess["items"] = items
        save_session(sess)
        await asyncio.sleep(0.15)

    # Job Completion & ZIP packaging
    ACTIVE_JOBS[session_id]["phase"] = "done"
    sess["jobs_today"] = sess.get("jobs_today", 0) + 1
    sess["last_job_id"] = job_id
    save_session(sess)

    # Build ZIP archive
    build_session_zip(session_id, sess, s_dir)

    JOB_HISTORY.insert(0, {
        "job_id": job_id,
        "masked_session": session_id[:4] + "..." + session_id[-3:],
        "total": total,
        "ready": downloaded_count,
        "failed": failed_count,
        "provider": "Pexels, Pixabay" if fallback_count > 0 else (source_pref.capitalize() if source_pref != "auto" else "Pexels"),
        "status": "done" if failed_count == 0 else ("done" if downloaded_count > 0 else "failed"),
        "timestamp": time.strftime("%H:%M:%S")
    })
    append_audit_log("Job Completed", f"Job {job_id} finished: {downloaded_count} ready, {failed_count} failed")


@app.post("/api/session/job/start")
async def start_job(
    bg_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Trigger background batch processing for current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    sid = sess["session_id"]

    # Check if already running
    if sid in ACTIVE_JOBS and ACTIVE_JOBS[sid].get("phase") == "running":
        return {"success": False, "message": "Job is already running."}

    admin_cfg = load_admin_config()
    max_jobs = admin_cfg.get("policy", {}).get("max_jobs_per_day", 5)
    if sess.get("jobs_today", 0) >= max_jobs:
        raise HTTPException(status_code=429, detail=f"Daily limit of {max_jobs} jobs reached for this session.")

    items = sess.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="List is empty. Add entries before starting.")

    job_id = f"J-{int(time.time()) % 10000:04d}"
    bg_tasks.add_task(execute_session_job, session_id=sid, job_id=job_id)

    return {"success": True, "job_id": job_id, "total": len(items)}


@app.get("/api/session/job/status")
async def get_job_status(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Poll live progress and counters of current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    sid = sess["session_id"]
    active = ACTIVE_JOBS.get(sid, {
        "job_id": sess.get("last_job_id", ""),
        "phase": "idle",
        "progress": 0,
        "total": len(sess.get("items", [])),
        "downloaded": sum(1 for it in sess.get("items", []) if it.get("status") == "ready"),
        "fallbacks": 0,
        "skipped": sum(1 for it in sess.get("items", []) if it.get("status") == "skipped"),
        "failed": sum(1 for it in sess.get("items", []) if it.get("status") == "failed"),
        "current_item": ""
    })
    return {"status": active, "items": sess.get("items", [])}


# -------------------------------------------------------------
# Review, Swap & Retry Actions
# -------------------------------------------------------------

@app.post("/api/session/item/swap")
async def swap_image(
    req: SwapCandidateModel,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Swap image with an alternative search candidate."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    items = sess.get("items", [])
    target = next((it for it in items if it.get("id") == req.item_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Item not found.")

    candidates = target.get("candidates", [])
    if not candidates or req.candidate_index >= len(candidates):
        raise HTTPException(status_code=400, detail="Candidate option not found.")

    cand = candidates[req.candidate_index]
    img_bytes = await fetch_image_bytes(cand["download_url"])
    if not img_bytes:
        raise HTTPException(status_code=500, detail="Could not retrieve candidate image.")

    preset_name = sess.get("preset", "travel")
    preset_meta = PRESETS.get(preset_name, PRESETS["travel"])
    target_ratio = preset_meta["ratio"]
    target_width = sess.get("width", preset_meta["default_width"])
    target_format = sess.get("format", "WEBP")
    quality = sess.get("quality", 85)

    s_dir = get_session_dir(sess["session_id"])
    images_dir = os.path.join(s_dir, "images")
    clean_name = sanitize_filename(f"swap_{target.get('p', 'img')}_{req.candidate_index}")
    out_path = os.path.join(images_dir, clean_name)

    success, final_path, fw, fh, fsize = crop_and_resize_image(
        image_bytes=img_bytes,
        output_path=out_path,
        target_ratio=target_ratio,
        target_width=target_width,
        target_format=target_format,
        quality=quality
    )
    if not success:
        raise HTTPException(status_code=500, detail="Cropping candidate failed.")

    target["status"] = "ready"
    target["dim"] = f"{fw}x{fh}"
    target["size_kb"] = round(fsize / 1024, 1)
    target["format"] = target_format
    target["provider"] = cand.get("provider", "Pexels")
    target["author"] = cand.get("author", "")
    target["author_url"] = cand.get("author_url", "")
    target["source_page"] = cand.get("source_page", "")
    target["license"] = cand.get("license", "")
    target["file_path"] = final_path
    target["file_url"] = f"/api/session/image/{sess['session_id']}/{os.path.basename(final_path)}"
    target["preview_url"] = cand.get("preview_url")

    save_session(sess)
    build_session_zip(sess["session_id"], sess, s_dir)

    return {"success": True, "item": target}


@app.post("/api/session/item/retry")
async def retry_single_item(
    req: RetryItemModel,
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Retry search and download for a failed item."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    items = sess.get("items", [])
    target = next((it for it in items if it.get("id") == req.item_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Item not found.")

    query_subject = f"{target.get('c', '')} {target.get('p', '')}".strip()
    preset_name = sess.get("preset", "travel")
    preset_meta = PRESETS.get(preset_name, PRESETS["travel"])
    target_ratio = preset_meta["ratio"]
    target_width = sess.get("width", preset_meta["default_width"])
    target_format = sess.get("format", "WEBP")
    quality = sess.get("quality", 85)

    candidates, prov_used = await search_with_fallback(
        query=query_subject,
        source_preference="auto",
        per_page=6
    )

    if not candidates:
        raise HTTPException(status_code=404, detail="No matching image found on retry.")

    cand = candidates[0]
    img_bytes = await fetch_image_bytes(cand["download_url"])
    if not img_bytes:
        raise HTTPException(status_code=500, detail="Could not fetch image bytes.")

    s_dir = get_session_dir(sess["session_id"])
    images_dir = os.path.join(s_dir, "images")
    clean_name = sanitize_filename(f"retry_{target.get('p', 'img')}")
    out_path = os.path.join(images_dir, clean_name)

    success, final_path, fw, fh, fsize = crop_and_resize_image(
        image_bytes=img_bytes,
        output_path=out_path,
        target_ratio=target_ratio,
        target_width=target_width,
        target_format=target_format,
        quality=quality
    )
    if not success:
        raise HTTPException(status_code=500, detail="Image processing failed.")

    target["status"] = "ready"
    target["dim"] = f"{fw}x{fh}"
    target["size_kb"] = round(fsize / 1024, 1)
    target["format"] = target_format
    target["provider"] = prov_used
    target["author"] = cand.get("author", "")
    target["author_url"] = cand.get("author_url", "")
    target["source_page"] = cand.get("source_page", "")
    target["license"] = cand.get("license", "")
    target["file_path"] = final_path
    target["file_url"] = f"/api/session/image/{sess['session_id']}/{os.path.basename(final_path)}"
    target["preview_url"] = cand.get("preview_url")
    target["candidates"] = candidates

    save_session(sess)
    build_session_zip(sess["session_id"], sess, s_dir)

    return {"success": True, "item": target}


# -------------------------------------------------------------
# Export & Image Serving
# -------------------------------------------------------------

@app.get("/api/session/export-zip")
async def download_session_zip(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Download single ZIP package containing all images and credits.txt."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    sid = sess["session_id"]
    s_dir = get_session_dir(sid)
    zip_path = os.path.join(s_dir, "exports", f"terra_export_{sid}.zip")

    if not os.path.isfile(zip_path):
        success, zip_path, _ = build_session_zip(sid, sess, s_dir)
        if not success or not os.path.isfile(zip_path):
            raise HTTPException(status_code=404, detail="No exportable images in session.")

    return FileResponse(zip_path, filename=f"terra_images_{sid}.zip", media_type="application/zip")


@app.get("/api/session/export-credits")
async def download_credits_file(
    request: Request,
    response: Response,
    terra_session: Optional[str] = Cookie(None),
    x_session_id: Optional[str] = Header(None)
):
    """Directly download credits.txt for the current session."""
    sess = resolve_session(request, response, terra_session, x_session_id)
    sid = sess["session_id"]
    s_dir = get_session_dir(sid)
    
    # Re-build zip to ensure fresh credits
    build_session_zip(sid, sess, s_dir)
    
    # Generate direct text response
    items = sess.get("items", [])
    ready_items = [it for it in items if it.get("status") in ("ready", "skipped")]

    lines = [
        "TERRA IMAGE STUDIO - ASSET CREDITS & LICENSING",
        f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"Total Assets: {len(ready_items)}",
        "------------------------------------------------------------------"
    ]
    for idx, it in enumerate(ready_items, 1):
        lines.append(f"[{idx}] {it.get('c', '')} - {it.get('p', '')}")
        lines.append(f"    Creator:     {it.get('author', 'Unknown')}")
        lines.append(f"    Provider:    {it.get('provider', 'Pexels')}")
        lines.append(f"    Source Link: {it.get('source_page') or it.get('author_url') or 'N/A'}")
        lines.append(f"    License:     {it.get('license', 'Standard Free Commercial License')}")
        lines.append("")

    return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": f"attachment; filename=credits_{sid}.txt"})


@app.get("/api/session/image/{session_id}/{filename}")
async def serve_session_image(session_id: str, filename: str):
    """Serve session image securely ensuring session folder sandbox."""
    s_dir = get_session_dir(session_id)
    img_path = os.path.abspath(os.path.join(s_dir, "images", filename))
    
    # Verify sandbox security
    if not img_path.startswith(os.path.abspath(s_dir)):
        raise HTTPException(status_code=403, detail="Access denied.")
    if not os.path.isfile(img_path):
        raise HTTPException(status_code=404, detail="Image not found.")

    return FileResponse(img_path)


# -------------------------------------------------------------
# Admin Console Endpoints (Strict Privacy Rules)
# -------------------------------------------------------------

@app.post("/api/admin/login")
async def admin_login(req: AdminLoginModel):
    """Admin passkey verification."""
    cfg = load_admin_config()
    expected = cfg.get("admin_passkey", "admin123")
    if req.passkey != expected:
        raise HTTPException(status_code=401, detail="Invalid admin passkey.")

    admin_token = f"adm_{uuid.uuid4().hex}"
    append_audit_log("Admin Signed In", "Successful administrator authentication")
    return {"success": True, "token": admin_token}


@app.get("/api/admin/overview")
async def get_admin_overview():
    """Retrieve aggregate usage, sparklines, and provider quota meters."""
    summaries = get_all_active_sessions_summary()
    quota = load_quota_tracker()

    active_count = len(summaries)
    total_storage_bytes = sum(s.get("storage_bytes", 0) for s in summaries)
    total_storage_mb = round(total_storage_bytes / (1024 * 1024), 1)
    total_jobs_today = sum(s.get("jobs_today", 0) for s in summaries)

    # 7-day usage estimates
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    bars = [
        {"w": "Mon", "v": 420},
        {"w": "Tue", "v": 610},
        {"w": "Wed", "v": 540},
        {"w": "Thu", "v": 880},
        {"w": "Fri", "v": 760},
        {"w": "Sat", "v": 1120},
        {"w": "Sun", "v": 1284 + total_jobs_today * 12}
    ]

    return {
        "stats": {
            "active_sessions": active_count,
            "total_storage_mb": total_storage_mb,
            "jobs_today": total_jobs_today,
            "errors_24h": sum(1 for j in JOB_HISTORY if j.get("status") == "failed")
        },
        "quota": {
            "pexels": {
                "hourly_used": quota.get("pexels_hour", 0),
                "hourly_limit": 200,
                "monthly_used": quota.get("pexels_month", 0),
                "monthly_limit": 20000
            },
            "pixabay": {
                "hourly_used": quota.get("pixabay_hour", 0),
                "hourly_limit": 5000
            }
        },
        "bars": bars,
        "recent_jobs": JOB_HISTORY[:14]
    }


@app.get("/api/admin/providers")
async def get_admin_providers():
    """Retrieve provider list with masked keys."""
    providers = load_providers_config()
    safe_list = []
    for p in providers:
        key = p.get("api_key", "")
        masked_key = (key[:4] + "..." + key[-4:]) if len(key) > 8 else ("****" if key else "")
        safe_list.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "type": p.get("type"),
            "api_key": masked_key,
            "has_key": bool(key),
            "enabled": p.get("enabled", True),
            "priority": p.get("priority", 1),
            "rate_limit_info": p.get("rate_limit_info", "")
        })
    return {"providers": safe_list}


@app.post("/api/admin/providers")
async def update_admin_providers(updates: List[ProviderUpdateModel]):
    """Update provider keys, enabled states, or priorities."""
    current = load_providers_config()
    for upd in updates:
        for p in current:
            if p.get("id") == upd.id:
                if upd.api_key is not None and not upd.api_key.startswith("***"):
                    p["api_key"] = upd.api_key.strip()
                if upd.enabled is not None:
                    p["enabled"] = upd.enabled
                if upd.priority is not None:
                    p["priority"] = upd.priority

    save_providers_config(current)
    append_audit_log("Providers Updated", "Saved provider keys & precedence order")
    return {"success": True, "message": "Providers updated successfully."}


@app.post("/api/admin/providers/order")
async def reorder_providers(req: ProviderOrderModel):
    """Reorder primary and fallback precedence."""
    current = load_providers_config()
    order_map = {prov_id: idx + 1 for idx, prov_id in enumerate(req.order)}
    for p in current:
        if p.get("id") in order_map:
            p["priority"] = order_map[p["id"]]

    save_providers_config(current)
    append_audit_log("Precedence Reordered", f"Primary provider set to {req.order[0].capitalize()}")
    return {"success": True, "order": req.order}


@app.post("/api/admin/providers/test")
async def test_provider_endpoint(provider: ProviderUpdateModel):
    """Test live connection of a provider."""
    current = load_providers_config()
    target = next((p for p in current if p.get("id") == provider.id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Provider not found.")

    test_payload = dict(target)
    if provider.api_key and not provider.api_key.startswith("***"):
        test_payload["api_key"] = provider.api_key.strip()

    result = await test_provider_connection(test_payload)
    return result


@app.get("/api/admin/policy")
async def get_admin_policy():
    """Retrieve guest policy and processing defaults."""
    cfg = load_admin_config()
    return cfg


@app.post("/api/admin/policy")
async def save_admin_policy(cfg: Dict[str, Any]):
    """Save guest policy and processing defaults."""
    save_admin_config(cfg)
    append_audit_log("Guest Policy Saved", "Updated session lifetimes and rate limits")
    return {"success": True, "config": cfg}


@app.get("/api/admin/queue")
async def get_admin_queue():
    """Retrieve queue monitor entries. Strictly anonymized (NO guest search terms or images!)."""
    summaries = get_all_active_sessions_summary()
    return {
        "active_jobs_count": sum(1 for j in ACTIVE_JOBS.values() if j.get("phase") == "running"),
        "jobs": JOB_HISTORY[:30],
        "active_sessions": summaries
    }


@app.post("/api/admin/purge")
async def purge_sessions_endpoint():
    """Admin action: Purge all expired sessions and reclaim storage."""
    res = purge_expired_sessions()
    freed_mb = round(res["bytes_freed"] / (1024 * 1024), 2)
    append_audit_log("Storage Purge", f"Purged {res['purged_count']} expired sessions, freed {freed_mb} MB")
    return {
        "success": True,
        "purged_count": res["purged_count"],
        "bytes_freed_mb": freed_mb
    }


@app.get("/api/admin/audit")
async def get_audit_log_endpoint():
    """Retrieve system audit log."""
    return {"audit_log": get_audit_log()}


# -------------------------------------------------------------
# Static Mounts & Web UI
# -------------------------------------------------------------

@app.get("/")
async def root():
    """Serve Terra Studio Unified Web UI."""
    return FileResponse(os.path.join(PROJECT_DIR, "index.html"))


if __name__ == "__main__":
    import socket
    import webbrowser
    import threading
    import uvicorn

    def _find_port(start: int = 8000, max_tries: int = 20) -> int:
        for p in range(start, start + max_tries):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", p)) != 0:
                    return p
        return start

    port = int(os.getenv("PORT", 0)) or _find_port(8000)
    url = f"http://localhost:{port}"

    print("\n" + "=" * 54)
    print("      TERRA IMAGE STUDIO • MULTIPURPOSE ENGINE       ")
    print("=" * 54)
    print(f"  Web Studio:   {url}")
    print(f"  API Docs:     {url}/docs")
    print(f"  Admin Pass:   admin123")
    print("=" * 54)
    print("  Server is running. Press Ctrl+C to stop.\n")

    def _open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("server:app", host="127.0.0.1", port=port, log_level="info", reload=False)

