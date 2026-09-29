import asyncio
from pathlib import Path

from db.sqlite_operator import connect_database
from fastapi import APIRouter, Depends, HTTPException
from logger import log_review, log_system
from processor import process_file
from review_cache import replace_review_cache_entries, review_counts
from runtime_context import get_runtime_context
from utils import asset_url_for, get_app_settings

from api.common import (
    REVIEW_RESOLVED_STATES,
    REVIEW_VISIBLE_STATES,
    _apply_manual_frontmatter_to_item,
    _ensure_review_hash,
    _ensure_review_sidecar_defaults,
    _guess_review_mime_type,
    _is_cleanup_review_state,
    _manual_frontmatter_for_hash,
    _normalize_review_state,
    _read_review_sidecar,
    _review_cleanup_path,
    _review_dir,
    _review_display_name,
    _review_failure_status,
    _review_path,
    _review_section_for_state,
    _review_sidecar_path,
    _set_review_state,
    _sqlite_identity_for_hash,
    _write_review_sidecar,
)
from api.guards import require_usable_vault_context
from api.library import _delete_item_after_replacement

router = APIRouter(dependencies=[Depends(require_usable_vault_context)])
_review_locks: dict[str, asyncio.Lock] = {}

def _get_review_lock(resource_key: str) -> asyncio.Lock:
    if resource_key not in _review_locks:
        _review_locks[resource_key] = asyncio.Lock()
    return _review_locks[resource_key]

@router.get("/api/review/count")
async def get_review_count(include_resolved: bool = False):
    return await asyncio.to_thread(_get_review_count_sync, include_resolved)

def _iter_review_media_files() -> list[Path]:
    review_dir = _review_dir()
    if not review_dir.exists():
        return []
    return sorted(
        [
            p
            for p in review_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() not in [".json", ".md"]
        ]
    )

def _resolve_review_entries() -> list[dict]:
    files = _iter_review_media_files()
    allowed = {
        f".{ext.lstrip('.').lower()}"
        for ext in get_app_settings().get("ingestion", {}).get("accepted_media", {}).get("extensions", [])
    }
    entries: list[dict] = []
    for media_path in files:
        sidecar = _read_review_sidecar(media_path)
        changed = False
        sidecar, defaults_changed = _ensure_review_sidecar_defaults(media_path, sidecar)
        changed = changed or defaults_changed
        before_hash = str(sidecar.get("file_hash") or "")
        sidecar = _ensure_review_hash(media_path, sidecar)
        if str(sidecar.get("file_hash") or "") != before_hash:
            changed = True
        raw_state = str(sidecar.get("state") or "pending")
        state = _normalize_review_state(raw_state)
        if state in REVIEW_RESOLVED_STATES:
            # The file physically exists in the review directory but has a resolved state.
            # This is a leftover/ghost file that should have been deleted, so it is pending cleanup.
            state = "pending_cleanup"
        if raw_state != state or not raw_state:
            sidecar["state"] = state
            changed = True
        
        validation_warning = ""
        ext = media_path.suffix.lower()
        if allowed and ext not in allowed:
            validation_warning = f"File extension '{ext}' violates the accepted-media policy."

        entries.append(
            {
                "path": media_path,
                "sidecar": sidecar,
                "state": state,
                "changed": changed,
                "mime_type": _guess_review_mime_type(media_path.name),
                "extension": ext,
                "validation_warning": validation_warning,
            }
        )

    for entry in entries:
        sidecar = entry["sidecar"]
        if entry["changed"]:
            try:
                _write_review_sidecar(entry["path"], sidecar)
            except Exception as exc:
                log_system("WARNING", "Failed to persist review sidecar reconciliation", filename=entry["path"].name, error=str(exc))
    replace_review_cache_entries(entries)
    return entries

def _get_review_count_sync(include_resolved: bool = False):
    return review_counts(include_resolved)

@router.get("/api/review")
async def get_review_items(include_resolved: bool = False):
    return await asyncio.to_thread(_get_review_items_sync, include_resolved)

