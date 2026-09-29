import os
import sys
import json
import sqlite3
import base64
import mimetypes
import asyncio
import inspect
import time
import traceback
import secrets
import threading
import copy
import shutil
import yaml
from collections import Counter
from pathlib import Path
from fastapi import HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from db.sqlite_operator import connect_database, init_database, normalize_source_url
from db.search_manager import search_manager
from utils import (
    get_app_settings, note_path_for,
    asset_path_for, calculate_file_hash, asset_url_for, wd_tag_cache_path_for
)
from runtime_context import RuntimeNotLoadedError, WorkspaceContext, get_runtime_context
from media_lifecycle import discover_owned_paths, storage_lifecycle_lock
from processor import process_file
from logger import log_auth, log_ingest_audit, log_ingest_local, log_review, log_svelte, log_system, log_dirs, startup_log_dirs
from md_generator import MANUAL_FRONTMATTER_FIELDS, load_note_frontmatter, load_note_topics, load_note_wd_tags, generate_markdown, normalize_topic_list
from metadata_index import (
    ensure_metadata_schema,
    item_facet_values,
    item_core_facet_values,
    indexed_item_metadata,
    metadata_facets,
    metadata_index_ready,
    metadata_index_status,
    metadata_repair_running,
    refresh_metadata_index_counters,
    refresh_metadata_facet_counts_for_values,
    safe_reindex_item_metadata,
    start_metadata_repair_worker,
    start_metadata_watchdog,
)
from topics import format_topics_for_note, parse_topic_value, parse_topic_values, rename_topic as rename_topic_file, slugify_topic_label
from tagging import load_tag_cache, tag_media
from thumbnails import ThumbnailBusyError, get_or_generate_thumbnail, thumbnail_path_for, video_thumbnail_path_for
from utils import (
    atomic_write_text, get_cookie_auth_status,
    get_pixiv_refresh_token, utc_now, utc_now_str
)
from ingest_control import local_stop_event, online_stop_event
from artists import (
    add_artist_alias,
    add_artist_link,
    delete_artist_alias,
    delete_artist_link,
    get_artist_detail,
    list_artists,
    merge_artists,
    normalize_artist_name,
    preview_artist_merge,
    resolve_artist_name,
    update_artist,
)
from platforms import list_platforms, resolve_platform_label
from workspace_db import connect_workspace_database, prune_unused_workspace_metadata, rebuild_workspace_metadata, upsert_wd_dictionary_tags
from review_cache import (
    mark_review_cache_dirty,
    remove_review_cache_entry,
    replace_review_cache_entries,
    review_counts,
    upsert_review_cache_entry,
)

class TerminalLogger:
    def __init__(self, filename, original_stream):
        self.terminal = original_stream
        self.filename = filename
        raw_logs_dir, _ = log_dirs()
        self.log_path = raw_logs_dir / filename
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = open(self.log_path, "a", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        with self._lock:
            self._handle.write(message)
            self._handle.flush()

    def flush(self):
        self.terminal.flush()
        with self._lock:
            self._handle.flush()

    def isatty(self):
        return hasattr(self.terminal, 'isatty') and self.terminal.isatty()

    def close(self):
        with self._lock:
            self._handle.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, attr):
        return getattr(self.terminal, attr)

_terminal_logging_configured = False

def configure_terminal_logging():
    global _terminal_logging_configured
    raw_logs_dir, _ = log_dirs()
    target_path = raw_logs_dir / "console.log"
    if (
        _terminal_logging_configured
        and isinstance(sys.stdout, TerminalLogger)
        and isinstance(sys.stderr, TerminalLogger)
        and sys.stdout.log_path == target_path
        and sys.stderr.log_path == target_path
    ):
        return
    original_stdout = sys.stdout.terminal if isinstance(sys.stdout, TerminalLogger) else sys.stdout
    original_stderr = sys.stderr.terminal if isinstance(sys.stderr, TerminalLogger) else sys.stderr
    if isinstance(sys.stdout, TerminalLogger):
        sys.stdout.close()
    if isinstance(sys.stderr, TerminalLogger):
        sys.stderr.close()
    sys.stdout = TerminalLogger("console.log", original_stdout)
    sys.stderr = TerminalLogger("console.log", original_stderr)
    _terminal_logging_configured = True


