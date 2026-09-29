import asyncio
import secrets
import shutil
import threading
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from artists import ensure_artist_schema, normalize_artist_name, resolve_artist_name
from fastapi import APIRouter, Depends, HTTPException
from ingest_control import online_stop_event
from logger import log_ingest_audit, log_ingest_local, log_system
from metadata_index import metadata_repair_running
from platforms import ensure_platform_schema, normalize_platform_key, resolve_platform_label
from processor import process_file
from pydantic import BaseModel
from queue_service import (
    INGESTION_LOCK,
    append_queue_block,
    clear_failed,
    move_failed_urls,
    parse_queue_preview,
    queue_counts,
    queue_path,
    read_queue,
    run_queue,
    write_queue,
)
from runtime_context import WorkspaceContext, get_runtime_context
from utils import get_app_settings, utc_now, utc_now_str
from workspace_db import connect_workspace_database

from api.common import (
    LOCAL_RESULTS_LIMIT,
    _local_ingest_dir,
    _open_path_external,
    _queue_name,
    local_ingest_lock,
    local_ingest_state,
    local_ingest_stop_event,
)
from api.guards import require_usable_vault_context

router = APIRouter(dependencies=[Depends(require_usable_vault_context)])

class QueueUpdate(BaseModel):
    content: str


class QueueAppendRequest(BaseModel):
    url: str
    artist: str | None = None
    platform: str | None = None


@router.get("/api/queue/{queue_name}")
async def get_queue(queue_name: str):
    return await asyncio.to_thread(_get_queue_sync, queue_name)

def _get_queue_sync(queue_name: str):
    queue_name = _queue_name(queue_name)
    return {"content": read_queue(queue_name), "count": queue_counts().get(queue_name, 0)}

@router.post("/api/queue/{queue_name}")
async def save_queue(queue_name: str, update: QueueUpdate):
    return await asyncio.to_thread(_save_queue_sync, queue_name, update)

def _save_queue_sync(queue_name: str, update: QueueUpdate):
    queue_name = _queue_name(queue_name)
    write_queue(queue_name, update.content)
    return {"status": "success", "count": queue_counts().get(queue_name, 0)}


@router.post("/api/queue/{queue_name}/append")
async def append_queue_entry(queue_name: str, body: QueueAppendRequest):
    return await asyncio.to_thread(_append_queue_entry_sync, queue_name, body)


def _append_queue_entry_sync(queue_name: str, body: QueueAppendRequest):
    queue_name = _queue_name(queue_name, allow_failed=False)
    url = clean_extension_queue_url(body.url)
    if not _is_supported_extension_online_host(url):
        raise HTTPException(status_code=400, detail="Unsupported online queue host")
    append_queue_block(
        queue_name,
        url,
        artist=str(body.artist or "").strip(),
        platform=str(body.platform or "").strip(),
    )
    return {"status": "success", "count": queue_counts().get(queue_name, 0)}


def clean_extension_queue_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")
    return value


def _is_supported_extension_online_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return (
        host == "x.com"
        or host.endswith(".x.com")
        or host == "twitter.com"
        or host.endswith(".twitter.com")
        or host == "pixiv.net"
        or host.endswith(".pixiv.net")
        or host == "instagram.com"
        or host.endswith(".instagram.com")
        or host == "pinterest.com"
        or host.endswith(".pinterest.com")
        or host == "pin.it"
        or host.endswith(".pin.it")
        or _is_supported_youtube_extension_url(parsed)
    )


def _is_supported_youtube_extension_url(parsed) -> bool:
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    query = parse_qs(parsed.query)

    if host == "youtu.be" or host.endswith(".youtu.be"):
        return bool(path.strip("/"))

    if host != "youtube.com" and not host.endswith(".youtube.com"):
        return False

    if path == "/watch":
        return bool(query.get("v"))
    if path.startswith("/shorts/"):
        return bool(path.removeprefix("/shorts/").strip("/"))
    if path.startswith("/post/"):
        return bool(path.removeprefix("/post/").strip("/"))
    if "/community" in path and query.get("lb"):
        return True
    return False

@router.post("/api/queue/{queue_name}/parse")
async def parse_queue_content(queue_name: str, update: QueueUpdate):
    return await asyncio.to_thread(_parse_queue_content_sync, queue_name, update)

