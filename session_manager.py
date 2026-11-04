"""
Session Manager for Terra Universal Image Studio.
Handles isolated guest sessions, unguessable recovery tokens, TTL expiration,
storage quotas, and automatic cleanup.
"""

import os
import time
import json
import uuid
import shutil
from typing import Dict, Any, Optional, List

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(PROJECT_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

DEFAULT_TTL_HOURS = 24
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_JOBS_PER_DAY = 5
DEFAULT_MAX_STORAGE_MB = 200

# In-memory index of active recovery tokens for fast lookup: token -> session_id
RECOVERY_MAP: Dict[str, str] = {}


def get_session_dir(session_id: str) -> str:
    """Return the filesystem directory for a given session."""
    return os.path.join(SESSIONS_DIR, session_id)


def sanitize_session_id(session_id: str) -> str:
    """Ensure session_id contains only alphanumeric and underscore characters."""
    return "".join(c for c in session_id if c.isalnum() or c == "_")


def compute_dir_size(path: str) -> int:
    """Compute total bytes of files in directory."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session metadata from disk."""
    s_id = sanitize_session_id(session_id)
    s_dir = get_session_dir(s_id)
    meta_path = os.path.join(s_dir, "data.json")

    if not os.path.isfile(meta_path):
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Check if expired
        now = time.time()
        if now > data.get("expires_at", 0):
            # Session expired - delete it
            delete_session(s_id)
            return None

        # Re-index recovery token
        rec_token = data.get("recovery_token")
        if rec_token:
            RECOVERY_MAP[rec_token] = s_id

        # Update storage calculation
        data["storage_bytes"] = compute_dir_size(s_dir)
        return data
    except Exception:
        return None


def save_session(session_data: Dict[str, Any]) -> None:
    """Save session metadata to disk."""
    s_id = sanitize_session_id(session_data["session_id"])
    s_dir = get_session_dir(s_id)
    os.makedirs(os.path.join(s_dir, "images"), exist_ok=True)
    os.makedirs(os.path.join(s_dir, "exports"), exist_ok=True)

    meta_path = os.path.join(s_dir, "data.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)

    if session_data.get("recovery_token"):
        RECOVERY_MAP[session_data["recovery_token"]] = s_id


def create_session(ttl_hours: int = DEFAULT_TTL_HOURS) -> Dict[str, Any]:
    """Create a new isolated guest session."""
    raw_uuid = uuid.uuid4().hex
    session_id = f"s_{raw_uuid[:8]}"
    recovery_token = f"rec_{uuid.uuid4().hex}"

    now = time.time()
    expires_at = now + (ttl_hours * 3600)

    session_data: Dict[str, Any] = {
        "session_id": session_id,
        "recovery_token": recovery_token,
        "created_at": now,
        "expires_at": expires_at,
        "ttl_hours": ttl_hours,
        "jobs_today": 0,
        "storage_bytes": 0,
        "preset": "travel",
        "format": "WEBP",
        "quality": 85,
        "width": 1920,
        "dedupe": True,
        "source": "auto",
        "items": [],
        "last_job_id": None
    }

    save_session(session_data)
    return session_data


def get_or_create_session(
    session_id: Optional[str] = None,
    recovery_token: Optional[str] = None
) -> Dict[str, Any]:
    """Retrieve existing session or create a new one."""
    # 1. Try recovery token
    if recovery_token:
        # Check in memory
        target_sid = RECOVERY_MAP.get(recovery_token)
        if target_sid:
            data = load_session(target_sid)
            if data:
                return data

        # Scan sessions folder if not in memory
        if os.path.isdir(SESSIONS_DIR):
            for entry in os.listdir(SESSIONS_DIR):
                entry_dir = os.path.join(SESSIONS_DIR, entry)
                if os.path.isdir(entry_dir):
                    data = load_session(entry)
                    if data and data.get("recovery_token") == recovery_token:
                        return data

    # 2. Try session_id
    if session_id:
        data = load_session(session_id)
        if data:
            return data

    # 3. Create fresh session
    return create_session()


def delete_session(session_id: str) -> bool:
    """Completely wipe a session and its files from the filesystem immediately."""
    s_id = sanitize_session_id(session_id)
    s_dir = get_session_dir(s_id)

    # Remove from memory map
    for token, sid in list(RECOVERY_MAP.items()):
        if sid == s_id:
            del RECOVERY_MAP[token]

    if os.path.isdir(s_dir):
        try:
            shutil.rmtree(s_dir, ignore_errors=True)
            return True
        except Exception:
            return False
    return False


def extend_session(session_id: str, extra_hours: int = 24) -> Optional[Dict[str, Any]]:
    """Extend session expiration by extra hours."""
    data = load_session(session_id)
    if not data:
        return None

    now = time.time()
    current_exp = max(data.get("expires_at", now), now)
    data["expires_at"] = current_exp + (extra_hours * 3600)
    save_session(data)
    return data


def purge_expired_sessions() -> Dict[str, Any]:
    """Admin function: Scan and purge all expired sessions."""
    if not os.path.isdir(SESSIONS_DIR):
        return {"purged_count": 0, "bytes_freed": 0}

    now = time.time()
    purged_count = 0
    bytes_freed = 0

    for entry in os.listdir(SESSIONS_DIR):
        entry_dir = os.path.join(SESSIONS_DIR, entry)
        if os.path.isdir(entry_dir):
            meta_path = os.path.join(entry_dir, "data.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if now > data.get("expires_at", 0):
                        sz = compute_dir_size(entry_dir)
                        shutil.rmtree(entry_dir, ignore_errors=True)
                        purged_count += 1
                        bytes_freed += sz
                except Exception:
                    pass
            else:
                # Orphan folder
                sz = compute_dir_size(entry_dir)
                shutil.rmtree(entry_dir, ignore_errors=True)
                purged_count += 1
                bytes_freed += sz

    return {"purged_count": purged_count, "bytes_freed": bytes_freed}


def get_all_active_sessions_summary() -> List[Dict[str, Any]]:
    """Admin function: Return anonymized aggregate metrics of all active sessions."""
    summaries: List[Dict[str, Any]] = []
    if not os.path.isdir(SESSIONS_DIR):
        return summaries

    now = time.time()
    for entry in os.listdir(SESSIONS_DIR):
        entry_dir = os.path.join(SESSIONS_DIR, entry)
        if os.path.isdir(entry_dir):
            data = load_session(entry)
            if data and now <= data.get("expires_at", 0):
                # Anonymize: show only masked ID, counts, sizes, status
                sid = data["session_id"]
                masked_id = sid[:4] + "..." + sid[-3:] if len(sid) > 7 else sid
                summaries.append({
                    "masked_session_id": masked_id,
                    "created_at": data.get("created_at"),
                    "expires_at": data.get("expires_at"),
                    "jobs_today": data.get("jobs_today", 0),
                    "items_count": len(data.get("items", [])),
                    "storage_bytes": data.get("storage_bytes", 0),
                    "last_job_id": data.get("last_job_id")
                })

    return summaries