def restore_terminal_logging():
    global _terminal_logging_configured
    if isinstance(sys.stdout, TerminalLogger):
        original_stdout = sys.stdout.terminal
        sys.stdout.close()
        sys.stdout = original_stdout
    if isinstance(sys.stderr, TerminalLogger):
        original_stderr = sys.stderr.terminal
        sys.stderr.close()
        sys.stderr = original_stderr
    _terminal_logging_configured = False

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "chrome-extension://ccpkdmcgagkelbfmbakapnminicjmmlk",
}

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOG_FILE_NAMES = {
    "system.jsonl": ("structured", "system.jsonl"),
    "svelte.jsonl": ("structured", "svelte.jsonl"),
    "ingest_local.jsonl": ("structured", "ingest_local.jsonl"),
    "ingest_online.jsonl": ("structured", "ingest_online.jsonl"),
    "review.jsonl": ("structured", "review.jsonl"),
    "auth.jsonl": ("structured", "auth.jsonl"),
    "ingestion_audit.jsonl": ("structured", "ingestion_audit.jsonl"),
    "console.log": ("raw", "console.log"),
}


class _DynamicLogFiles(dict):
    def __getitem__(self, filename):
        return _log_file_for(filename)

    def get(self, filename, default=None):
        try:
            return _log_file_for(filename)
        except HTTPException:
            return default


LOG_FILES = _DynamicLogFiles({name: None for name in LOG_FILE_NAMES})

REVIEW_RESOLVED_STATES = {
    "resolved_variant",
    "resolved_delete",
    "resolved_replace",
}
REVIEW_PENDING_STATES = {"pending", "deferred"}
REVIEW_CLEANUP_STATES = {"pending_cleanup", "cleanup_failed"}
REVIEW_VISIBLE_STATES = REVIEW_PENDING_STATES | REVIEW_CLEANUP_STATES
LOCAL_RESULTS_LIMIT = 500
_LOCAL_INGEST_LOCKS: dict[Path, threading.Lock] = {}
_LOCAL_INGEST_STATES: dict[Path, dict] = {}
_LOCAL_INGEST_STATE_LOCK = threading.Lock()