def _parse_queue_content_sync(queue_name: str, update: QueueUpdate):
    _queue_name(queue_name)
    preview = parse_queue_preview(update.content)
    workspace_conn = connect_workspace_database()
    try:
        ensure_artist_schema(workspace_conn, backfill=False)
        ensure_platform_schema(workspace_conn, backfill=False)
        for group in preview.get("groups", []):
            artist = str(group.get("artist") or "").strip()
            if artist:
                artist_norm = normalize_artist_name(artist)
                exact = workspace_conn.execute("SELECT name FROM artists WHERE name_norm = ?", (artist_norm,)).fetchone()
                alias = workspace_conn.execute(
                    """
                    SELECT artists.name
                    FROM artist_aliases
                    JOIN artists ON artists.id = artist_aliases.artist_id
                    WHERE artist_aliases.alias_norm = ?
                    """,
                    (artist_norm,),
                ).fetchone()
                if exact:
                    group["artist_status"] = "existing"
                    group["artist_label"] = str(exact[0])
                elif alias:
                    group["artist_status"] = "alias"
                    group["artist_label"] = str(alias[0])
                else:
                    group["artist_status"] = "new"
                    group["artist_label"] = artist
            else:
                group["artist_status"] = "unknown"
                group["artist_label"] = ""

            platform = str(group.get("platform") or "").strip()
            if platform:
                platform_norm = normalize_platform_key(platform)
                exact = workspace_conn.execute("SELECT display_name FROM platforms WHERE key_norm = ?", (platform_norm,)).fetchone()
                alias = workspace_conn.execute(
                    """
                    SELECT platforms.display_name
                    FROM platform_aliases
                    JOIN platforms ON platforms.id = platform_aliases.platform_id
                    WHERE platform_aliases.alias_norm = ?
                    """,
                    (platform_norm,),
                ).fetchone()
                if exact:
                    group["platform_status"] = "existing"
                    group["platform_label"] = str(exact[0])
                elif alias:
                    group["platform_status"] = "alias"
                    group["platform_label"] = str(alias[0])
                else:
                    group["platform_status"] = "new"
                    group["platform_label"] = platform
            else:
                group["platform_status"] = "inferred"
                group["platform_label"] = ""
        return preview
    finally:
        workspace_conn.close()

@router.post("/api/queue/actions/clear-failed")
async def api_clear_failed():
    return await asyncio.to_thread(_api_clear_failed_sync)

def _api_clear_failed_sync():
    clear_failed()
    return {"status": "success", "counts": queue_counts()}

class RetryFailedBody(BaseModel):
    target: str

@router.post("/api/queue/actions/retry-failed")
async def api_retry_failed(body: RetryFailedBody):
    return await asyncio.to_thread(_api_retry_failed_sync, body)

def _api_retry_failed_sync(body: RetryFailedBody):
    if body.target not in ["normal", "force"]: raise HTTPException(400, "Invalid target")
    moved = move_failed_urls(body.target)
    return {"status": "success", "moved": moved, "counts": queue_counts()}

@router.post("/api/queue/{queue_name}/open")
async def open_queue_external(queue_name: str):
    return await asyncio.to_thread(_open_queue_external_sync, queue_name)