def _get_review_items_sync(include_resolved: bool = False):
    entries = _resolve_review_entries()
    if not include_resolved:
        entries = [entry for entry in entries if _normalize_review_state(entry["state"]) in REVIEW_VISIBLE_STATES]

    items = []
    all_hashes_set = set()
    for entry in entries:
        sidecar = entry["sidecar"]
        best_match = str(sidecar.get("best_match") or "").strip()
        if best_match:
            all_hashes_set.add(best_match)
        matches = sidecar.get("matches") or []
        for m in matches:
            m_str = str(m).strip()
            if m_str:
                all_hashes_set.add(m_str)

    best_hashes = sorted(list(all_hashes_set))
    match_map = {}
    if best_hashes:
        conn = connect_database()
        try:
            placeholders = ",".join("?" for _ in best_hashes)
            cursor = conn.cursor()
            cursor.execute(f"SELECT hash, file_extension, mime_type, source_artist, storage_id, width, height, size_bytes FROM items WHERE hash IN ({placeholders})", best_hashes)
            for row in cursor.fetchall():
                cursor.execute("SELECT tag FROM item_wd_tags WHERE item_hash = ?", (row[0],))
                tags = [r[0] for r in cursor.fetchall()]
                match_map[row[0]] = {
                    "hash": row[0],
                    "url": asset_url_for(row[0], row[1] or "", row[2] or "", storage_id=row[4]),
                    "artist": row[3],
                    "mime_type": row[2] or "",
                    "extension": row[1] or "",
                    "width": row[5],
                    "height": row[6],
                    "size_bytes": row[7],
                    "wd_tags": tags,
                }
        finally:
            conn.close()

    for entry in entries:
        p = entry["path"]
        sidecar = entry["sidecar"]
        best_match = str(sidecar.get("best_match") or "").strip()
        matches_list = sidecar.get("matches") or []
        if not matches_list and best_match:
            matches_list = [best_match]
        
        resolved_matches = []
        for m in matches_list:
            m_str = str(m).strip()
            if m_str in match_map:
                resolved_matches.append(match_map[m_str])

        display_name = _review_display_name(p, sidecar)
        items.append({
            "filename": p.name,
            "display_name": display_name,
            "url": f"/review-assets/{p.name}",
            "metadata": sidecar,
            "best_match": match_map.get(best_match) if best_match else None,
            "matches": resolved_matches,
            "mime_type": entry["mime_type"],
            "extension": entry["extension"],
            "state": entry["state"],
            "section": _review_section_for_state(entry["state"]),
            "last_action": sidecar.get("last_action") or "",
            "last_cleanup_error": sidecar.get("last_cleanup_error") or "",
            "validation_warning": entry.get("validation_warning") or "",
        })
    return items

@router.post("/api/review/{filename}/action")
async def review_action(filename: str, action: str, target_hash: str = None):
    lock = _get_review_lock(filename)
    async with lock:
        return await asyncio.to_thread(_review_action_sync, filename, action, target_hash)

