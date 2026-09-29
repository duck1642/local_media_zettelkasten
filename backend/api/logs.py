import asyncio
import os
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from logger import log_svelte
from pydantic import BaseModel

from api.common import (
    _log_dirs_for_source,
    _log_file_for,
    _log_file_signature,
    _open_path_external,
    _scan_auth_status_sync,
    _tail_lines,
)

router = APIRouter()

@router.get("/api/logs")
async def stream_logs(filename: str = Query("system.jsonl"), source: str = Query("active")):
    log_file = _log_file_for(filename, source=source)

    if not log_file.exists():
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.touch()
    
    async def log_generator():
        for line in _tail_lines(log_file, 150):
            yield f"data: {line}\n\n"
        position = log_file.stat().st_size if log_file.exists() else 0
        file_signature = _log_file_signature(log_file)
        heartbeat_seconds = 15.0
        poll_seconds = 0.5
        last_emit = time.monotonic()

        while True:
            try:
                if not log_file.exists():
                    log_file.parent.mkdir(parents=True, exist_ok=True)
                    log_file.touch()

                current_signature = _log_file_signature(log_file)
                if file_signature is not None and current_signature != file_signature:
                    position = 0
                file_signature = current_signature

                size = log_file.stat().st_size
                if size < position:
                    position = 0
                elif position > 0 and size > 0:
                    try:
                        with open(log_file, "rb") as probe:
                            probe.seek(position - 1, os.SEEK_SET)
                            if probe.read(1) != b"\n":
                                position = 0
                    except OSError:
                        position = 0

                emitted = False
                with open(log_file, "rb") as f:
                    f.seek(position, os.SEEK_SET)
                    for line in f:
                        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                        yield f"data: {text}\n\n"
                        emitted = True
                    position = f.tell()

                if emitted:
                    last_emit = time.monotonic()
                elif (time.monotonic() - last_emit) >= heartbeat_seconds:
                    yield ": keep-alive\n\n"
                    last_emit = time.monotonic()
            except Exception:
                yield ": keep-alive\n\n"
                last_emit = time.monotonic()
            await asyncio.sleep(poll_seconds)
    return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get("/api/logs/location")
async def get_logs_location(source: str = Query("active")):
    from logger import log_location, startup_log_dirs

    active = log_location()
    vault_available = active["mode"] == "vault"
    if source == "startup":
        raw_dir, structured_dir = startup_log_dirs()
        selected = {
            "mode": "startup",
            "label": "Startup logs",
            "raw_dir": str(raw_dir),
            "structured_dir": str(structured_dir),
            "vault": "",
        }
    elif source == "vault":
        if not vault_available:
            raise HTTPException(status_code=503, detail="Vault logs unavailable")
        selected = active
    elif source == "console":
        selected = {
            **active,
            "mode": "console",
            "label": "Console",
        }
    elif source == "active":
        selected = active
    else:
        raise HTTPException(status_code=400, detail="Invalid log source")

    return {
        **selected,
        "active_mode": active["mode"],
        "vault_available": vault_available,
        "available_sources": ["startup", "vault", "console"] if vault_available else ["startup", "console"],
    }

@router.post("/api/auth/scan")
async def scan_auth_status():
    statuses = await asyncio.to_thread(_scan_auth_status_sync, "manual")
    return {"status": "ok", "auth": statuses}

class UILogEntry(BaseModel):
    level: str
    message: str
    extra: dict = None

@router.post("/api/logs/ui")
async def post_ui_log(entry: UILogEntry):
    log_svelte(entry.level, entry.message, **(entry.extra or {}))
    return {"status": "ok"}

@router.post("/api/logs/open")
async def open_log_external(filename: str = Query(...), source: str = Query("active")):
    return await asyncio.to_thread(_open_log_external_sync, filename, source)

def _open_log_external_sync(filename: str, source: str = "active"):
    log_file = _log_file_for(filename, source=source)
        
    if not log_file.exists(): raise HTTPException(status_code=404)
    try:
        _open_path_external(log_file)
        return {"status": "opened"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/logs/clear")
async def clear_all_logs(source: str = Query("active")):
    return await asyncio.to_thread(_clear_all_logs_sync, source)

def _clear_all_logs_sync(source: str = "active"):
    try:
        if str(source or "").strip().lower() == "console":
            log_file = _log_file_for("console.log", source="console")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("", encoding="utf-8")
            return {"status": "success"}
        raw_logs_dir, structured_logs_dir = _log_dirs_for_source(source)
        for folder in [raw_logs_dir, structured_logs_dir]:
            if folder.exists():
                for f in folder.iterdir():
                    if f.is_file() and (f.suffix == '.log' or f.suffix == '.jsonl'):
                        with open(f, 'w', encoding='utf-8') as out:
                            out.write('')
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