def _open_queue_external_sync(queue_name: str):
    queue_name = _queue_name(queue_name)
    path = queue_path(queue_name)
    if not path.exists():
        read_queue(queue_name)
    try:
        _open_path_external(path)
        return {"status": "opened"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/ingest/{queue_name}")
async def start_ingestion(queue_name: str):
    queue_name = _queue_name(queue_name, allow_failed=False)
    ctx = get_runtime_context()
    if not INGESTION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Already running")
    online_stop_event(ctx).clear()
    def run_in_background():
        try:
            run_queue(queue_name, ctx=ctx)
        except Exception as e:
            log_system("ERROR", "Ingestion worker crashed", error=str(e), traceback=traceback.format_exc())
        finally:
            INGESTION_LOCK.release()
    asyncio.get_running_loop().run_in_executor(None, run_in_background)
    return {"status": "success"}

@router.get("/api/ingest/runtime-status")
async def ingest_runtime_status():
    ctx = get_runtime_context()
    with local_ingest_lock(ctx):
        state = local_ingest_state(ctx)
        local_running = bool(state.get("running"))
        local_stop_requested = bool(state.get("stop_requested"))
    online_running = bool(INGESTION_LOCK.locked())
    return {
        "online_running": online_running,
        "online_stop_requested": bool(online_stop_event(ctx).is_set()),
        "local_running": local_running,
        "local_stop_requested": local_stop_requested,
        "any_running": bool(online_running or local_running),
    }

@router.post("/api/ingest/stop-after-current")
async def ingest_stop_after_current():
    ctx = get_runtime_context()
    online_running = bool(INGESTION_LOCK.locked())
    with local_ingest_lock(ctx):
        state = local_ingest_state(ctx)
        local_running = bool(state.get("running"))
        if local_running:
            state["stop_requested"] = True
            state["phase"] = "stopping"
    if online_running:
        online_stop_event(ctx).set()
    if local_running:
        local_ingest_stop_event(ctx).set()
    if not online_running and not local_running:
        return {"status": "idle", "message": "No ingestion is running."}
    return {
        "status": "success",
        "online_stop_requested": online_running,
        "local_stop_requested": local_running,
    }

@router.get("/api/queue-stats")
async def get_queue_stats(): return await asyncio.to_thread(queue_counts)

class LocalIngestDefaults(BaseModel):
    artist: str | None = None
    platform: str | None = None
    source_url: str | None = None

class LocalIngestStartRequest(BaseModel):
    paths: list[str]
    defaults: LocalIngestDefaults | None = None
    skip_similarity: bool = False

class LocalIngestDropIntakeRequest(BaseModel):
    session_id: str | None = None
    source_tab: str | None = None
    paths: list[str]

def _iter_local_ingest_paths(paths: list[str], stop_event: threading.Event | None = None):
    allowed_exts = {
        ext.lstrip(".").lower()
        for ext in get_app_settings().get("ingestion", {}).get("accepted_media", {}).get("extensions", [])
    }
    seen = set()
    for raw in paths or []:
        if stop_event and stop_event.is_set():
            break
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = (_local_ingest_dir() / path).resolve()
        else:
            path = path.resolve()
        if path.is_file():
            key = str(path)
            if key not in seen:
                seen.add(key)
                yield path
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if stop_event and stop_event.is_set():
                    break
                if not child.is_file():
                    continue
                ext = child.suffix.lstrip(".").lower()
                if allowed_exts and ext not in allowed_exts:
                    continue
                key = str(child.resolve())
                if key not in seen:
                    seen.add(key)
                    yield child.resolve()

def _local_drop_intake_sync(body: LocalIngestDropIntakeRequest):
    ctx = get_runtime_context()
    with local_ingest_lock(ctx):
        local_running = bool(local_ingest_state(ctx).get("running"))
    online_running = bool(INGESTION_LOCK.locked())
    if local_running or online_running:
        raise HTTPException(status_code=409, detail="Ingestion is already running")

    raw_paths = [str(path or "").strip() for path in (body.paths or []) if str(path or "").strip()]
    allowed_exts = {
        ext.lstrip(".").lower()
        for ext in get_app_settings().get("ingestion", {}).get("accepted_media", {}).get("extensions", [])
    }
    accepted: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()

    for raw in raw_paths:
        try:
            candidate = Path(raw).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (_local_ingest_dir() / candidate).resolve()
        except Exception:
            skipped.append({"path": raw, "reason": "invalid_path"})
            continue

        if not resolved.exists():
            skipped.append({"path": str(resolved), "reason": "missing_path"})
            continue

        if resolved.is_dir():
            key = str(resolved)
            if key in seen:
                skipped.append({"path": key, "reason": "duplicate_path"})
                continue
            seen.add(key)
            accepted.append(key)
            continue

        if not resolved.is_file():
            skipped.append({"path": str(resolved), "reason": "unsupported_type"})
            continue

        ext = resolved.suffix.lstrip(".").lower()
        if allowed_exts and ext not in allowed_exts:
            skipped.append({"path": str(resolved), "reason": "unsupported_extension"})
            continue

        key = str(resolved)
        if key in seen:
            skipped.append({"path": key, "reason": "duplicate_path"})
            continue
        seen.add(key)
        accepted.append(key)

    reason_counts = dict(Counter(str(item.get("reason") or "unknown") for item in skipped))
    session_id = str(body.session_id or "").strip() or _local_run_id()
    source_tab = str(body.source_tab or "").strip() or "unknown"
    summary = {
        "received": len(raw_paths),
        "accepted": len(accepted),
        "skipped": len(skipped),
    }

    log_ingest_local(
        "INFO",
        "Local drop intake processed",
        session_id=session_id,
        source_tab=source_tab,
        received=summary["received"],
        accepted=summary["accepted"],
        skipped=summary["skipped"],
        skipped_reasons=reason_counts,
    )
    return {
        "session_id": session_id,
        "accepted_paths": accepted,
        "skipped": skipped,
        "summary": summary,
    }

def _snapshot_local_ingest_state(ctx: WorkspaceContext | None = None) -> dict:
    with local_ingest_lock(ctx):
        state = local_ingest_state(ctx)
        return {
            "running": bool(state["running"]),
            "phase": state.get("phase") or "idle",
            "run_id": state.get("run_id"),
            "scanned": int(state.get("scanned") or 0),
            "staged": int(state.get("staged") or 0),
            "queued": int(state["queued"]),
            "processed": int(state["processed"]),
            "summary": dict(state["summary"]),
            "results": list(state["results"]),
            "failed_paths": list(state["failed_paths"]),
            "last_defaults": dict(state.get("last_defaults") or {}),
            "last_skip_similarity": bool(state.get("last_skip_similarity")),
            "started_at": state["started_at"],
            "finished_at": state["finished_at"],
            "stop_requested": bool(state.get("stop_requested")),
        }

def runtime_switch_preflight(ctx: WorkspaceContext | None = None) -> dict:
    ctx = ctx or get_runtime_context()
    blockers = []
    with local_ingest_lock(ctx):
        if local_ingest_state(ctx).get("running"):
            blockers.append("local_ingest_running")
    if INGESTION_LOCK.locked():
        blockers.append("online_ingest_running")
    if metadata_repair_running(ctx):
        blockers.append("metadata_repair_running")
    return {"allowed": not blockers, "blockers": blockers}

def _local_run_id() -> str:
    return f"{utc_now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"


def _submit_local_ingest_worker(loop, raw_paths: list[str], defaults: dict, skip_similarity: bool, run_id: str, ctx: WorkspaceContext):
    return loop.run_in_executor(
        None,
        _run_local_ingest_worker,
        raw_paths,
        defaults,
        skip_similarity,
        run_id,
        ctx,
    )

def _safe_staged_filename(index: int, source_path: Path) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if char in invalid or ord(char) < 32 else char for char in source_path.name).strip(" .")
    if not safe:
        safe = "local_file"
    if len(safe) > 180:
        stem = Path(safe).stem[:140]
        suffix = Path(safe).suffix[:20]
        safe = f"{stem}{suffix}"
    return f"{index:06d}_{safe}"