def _review_action_sync(filename: str, action: str, target_hash: str = None):
    ctx = get_runtime_context()
    cfg = get_app_settings()
    if action not in {"delete", "variant", "replace"}:
        raise HTTPException(status_code=400, detail="Invalid review action")
    file_path = _review_path(filename)
    if not file_path.exists(): raise HTTPException(status_code=404)
    sidecar = _read_review_sidecar(file_path)
    sidecar, _ = _ensure_review_sidecar_defaults(file_path, sidecar)
    display_name = _review_display_name(file_path, sidecar)
    meta_path = _review_sidecar_path(file_path)
    current_state = _normalize_review_state(sidecar.get("state"))
    if current_state == "pending_cleanup":
        message = "Review item is pending cleanup; retry cleanup instead of applying review actions."
        log_review("WARNING", "Review action rejected for cleanup item", action=action, filename=filename, display_name=display_name, state=current_state)
        raise HTTPException(status_code=409, detail=message)
    metadata = sidecar.get("metadata", {}) if isinstance(sidecar, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    sidecar = _ensure_review_hash(file_path, sidecar)
    _write_review_sidecar(file_path, sidecar)

    if action == "delete":
        file_deleted, file_err = _review_cleanup_path(file_path)
        sidecar_deleted = True
        sidecar_err = ""
        if meta_path.exists():
            sidecar_deleted, sidecar_err = _review_cleanup_path(meta_path)
        if file_deleted and sidecar_deleted:
            message = "Review item deleted."
            log_review("INFO", "Review action succeeded", action=action, filename=filename, display_name=display_name, state="resolved_delete", detail=message)
            return {"status": "success", "action": action, "message": message}

        sidecar = _set_review_state(
            _read_review_sidecar(file_path) if file_path.exists() else sidecar,
            "pending_cleanup",
            cleanup_error=file_err or sidecar_err,
            action=action,
        )
        if file_path.exists():
            _write_review_sidecar(file_path, sidecar)
        message = "Review delete requested, but cleanup is pending."
        log_review("WARNING", "Review delete cleanup pending", action=action, filename=filename, display_name=display_name, state="pending_cleanup", error=file_err or sidecar_err)
        return {"status": "warning", "action": action, "message": message}

    target_hash = str(target_hash or "").strip()
    replacement_manual_fields = {}
    replacement_identity_fields = {}
    if action == "replace":
        if not target_hash:
            target_hash = str(sidecar.get("best_match") or "").strip()
        if not target_hash:
            message = "Replace target is missing. Item kept pending."
            log_review("WARNING", "Review replace warning", action=action, filename=filename, display_name=display_name, detail=message)
            return {"status": "warning", "action": action, "message": message}
        conn = connect_database(ctx=ctx)
        try:
            target_exists = bool(conn.execute("SELECT 1 FROM items WHERE hash = ?", (target_hash,)).fetchone())
        finally:
            conn.close()
        if not target_exists:
            message = "Replace target no longer exists in DB. Item kept pending."
            log_review("WARNING", "Review replace warning", action=action, filename=filename, display_name=display_name, target_hash=target_hash, detail=message)
            return {"status": "warning", "action": action, "message": message}
        replacement_manual_fields = _manual_frontmatter_for_hash(target_hash, ctx=ctx)
        replacement_identity_fields = _sqlite_identity_for_hash(target_hash, ctx=ctx)

    try:
        ok, process_message, idx_data = process_file(
            file_path,
            cfg,
            metadata=metadata,
            delete_source=True,
            skip_similarity=True,
            ctx=ctx,
            allow_pending_review=True,
        )
    except Exception as exc:
        log_review("ERROR", "Review action failed", action=action, filename=filename, display_name=display_name, target_hash=target_hash, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Review action failed: {exc}") from exc

    if not ok:
        status_code = _review_failure_status(process_message)
        log_review("ERROR", "Review action failed", action=action, filename=filename, display_name=display_name, target_hash=target_hash, error=process_message)
        raise HTTPException(status_code=status_code, detail=process_message)

    preserve_error = ""
    if action == "replace":
        new_hash = str((idx_data or {}).get("file_hash") or "").strip()
        if not new_hash:
            preserve_error = "replacement hash was not returned by processor"
        else:
            try:
                _apply_manual_frontmatter_to_item(new_hash, replacement_manual_fields, replacement_identity_fields, ctx=ctx)
            except Exception as exc:
                preserve_error = str(exc)
                log_review("ERROR", "Review replace metadata preservation failed", action=action, filename=filename, display_name=display_name, target_hash=target_hash, new_hash=new_hash, error=preserve_error)

    resolved_state = "resolved_replace" if action == "replace" else "resolved_variant"
    sidecar = _set_review_state(sidecar, resolved_state, action=action, target_hash=target_hash)
    file_deleted, file_err = _review_cleanup_path(file_path)
    sidecar_deleted = True
    sidecar_err = ""
    if meta_path.exists():
        sidecar_deleted, sidecar_err = _review_cleanup_path(meta_path)
    if not file_deleted:
        sidecar = _set_review_state(sidecar, "pending_cleanup", cleanup_error=file_err, action=action, target_hash=target_hash)
        _write_review_sidecar(file_path, sidecar)
        log_review("WARNING", "Review cleanup pending after successful ingest", action=action, filename=filename, display_name=display_name, state="pending_cleanup", target_hash=target_hash, error=file_err)
        return {
            "status": "warning",
            "action": action,
            "message": "Ingested to DB, but failed to delete review file. Item kept pending for cleanup.",
        }
    elif not sidecar_deleted:
        log_review("WARNING", "Review sidecar cleanup pending after successful ingest", action=action, filename=filename, display_name=display_name, state=resolved_state, target_hash=target_hash, error=sidecar_err)

    if preserve_error:
        message = "Replacement ingested, but old target was kept because manual metadata preservation failed."
        return {"status": "warning", "action": action, "message": message, "error": preserve_error}

    if action == "replace":
        replace_result = _delete_item_after_replacement(target_hash, ctx=ctx)
        replace_cleanup_errors = replace_result.get("cleanup_errors") or []
        if replace_result["status"] != "deleted":
            error_text = "; ".join(str(item.get("error", "")) for item in replace_result.get("cleanup_errors", []) if item.get("error"))
            message = "Replacement ingested, but old target cleanup failed. Both vault items are kept."
            log_review(
                "WARNING",
                "Review replace target cleanup failed",
                action=action,
                filename=filename,
                display_name=display_name,
                state=resolved_state,
                target_hash=target_hash,
                error=error_text or replace_result["status"],
            )
            return {"status": "warning", "action": action, "message": message}
        if replace_cleanup_errors:
            error_text = "; ".join(str(item.get("error", "")) for item in replace_cleanup_errors if item.get("error"))
            message = "Replacement ingested and old target removed from DB, but staged file cleanup is incomplete."
            log_review(
                "WARNING",
                "Review replace staged cleanup incomplete",
                action=action,
                filename=filename,
                display_name=display_name,
                state=resolved_state,
                target_hash=target_hash,
                error=error_text or "cleanup incomplete",
            )
            return {
                "status": "warning",
                "action": action,
                "message": message,
                "cleanup_errors": replace_cleanup_errors,
            }

    message = "Review item replaced and ingested." if action == "replace" else "Review item ingested as variant."
    log_review("INFO", "Review action succeeded", action=action, filename=filename, display_name=display_name, state=resolved_state, target_hash=target_hash, detail=message)
    return {"status": "success", "action": action, "message": message}

@router.post("/api/review/cleanup")
async def cleanup_review_resolved():
    return await asyncio.to_thread(_cleanup_review_resolved_sync)

def _cleanup_review_resolved_sync():
    entries = _resolve_review_entries()
    cleaned = 0
    failed = 0
    cleaned_orphans = 0
    failed_orphans = 0
    seen_sidecars = set()
    for entry in entries:
        state = entry["state"]
        if state not in REVIEW_RESOLVED_STATES and not _is_cleanup_review_state(state):
            continue
        file_path = entry["path"]
        display_name = _review_display_name(file_path, entry["sidecar"])
        sidecar_path = _review_sidecar_path(file_path)
        seen_sidecars.add(sidecar_path.resolve())
        ok_file, err_file = _review_cleanup_path(file_path)
        ok_sidecar, err_sidecar = _review_cleanup_path(sidecar_path)
        if ok_file and ok_sidecar:
            cleaned += 1
            log_review("INFO", "Review cleanup succeeded", filename=file_path.name, display_name=display_name, state=state)
            continue
        failed += 1
        if file_path.exists():
            sidecar = _read_review_sidecar(file_path)
            sidecar = _set_review_state(sidecar, "pending_cleanup", cleanup_error=err_file or err_sidecar, action=sidecar.get("last_action") or "cleanup")
            _write_review_sidecar(file_path, sidecar)
        log_review("WARNING", "Review cleanup failed", filename=file_path.name, display_name=display_name, state="pending_cleanup", error=err_file or err_sidecar)

    review_dir = _review_dir()
    if review_dir.exists():
        for sidecar_path in review_dir.glob("*.json"):
            resolved_sidecar = sidecar_path.resolve()
            if resolved_sidecar in seen_sidecars:
                continue
            media_path = sidecar_path.with_suffix("")
            if media_path.exists():
                continue
            ok_sidecar, err_sidecar = _review_cleanup_path(sidecar_path)
            if ok_sidecar:
                cleaned_orphans += 1
                log_review("INFO", "Review orphan sidecar cleaned", sidecar=str(sidecar_path.name))
            else:
                failed_orphans += 1
                log_review("WARNING", "Review orphan sidecar cleanup failed", sidecar=str(sidecar_path.name), error=err_sidecar)
    return {
        "status": "success",
        "cleaned": cleaned,
        "failed": failed,
        "cleaned_orphans": cleaned_orphans,
        "failed_orphans": failed_orphans,
    }
