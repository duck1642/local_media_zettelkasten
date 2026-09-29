import asyncio
import json
import mimetypes
import re
import secrets
import shutil
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from logger import log_ingest_local
from processor import process_file
from pydantic import BaseModel
from runtime_context import WorkspaceContext, get_runtime_context
from utils import atomic_write_text, calculate_file_hash, get_app_settings, utc_now, utc_now_str
from validators import get_mime_type, is_allowed_mime

from api.common import _require_api_key
from api.guards import require_usable_vault_context

router = APIRouter(dependencies=[Depends(require_usable_vault_context)])

STAGED_ID_RE = re.compile(r"^staged_\d{8}_\d{6}_[0-9a-f]{8}$")
CAPTURE_STAGE_DIR_NAME = "capture_staging"
CAPTURE_HANDLED_DIR_NAME = ".handled"

_capture_commit_locks: dict[str, threading.Lock] = {}
_capture_commit_locks_guard = threading.Lock()


class CaptureCommitRequest(BaseModel):
    staged_id: str
    artist: str | None = None
    platform: str | None = None
    skip_similarity: bool = False


def _capture_stage_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.root / CAPTURE_STAGE_DIR_NAME


def _capture_handled_dir(ctx: WorkspaceContext | None = None) -> Path:
    return _capture_stage_dir(ctx) / CAPTURE_HANDLED_DIR_NAME


def _capture_handled_path(staged_id: str, ctx: WorkspaceContext | None = None) -> Path:
    staged_id = _validate_staged_id(staged_id)
    return _capture_handled_dir(ctx) / f"{staged_id}.json"


def _capture_commit_lock(staged_id: str) -> threading.Lock:
    staged_id = _validate_staged_id(staged_id)
    with _capture_commit_locks_guard:
        lock = _capture_commit_locks.get(staged_id)
        if lock is None:
            lock = threading.Lock()
            _capture_commit_locks[staged_id] = lock
        return lock


def _validate_staged_id(staged_id: str) -> str:
    value = str(staged_id or "").strip()
    if not STAGED_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid staged id")
    return value


def _platform_guess(source_url: str) -> str:
    value = str(source_url or "").casefold()
    if "pixiv.net" in value:
        return "Pixiv"
    if "twitter.com" in value or "x.com" in value:
        return "X"
    if "instagram.com" in value:
        return "Instagram"
    if "pinterest." in value or "pin.it" in value:
        return "Pinterest"
    return "General Web"


def _clean_original_name(filename: str | None, staged_id: str, content_type: str | None) -> str:
    raw = str(filename or "").strip()
    name = Path(raw).name if raw else ""
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if ch in invalid or ord(ch) < 32 else ch for ch in name).strip(" .")
    if not safe or safe == "blob":
        ext = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip()) or ".bin"
        if ext == ".jpe":
            ext = ".jpg"
        safe = f"{staged_id}{ext}"
    if len(safe) > 180:
        suffix = Path(safe).suffix[:20]
        safe = f"{Path(safe).stem[:140]}{suffix}"
    return safe


def _stage_paths(staged_id: str, original_name: str, ctx: WorkspaceContext | None = None) -> tuple[Path, Path]:
    stage_dir = _capture_stage_dir(ctx)
    suffix = Path(original_name).suffix.lower() or ".bin"
    return stage_dir / f"{staged_id}{suffix}", stage_dir / f"{staged_id}.json"


def _load_sidecar(staged_id: str, ctx: WorkspaceContext | None = None) -> dict:
    staged_id = _validate_staged_id(staged_id)
    sidecar_path = _capture_stage_dir(ctx) / f"{staged_id}.json"
    if not sidecar_path.exists():
        raise HTTPException(status_code=404, detail="Capture not found")
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Capture sidecar is unreadable")
    if payload.get("staged_id") != staged_id:
        raise HTTPException(status_code=500, detail="Capture sidecar mismatch")
    return payload


def _load_handled_capture(staged_id: str, ctx: WorkspaceContext | None = None) -> dict | None:
    path = _capture_handled_path(staged_id, ctx)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("staged_id") != staged_id:
        return None
    payload["already_handled"] = True
    return payload