def _append_local_ingest_result(result: dict, ctx: WorkspaceContext | None = None):
    state = local_ingest_state(ctx)
    state["results"].append(result)
    overflow = len(state["results"]) - LOCAL_RESULTS_LIMIT
    if overflow > 0:
        del state["results"][:overflow]

def _prepare_local_ingest_run(run_id: str, defaults: dict, skip_similarity: bool, path_count: int = 0, ctx: WorkspaceContext | None = None):
    now = utc_now_str()
    with local_ingest_lock(ctx):
        state = local_ingest_state(ctx)
        if state["running"]:
            raise HTTPException(status_code=409, detail="Local ingestion already running")
        local_ingest_stop_event(ctx).clear()
        state["running"] = True
        state["phase"] = "scanning"
        state["run_id"] = run_id
        state["scanned"] = 0
        state["staged"] = 0
        state["queued"] = 0
        state["processed"] = 0
        state["summary"] = {"ingested": 0, "review": 0, "failed": 0, "duplicate": 0}
        state["results"] = []
        state["failed_paths"] = []
        state["last_defaults"] = dict(defaults or {})
        state["last_skip_similarity"] = bool(skip_similarity)
        state["started_at"] = now
        state["finished_at"] = None
        state["stop_requested"] = False
    log_ingest_local(
        "INFO",
        "Local ingest run started",
        run_id=run_id,
        path_count=path_count,
        artist=defaults.get("artist") or "Local",
        platform=defaults.get("platform") or "Local",
        skip_similarity=bool(skip_similarity),
    )

def _cleanup_local_run_dir(run_dir: Path):
    try:
        if run_dir.exists():
            shutil.rmtree(run_dir)
    except OSError as exc:
        log_ingest_local("WARNING", "Failed to clean local ingest staging directory", run_id=run_dir.name, path=str(run_dir), error=str(exc))