def _runtime_key(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.db_path.resolve()


def _new_local_ingest_state() -> dict:
    return {
        "running": False,
        "phase": "idle",
        "run_id": None,
        "scanned": 0,
        "staged": 0,
        "queued": 0,
        "processed": 0,
        "summary": {"ingested": 0, "review": 0, "failed": 0, "duplicate": 0},
        "results": [],
        "failed_paths": [],
        "last_defaults": {},
        "last_skip_similarity": False,
        "started_at": None,
        "finished_at": None,
        "stop_requested": False,
    }


def local_ingest_state(ctx: WorkspaceContext | None = None) -> dict:
    key = _runtime_key(ctx)
    with _LOCAL_INGEST_STATE_LOCK:
        state = _LOCAL_INGEST_STATES.get(key)
        if state is None:
            state = _new_local_ingest_state()
            _LOCAL_INGEST_STATES[key] = state
        return state


def local_ingest_lock(ctx: WorkspaceContext | None = None) -> threading.Lock:
    key = _runtime_key(ctx)
    with _LOCAL_INGEST_STATE_LOCK:
        lock = _LOCAL_INGEST_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_INGEST_LOCKS[key] = lock
        return lock


def local_ingest_stop_event(ctx: WorkspaceContext | None = None) -> threading.Event:
    return local_stop_event(ctx)


def reset_local_ingest_state(ctx: WorkspaceContext | None = None):
    key = _runtime_key(ctx)
    with _LOCAL_INGEST_STATE_LOCK:
        _LOCAL_INGEST_STATES[key] = _new_local_ingest_state()
    local_ingest_stop_event(ctx).clear()


class _LocalIngestStateProxy:
    def __getitem__(self, key):
        return local_ingest_state()[key]

    def __setitem__(self, key, value):
        local_ingest_state()[key] = value

    def get(self, key, default=None):
        return local_ingest_state().get(key, default)


class _LocalIngestLockProxy:
    def __enter__(self):
        self._lock = local_ingest_lock()
        self._lock.acquire()
        return self._lock

    def __exit__(self, _exc_type, _exc, _tb):
        self._lock.release()


LOCAL_INGEST_STATE = _LocalIngestStateProxy()
LOCAL_INGEST_LOCK = _LocalIngestLockProxy()

def _scan_auth_status_sync(reason: str = "manual") -> dict:
    cookie_status = get_cookie_auth_status()
    pixiv_token = get_pixiv_refresh_token()
    platform_details = cookie_status.get("platform_details") or {}

    def platform_status(platform_id: str, token: str = "not_required", token_source: str = "not_required") -> dict:
        detail = platform_details.get(platform_id) or {}
        return {
            "cookies": detail.get("status") or cookie_status.get(platform_id, "missing"),
            "cookies_path": detail.get("path", ""),
            "cookie_source": detail.get("source", "missing"),
            "token": token,
            "token_source": token_source,
        }

    statuses = {
        "cookies": cookie_status.get("cookies"),
        "platforms": {
            "X": platform_status("x"),
            "Instagram": platform_status("instagram"),
            "Pinterest": platform_status("pinterest"),
            "YouTube": platform_status("youtube"),
            "Pixiv": platform_status("pixiv", pixiv_token["status"], pixiv_token["source"]),
        },
    }

    log_auth(
        "INFO",
        "Auth scan summary",
        reason=reason,
        cookies=statuses["cookies"],
    )
    for platform, status in statuses["platforms"].items():
        log_auth(
            "INFO",
            "Auth platform status",
            reason=reason,
            platform=platform,
            cookies=status["cookies"],
            cookie_source=status.get("cookie_source", "missing"),
            cookies_path=status.get("cookies_path", ""),
            token=status["token"],
            token_source=status["token_source"],
        )
    return statuses


def _api_key_path() -> Path:
    from app_paths import get_app_paths

    return get_app_paths().secrets_dir / ".api_key"

def _api_key() -> str:
    path = _api_key_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    atomic_write_text(path, value)
    return value


def require_runtime_loaded():
    try:
        return get_runtime_context()
    except RuntimeNotLoadedError as exc:
        raise HTTPException(status_code=503, detail="Workspace not loaded") from exc

def _validate_origin(origin: str | None):
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")

def _require_api_key(request: Request):
    provided = request.headers.get("X-LMZ-API-KEY", "")
    expected = _api_key()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")

def _log_dirs_for_source(source: str = "active") -> tuple[Path, Path]:
    if hasattr(source, "default"):
        source = source.default
    clean = str(source or "active").strip().lower()
    if clean == "startup":
        return startup_log_dirs()
    if clean == "vault":
        ctx = get_runtime_context()
        if not ctx.active_vault.root.exists():
            raise HTTPException(status_code=503, detail="Vault logs unavailable")
        return ctx.active_vault.logs_dir / "raw", ctx.active_vault.logs_dir / "structured"
    if clean == "console":
        return log_dirs()
    if clean == "active":
        return log_dirs()
    raise HTTPException(status_code=400, detail="Invalid log source")


def _log_file_for(filename: str, source: str = "active") -> Path:
    if hasattr(filename, "default"):
        filename = filename.default
    if hasattr(source, "default"):
        source = source.default
    source_name = str(source or "active").strip().lower()
    if source_name == "console":
        filename = "console.log"
    spec = LOG_FILE_NAMES.get(filename)
    if not spec:
        raise HTTPException(status_code=400, detail="Invalid log file")
    raw_logs_dir, structured_logs_dir = _log_dirs_for_source(source)
    folder = raw_logs_dir if spec[0] == "raw" else structured_logs_dir
    return folder / spec[1]

def _queue_name(queue_name: str, allow_failed: bool = True) -> str:
    allowed = {"normal", "force", "failed"} if allow_failed else {"normal", "force"}
    if queue_name not in allowed:
        raise HTTPException(status_code=400, detail="Invalid queue")
    return queue_name

def _review_path(filename: str) -> Path:
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid review filename")
    review_root = get_runtime_context().active_vault.review_dir.resolve()
    path = (review_root / filename).resolve()
    if path.parent != review_root:
        raise HTTPException(status_code=400, detail="Invalid review filename")
    return path


def _assets_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.assets_dir


def _review_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.review_dir


def _local_ingest_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.local_ingest_dir


def _topics_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).topics_dir


def _file_response_under(root: Path, relative_path: str):
    candidate = (root / relative_path).resolve()
    root = root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404)
    if not candidate.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(candidate)


def _open_path_external(path: Path):
    if os.name == 'nt':
        os.startfile(str(path))
    else:
        import subprocess
        opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
        subprocess.call([opener, str(path)])

def _review_sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")

def _read_review_sidecar(path: Path) -> dict:
    sidecar_path = _review_sidecar_path(path)
    if not sidecar_path.exists():
        return {}
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_review_sidecar(path: Path, sidecar: dict):
    sidecar_path = _review_sidecar_path(path)
    atomic_write_text(sidecar_path, json.dumps(sidecar, indent=2, ensure_ascii=False))
    upsert_review_cache_entry(path, sidecar)

def _stat_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_size), int(stat.st_mtime_ns)