def _write_handled_capture(staged_id: str, payload: dict, ctx: WorkspaceContext | None = None):
    path = _capture_handled_path(staged_id, ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = dict(payload)
    marker["staged_id"] = staged_id
    marker["handled_at"] = utc_now_str()
    atomic_write_text(path, json.dumps(marker, indent=2, sort_keys=True))


def _staged_file_from_sidecar(sidecar: dict, ctx: WorkspaceContext | None = None) -> Path:
    stage_dir = _capture_stage_dir(ctx).resolve()
    filename = str(sidecar.get("stored_name") or "")
    if Path(filename).name != filename:
        raise HTTPException(status_code=500, detail="Invalid staged filename")
    path = (stage_dir / filename).resolve()
    if path.parent != stage_dir:
        raise HTTPException(status_code=500, detail="Invalid staged path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Staged file not found")
    return path


def _cleanup_stage(staged_id: str, ctx: WorkspaceContext | None = None):
    staged_id = _validate_staged_id(staged_id)
    stage_dir = _capture_stage_dir(ctx)
    for path in stage_dir.glob(f"{staged_id}.*"):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            log_ingest_local("WARNING", "Capture staging cleanup failed", staged_id=staged_id, path=str(path), error=str(exc))


def _capture_status_from_message(ok: bool, message: str) -> str:
    text = str(message or "").casefold()
    if ok:
        return "ingested"
    if "moved to review" in text:
        return "quarantined"
    if text.startswith("duplicate ignored") or text.startswith("already pending review"):
        return "duplicate"
    return "failed"


@router.post("/api/capture/stage")
async def stage_capture(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    media_url: str = Form(""),
    page_title: str = Form(""),
):
    return await asyncio.to_thread(_stage_capture_sync, file, source_url, media_url, page_title)


def _stage_capture_sync(file: UploadFile, source_url: str = "", media_url: str = "", page_title: str = "") -> dict:
    ctx = get_runtime_context()
    stage_dir = _capture_stage_dir(ctx)
    stage_dir.mkdir(parents=True, exist_ok=True)

    staged_id = f"staged_{utc_now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    original_name = _clean_original_name(file.filename, staged_id, file.content_type)
    stored_path, sidecar_path = _stage_paths(staged_id, original_name, ctx)

    try:
        with stored_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    mime_type = get_mime_type(stored_path) or str(file.content_type or "").split(";", 1)[0].strip().lower()
    cfg = get_app_settings()
    accepted_media = cfg.get("ingestion", {}).get("accepted_media", {})
    allowed_mimes = accepted_media.get("mime_types", [])
    allowed_exts = {str(ext).lstrip(".").casefold() for ext in accepted_media.get("extensions", [])}
    ext = stored_path.suffix.lstrip(".").casefold()
    if (allowed_mimes and not is_allowed_mime(mime_type, allowed_mimes)) or (allowed_exts and ext not in allowed_exts):
        try:
            stored_path.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=f"Unsupported capture media: {mime_type or stored_path.suffix}")

    sidecar = {
        "staged_id": staged_id,
        "stored_name": stored_path.name,
        "original_name": original_name,
        "source_url": str(source_url or "").strip(),
        "media_url": str(media_url or "").strip(),
        "page_title": str(page_title or "").strip(),
        "mime_type": mime_type,
        "size_bytes": stored_path.stat().st_size,
        "captured_at": utc_now_str(),
        "platform_guess": _platform_guess(source_url),
    }
    atomic_write_text(sidecar_path, json.dumps(sidecar, indent=2, sort_keys=True))
    log_ingest_local("INFO", "Browser capture staged", staged_id=staged_id, mime_type=mime_type, source_url=sidecar["source_url"])
    return {
        "staged_id": staged_id,
        "platform_guess": sidecar["platform_guess"],
        "source_url": sidecar["source_url"],
        "media_url": sidecar["media_url"],
        "page_title": sidecar["page_title"],
        "original_name": sidecar["original_name"],
        "mime_type": sidecar["mime_type"],
        "size_bytes": sidecar["size_bytes"],
    }


@router.get("/api/capture/preview/{staged_id}")
async def preview_capture(staged_id: str, request: Request):
    _require_api_key(request)
    return await asyncio.to_thread(_preview_capture_sync, staged_id)


def _preview_capture_sync(staged_id: str):
    sidecar = _load_sidecar(staged_id)
    path = _staged_file_from_sidecar(sidecar)
    return FileResponse(path, media_type=sidecar.get("mime_type") or get_mime_type(path) or "application/octet-stream")


@router.delete("/api/capture/stage/{staged_id}")
async def delete_capture_stage(staged_id: str):
    return await asyncio.to_thread(_delete_capture_stage_sync, staged_id)


def _delete_capture_stage_sync(staged_id: str) -> dict:
    _cleanup_stage(staged_id)
    return {"success": True}


@router.post("/api/capture/commit")
async def commit_capture(body: CaptureCommitRequest):
    return await asyncio.to_thread(_commit_capture_sync, body)


def _commit_capture_sync(body: CaptureCommitRequest) -> dict:
    ctx = get_runtime_context()
    staged_id = _validate_staged_id(body.staged_id)
    handled = _load_handled_capture(staged_id, ctx)
    if handled:
        return handled

    commit_lock = _capture_commit_lock(staged_id)
    if not commit_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Capture is already committing")
    try:
        handled = _load_handled_capture(staged_id, ctx)
        if handled:
            return handled
        return _commit_capture_locked(body, staged_id, ctx)
    finally:
        commit_lock.release()


def _commit_capture_locked(body: CaptureCommitRequest, staged_id: str, ctx: WorkspaceContext) -> dict:
    sidecar = _load_sidecar(staged_id, ctx)
    staged_path = _staged_file_from_sidecar(sidecar, ctx)
    file_hash = calculate_file_hash(staged_path)

    metadata = {
        "source_url": str(sidecar.get("source_url") or ""),
        "artist": str(body.artist or "").strip() or "Local",
        "platform": str(body.platform or "").strip() or sidecar.get("platform_guess") or "General Web",
        "original_name": str(sidecar.get("original_name") or staged_path.name),
        "source_path": str(sidecar.get("media_url") or sidecar.get("source_url") or ""),
        "media_url": str(sidecar.get("media_url") or ""),
        "staged_from": "browser_capture",
        "ingest_type": "capture",
        "run_id": staged_id,
    }
    cfg = get_app_settings()
    ok, message, index_data = process_file(
        staged_path,
        cfg,
        metadata=metadata,
        delete_source=True,
        skip_similarity=bool(body.skip_similarity),
        ctx=ctx,
    )
    status = _capture_status_from_message(ok, message)
    if status in {"ingested", "quarantined", "duplicate"}:
        _cleanup_stage(staged_id, ctx)
    log_ingest_local(
        "INFO" if status in {"ingested", "quarantined", "duplicate"} else "ERROR",
        "Browser capture committed",
        staged_id=staged_id,
        status=status,
        hash=file_hash,
        result_message=message,
    )
    result = {
        "success": status in {"ingested", "quarantined", "duplicate"},
        "status": status,
        "hash": file_hash,
        "message": message,
        "tagging_status": (index_data or {}).get("tagging_status", ""),
    }
    if result["success"]:
        _write_handled_capture(staged_id, result, ctx)
    return result