def _run_local_ingest_worker(raw_paths: list[str], defaults: dict, skip_similarity: bool, run_id: str, ctx: WorkspaceContext | None = None):
    ctx = ctx or get_runtime_context()
    cfg = get_app_settings()
    run_dir = ctx.active_vault.local_ingest_dir / run_id
    lock = local_ingest_lock(ctx)
    state = local_ingest_state(ctx)
    stop_event = local_ingest_stop_event(ctx)
    discovered = 0
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        for source_path in _iter_local_ingest_paths(raw_paths, stop_event):
            if stop_event.is_set():
                break
            discovered += 1
            with lock:
                state["scanned"] = discovered
                state["phase"] = "staging"
            staged_path = run_dir / _safe_staged_filename(discovered, source_path)
            try:
                shutil.copy2(source_path, staged_path)
                with lock:
                    state["staged"] = int(state.get("staged") or 0) + 1
                    state["queued"] = int(state.get("queued") or 0) + 1
            except Exception as exc:
                message = f"Local staging failed: {exc}"
                log_ingest_local(
                    "ERROR",
                    "Local ingest staging failed",
                    run_id=run_id,
                    source_path=str(source_path),
                    name=source_path.name,
                    error=str(exc),
                )
                with lock:
                    state["summary"]["failed"] = int(state["summary"].get("failed", 0)) + 1
                    state["failed_paths"].append(str(source_path))
                    _append_local_ingest_result(
                        {
                            "path": str(source_path),
                            "source_path": str(source_path),
                            "staged_path": "",
                            "name": source_path.name,
                            "status": "failed",
                            "message": message,
                        },
                        ctx=ctx,
                    )
                continue

            metadata = {
                "artist": defaults.get("artist") or "Local",
                "platform": defaults.get("platform") or "Local",
                "source_url": defaults.get("source_url") or "",
                "original_path": str(source_path),
                "staged_from": "local",
                "ingest_type": "local",
                "run_id": run_id,
            }
            with lock:
                state["phase"] = "running"
            try:
                ok, message, index_data = process_file(
                    staged_path,
                    cfg,
                    metadata=metadata,
                    delete_source=True,
                    skip_similarity=skip_similarity,
                    ctx=ctx,
                )
                tag_status = str((index_data or {}).get("tagging_status") or "").strip()
                tag_count = int((index_data or {}).get("tagging_tag_count") or 0)
                tag_error = str((index_data or {}).get("tagging_error") or "").strip()
                if ok and tag_status:
                    if tag_status == "ok":
                        message = f"{message} | WD tags: ok ({tag_count})"
                    elif tag_error:
                        message = f"{message} | WD tags: {tag_status} ({tag_error})"
                    else:
                        message = f"{message} | WD tags: {tag_status}"
                if ok:
                    status = "ingested"
                elif "moved to review" in message.lower():
                    status = "review"
                elif message.lower().startswith("already pending review"):
                    status = "duplicate"
                elif message.lower().startswith("duplicate ignored"):
                    status = "duplicate"
                else:
                    status = "failed"
            except Exception as exc:
                status = "failed"
                message = f"Local ingest crash: {exc}"
            log_ingest_local(
                "INFO" if status in {"ingested", "duplicate", "review"} else "ERROR",
                "Local ingest item processed",
                run_id=run_id,
                status=status,
                source_path=str(source_path),
                name=source_path.name,
                result_message=message,
            )

            with lock:
                state["processed"] += 1
                state["queued"] = max(0, int(state.get("queued") or 0) - 1)
                state["summary"][status] = int(state["summary"].get(status, 0)) + 1
                _append_local_ingest_result(
                    {
                        "path": str(source_path),
                        "source_path": str(source_path),
                        "staged_path": str(staged_path),
                        "name": source_path.name,
                        "status": status,
                        "message": message,
                    },
                    ctx=ctx,
                )
                if status == "failed":
                    state["failed_paths"].append(str(source_path))
                if not stop_event.is_set():
                    state["phase"] = "scanning"

        if discovered == 0:
            with lock:
                state["phase"] = "finished" if stop_event.is_set() else "failed"
                if not stop_event.is_set():
                    log_ingest_local("WARNING", "Local ingest found no valid files", run_id=run_id)
                    _append_local_ingest_result(
                        {
                            "path": "",
                            "source_path": "",
                            "staged_path": "",
                            "name": "",
                            "status": "failed",
                            "message": "No valid local files found",
                        },
                        ctx=ctx,
                    )
                    state["summary"]["failed"] = 1
        elif stop_event.is_set():
            log_ingest_local("INFO", "Local ingest stop-after-current acknowledged", run_id=run_id)
            with lock:
                state["phase"] = "stopping"
    except Exception as exc:
        log_ingest_local("ERROR", "Local ingest worker crashed", run_id=run_id, error=str(exc), traceback=traceback.format_exc())
        with lock:
            state["phase"] = "failed"
            state["summary"]["failed"] = int(state["summary"].get("failed", 0)) + 1
            _append_local_ingest_result(
                {
                    "path": "",
                    "source_path": "",
                    "staged_path": "",
                    "name": "",
                    "status": "failed",
                    "message": f"Local ingest worker crashed: {exc}",
                },
                ctx=ctx,
            )
    finally:
        _cleanup_local_run_dir(run_dir)
        with lock:
            if state.get("phase") not in {"failed", "stopping"}:
                state["phase"] = "finished"
            elif state.get("phase") == "stopping":
                state["phase"] = "finished"
            state["running"] = False
            state["finished_at"] = utc_now_str()
            state["stop_requested"] = False
            summary = dict(state.get("summary") or {})
            phase = str(state.get("phase") or "")
        stop_event.clear()
        log_ingest_local(
            "INFO" if phase == "finished" else "ERROR",
            "Local ingest run finished",
            run_id=run_id,
            phase=phase,
            summary_ingested=summary.get("ingested", 0),
            summary_review=summary.get("review", 0),
            summary_failed=summary.get("failed", 0),
            summary_duplicate=summary.get("duplicate", 0),
        )
        log_ingest_audit(
            "INFO" if phase == "finished" else "ERROR",
            "Local ingestion run summary",
            ingest_type="local",
            run_id=run_id,
            phase=phase,
            summary_ingested=summary.get("ingested", 0),
            summary_review=summary.get("review", 0),
            summary_failed=summary.get("failed", 0),
            summary_duplicate=summary.get("duplicate", 0),
        )