def _ensure_review_hash(path: Path, sidecar: dict) -> dict:
    size, mtime_ns = _stat_signature(path)
    cached_hash = str(sidecar.get("file_hash") or "").strip()
    cached_size = int(sidecar.get("file_size") or -1)
    cached_mtime = int(sidecar.get("file_mtime") or -1)
    if not cached_hash or cached_size != size or cached_mtime != mtime_ns:
        sidecar["file_hash"] = calculate_file_hash(path)
        sidecar["file_size"] = size
        sidecar["file_mtime"] = mtime_ns
    return sidecar

def _review_display_name(path: Path, sidecar: dict) -> str:
    original_name = str(sidecar.get("original_name") or "").strip()
    if original_name:
        return Path(original_name).name
    metadata = sidecar.get("metadata") if isinstance(sidecar, dict) else {}
    if isinstance(metadata, dict):
        for key in ("original_name", "original_path", "source_path"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return Path(value).name
    return path.name

def _ensure_review_sidecar_defaults(path: Path, sidecar: dict) -> tuple[dict, bool]:
    changed = False
    if not isinstance(sidecar, dict):
        sidecar = {}
        changed = True
    if not sidecar.get("storage_name"):
        sidecar["storage_name"] = path.name
        changed = True
    if not sidecar.get("original_name"):
        sidecar["original_name"] = _review_display_name(path, sidecar)
        changed = True
    if not sidecar.get("review_id"):
        sidecar["review_id"] = f"review_{path.stem}"
        changed = True
    if not sidecar.get("state"):
        sidecar["state"] = "pending"
        changed = True
    if not sidecar.get("source_path"):
        metadata = sidecar.get("metadata") if isinstance(sidecar.get("metadata"), dict) else {}
        source_path = str(metadata.get("original_path") or metadata.get("source_path") or "").strip()
        if source_path:
            sidecar["source_path"] = source_path
            changed = True
    if not sidecar.get("staged_from"):
        metadata = sidecar.get("metadata") if isinstance(sidecar.get("metadata"), dict) else {}
        staged_from = str(metadata.get("staged_from") or "").strip()
        if staged_from:
            sidecar["staged_from"] = staged_from
            changed = True
    return sidecar, changed

def _normalize_review_state(state: str | None) -> str:
    clean = str(state or "").strip()
    if clean == "cleanup_failed":
        return "pending_cleanup"
    if clean in REVIEW_RESOLVED_STATES or clean in REVIEW_PENDING_STATES or clean == "pending_cleanup":
        return clean
    return "pending"

def _review_section_for_state(state: str | None) -> str:
    return "cleanup" if _normalize_review_state(state) == "pending_cleanup" else "pending"

def _is_cleanup_review_state(state: str | None) -> bool:
    return _normalize_review_state(state) == "pending_cleanup"

def _set_review_state(
    sidecar: dict,
    state: str,
    cleanup_error: str | None = None,
    action: str | None = None,
    target_hash: str | None = None,
) -> dict:
    state = _normalize_review_state(state)
    sidecar["state"] = state
    if state in REVIEW_RESOLVED_STATES:
        sidecar["resolved_at"] = utc_now_str()
    if action:
        sidecar["last_action"] = action
    if target_hash:
        sidecar["target_hash"] = target_hash
    if cleanup_error:
        sidecar["last_cleanup_error"] = cleanup_error
    elif "last_cleanup_error" in sidecar:
        sidecar.pop("last_cleanup_error", None)
    return sidecar

def _guess_review_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    ext = Path(filename).suffix.lower()
    fallback_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jfif": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".ogv": "video/ogg",
    }
    return fallback_map.get(ext, "application/octet-stream")

def _review_failure_status(message: str) -> int:
    text = (message or "").strip().lower()
    if text.startswith("duplicate ignored"):
        return 409
    if text.startswith("invalid"):
        return 400
    if text.startswith("system error"):
        return 500
    return 400

def _review_cleanup_path(path: Path, retries: int = 5, delay_seconds: float = 0.2) -> tuple[bool, str]:
    if not path.exists():
        return True, ""
    last_error = ""
    for _ in range(retries):
        try:
            path.unlink()
            if path.suffix.lower() != ".json":
                remove_review_cache_entry(path)
            else:
                mark_review_cache_dirty()
            return True, ""
        except OSError as exc:
            last_error = str(exc)
            time.sleep(delay_seconds)
    return False, last_error

def _review_db_has_hashes(hashes: list[str]) -> set[str]:
    clean = sorted({str(h or "").strip() for h in hashes if str(h or "").strip()})
    if not clean:
        return set()
    conn = connect_database()
    try:
        placeholders = ",".join("?" for _ in clean)
        cursor = conn.cursor()
        cursor.execute(f"SELECT hash FROM items WHERE hash IN ({placeholders})", clean)
        return {row[0] for row in cursor.fetchall() if row and row[0]}
    finally:
        conn.close()