def _resolve_local_ingest_defaults(defaults: dict) -> dict:
    resolved = dict(defaults or {})
    conn = connect_workspace_database()
    try:
        resolved["artist"] = resolve_artist_name(conn, resolved.get("artist") or "Local")
        resolved["platform"] = resolve_platform_label(conn, resolved.get("platform") or "Local")
        resolved["source_url"] = str(resolved.get("source_url") or "")
        conn.commit()
        return resolved
    finally:
        conn.close()

@router.post("/api/local-ingest/start")
async def local_ingest_start(body: LocalIngestStartRequest):
    ctx = get_runtime_context()
    raw_paths = [str(path or "").strip() for path in (body.paths or []) if str(path or "").strip()]
    if not raw_paths:
        raise HTTPException(status_code=400, detail="No local paths provided")
    defaults = _resolve_local_ingest_defaults(body.defaults.model_dump() if body.defaults else {})
    run_id = _local_run_id()
    _prepare_local_ingest_run(run_id, defaults, bool(body.skip_similarity), len(raw_paths), ctx=ctx)
    _submit_local_ingest_worker(asyncio.get_running_loop(), raw_paths, defaults, bool(body.skip_similarity), run_id, ctx)
    return {"status": "success", "run_id": run_id, "phase": "scanning"}

@router.post("/api/local-ingest/drop-intake")
async def local_ingest_drop_intake(body: LocalIngestDropIntakeRequest):
    return await asyncio.to_thread(_local_drop_intake_sync, body)

@router.get("/api/local-ingest/status")
async def local_ingest_status():
    return await asyncio.to_thread(_snapshot_local_ingest_state)

@router.post("/api/local-ingest/retry-failed")
async def local_ingest_retry_failed():
    ctx = get_runtime_context()
    with local_ingest_lock(ctx):
        state = local_ingest_state(ctx)
        if state["running"]:
            raise HTTPException(status_code=409, detail="Local ingestion already running")
        failed_paths = list(state.get("failed_paths") or [])
        defaults = dict(state.get("last_defaults") or {})
        skip_similarity = bool(state.get("last_skip_similarity"))
    if not failed_paths:
        return {"status": "success", "queued": 0, "phase": "idle"}
    run_id = _local_run_id()
    defaults = _resolve_local_ingest_defaults(defaults)
    _prepare_local_ingest_run(run_id, defaults, skip_similarity, len(failed_paths), ctx=ctx)
    _submit_local_ingest_worker(asyncio.get_running_loop(), failed_paths, defaults, skip_similarity, run_id, ctx)
    return {"status": "success", "run_id": run_id, "phase": "scanning", "queued": len(failed_paths)}