def _manual_frontmatter_for_hash(item_hash: str, ctx: WorkspaceContext | None = None) -> dict:
    conn = connect_database(ctx=ctx)
    try:
        row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
        if not row or not row[0]:
            return {}
        frontmatter = load_note_frontmatter(item_hash, row[0])
    finally:
        conn.close()
    return {
        field: frontmatter[field]
        for field in MANUAL_FRONTMATTER_FIELDS
        if field in frontmatter
    }

def _sqlite_identity_for_hash(item_hash: str, ctx: WorkspaceContext | None = None) -> dict:
    conn = connect_database(ctx=ctx)
    try:
        row = conn.execute(
            "SELECT source_artist, platform, source_url, date_added FROM items WHERE hash = ?",
            (item_hash,),
        ).fetchone()
        if not row:
            return {}
        return {
            "artist": row[0] or "",
            "platform": row[1] or "",
            "source_url": row[2] or "",
            "date_added": row[3] or "",
        }
    finally:
        conn.close()

def _apply_manual_frontmatter_to_item(item_hash: str, manual_fields: dict, identity_fields: dict | None = None, ctx: WorkspaceContext | None = None):
    if not manual_fields and not identity_fields:
        return
    conn = init_database(ctx=ctx)
    workspace_conn = connect_workspace_database(ctx=ctx)
    try:
        identity_fields = identity_fields or {}
        if identity_fields:
            source_url = str(identity_fields.get("source_url") or "")
            conn.execute(
                """
                UPDATE items
                SET source_artist = ?, platform = ?, source_url = ?, source_url_norm = ?, date_added = ?
                WHERE hash = ?
                """,
                (
                    resolve_artist_name(workspace_conn, identity_fields.get("artist") or ""),
                    resolve_platform_label(workspace_conn, identity_fields.get("platform") or ""),
                    source_url,
                    normalize_source_url(source_url),
                    str(identity_fields.get("date_added") or ""),
                    item_hash,
                ),
            )
        md_content = generate_markdown(conn, item_hash, manual_overrides=manual_fields)
        if not md_content:
            raise RuntimeError("replacement note generation returned empty content")
        row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
        if not row or not row[0]:
            raise RuntimeError(f"item {item_hash} is missing storage_id")
        with storage_lifecycle_lock(row[0], ctx=ctx):
            current = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
            if not current or current[0] != row[0]:
                raise RuntimeError(f"item {item_hash} changed during replacement metadata update")
            note_path = note_path_for(item_hash, row[0], ctx=ctx)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(note_path, md_content)
            safe_reindex_item_metadata(conn, item_hash, "review_replace_preserve", update_workspace_wd=False)
            workspace_conn.commit()
            conn.commit()
    except Exception:
        workspace_conn.rollback()
        conn.rollback()
        raise
    finally:
        conn.close()
        workspace_conn.close()

def _item_file_paths(item_hash: str, extension: str, mime_type: str, storage_id: str | None, conn=None, ctx: WorkspaceContext | None = None) -> list[Path]:
    if not storage_id:
        raise RuntimeError(f"item {item_hash} is missing storage_id")
    paths = [
        asset_path_for(item_hash, extension, mime_type, storage_id=storage_id, ctx=ctx),
        note_path_for(item_hash, storage_id=storage_id, ctx=ctx),
        wd_tag_cache_path_for(item_hash, storage_id=storage_id, ctx=ctx),
    ]
    paths.append(thumbnail_path_for(item_hash, storage_id, ctx=ctx))
    paths.append(video_thumbnail_path_for(item_hash, storage_id, ctx=ctx))
    paths.extend(discover_owned_paths(storage_id, ctx=ctx))
    return list(dict.fromkeys(paths))

def _tail_lines(path: Path, count: int = 150) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        block_size = 8192
        data = b""
        position = end
        while position > 0 and data.count(b"\n") <= count:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
        return data.decode("utf-8", errors="replace").splitlines()[-count:]

def _log_file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
        return int(stat.st_dev), int(stat.st_ino), int(stat.st_ctime_ns)
    except OSError:
        return None

# Bu liste, `from api.common import *` sırasında route dosyalarına aktarılacak adları belirler.
# Yıldızlı import bağımlılıkları gizler; mümkün olduğunda açık import daha anlaşılırdır.
__all__ = [name for name in globals() if not name.startswith("__")]

