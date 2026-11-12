import subprocess
import threading
import uuid
from pathlib import Path

from PIL import Image
from logger import log_system
from media_lifecycle import remove_stale_derived_files, storage_lifecycle_lock
from runtime_context import WorkspaceContext, get_runtime_context
from utils import asset_path_for, require_storage_id, storage_shard_for_hash

TARGET_WIDTH = 600
MAX_HEIGHT = 800
JPEG_QUALITY = 80
GENERATION_CONCURRENCY = 2
_GENERATION_SEMAPHORE = threading.Semaphore(GENERATION_CONCURRENCY)


class ThumbnailBusyError(RuntimeError):
    pass


def _thumbnail_dir(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.thumbnails_dir


class _ThumbnailDirProxy:
    def __fspath__(self):
        return str(_thumbnail_dir())

    def __str__(self):
        return str(_thumbnail_dir())

    def __eq__(self, other):
        return _thumbnail_dir() == Path(other)

    def __truediv__(self, other):
        return _thumbnail_dir() / other

    def __getattr__(self, name):
        return getattr(_thumbnail_dir(), name)


THUMBNAIL_DIR = _ThumbnailDirProxy()


def thumbnail_path_for(item_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    storage_id = require_storage_id(storage_id)
    return _thumbnail_dir(ctx) / storage_shard_for_hash(item_hash) / f"{storage_id}.jpg"


def video_thumbnail_path_for(item_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    storage_id = require_storage_id(storage_id)
    return _thumbnail_dir(ctx) / storage_shard_for_hash(item_hash) / f"{storage_id}_video.jpg"


def _expected_thumbnail_path(item_hash: str, mime_type: str | None, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    if (mime_type or "").startswith("video/"):
        return video_thumbnail_path_for(item_hash, storage_id, ctx)
    return thumbnail_path_for(item_hash, storage_id, ctx)


def _thumbnail_is_fresh(thumb_path: Path, asset_path: Path) -> bool:
    return thumb_path.exists() and thumb_path.stat().st_mtime >= asset_path.stat().st_mtime


def generate_image_thumbnail(asset_path: Path, item_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    thumb_path = thumbnail_path_for(item_hash, storage_id, ctx)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = thumb_path.with_name(f".{thumb_path.stem}.{uuid.uuid4().hex}.tmp.jpg")
    try:
        with Image.open(asset_path) as image:
            image.thumbnail((TARGET_WIDTH, MAX_HEIGHT))
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(temp_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        temp_path.replace(thumb_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    log_system("INFO", "Thumbnail generated", hash=item_hash, path=str(thumb_path))
    return thumb_path


def generate_video_thumbnail(asset_path: Path, item_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    thumb_path = video_thumbnail_path_for(item_hash, storage_id, ctx)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = thumb_path.with_name(f".{thumb_path.stem}.{uuid.uuid4().hex}.tmp.jpg")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", "00:00:01", "-i", str(asset_path),
        "-vf", f"scale='min({TARGET_WIDTH},iw)':-2",
        "-frames:v", "1", str(temp_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not temp_path.exists():
            err_msg = result.stderr or "Output file not created"
            log_system("ERROR", "Video thumbnail failed", hash=item_hash, error=err_msg)
            raise RuntimeError(f"Video thumbnail generation failed: {err_msg}")
        temp_path.replace(thumb_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    log_system("INFO", "Video thumbnail generated", hash=item_hash, path=str(thumb_path))
    return thumb_path


def ensure_thumbnail(item_hash: str, extension: str | None, mime_type: str | None, wait: bool = True, storage_id: str | None = None, ctx: WorkspaceContext | None = None) -> Path:
    """Güncel küçük resmi kullan; yoksa medya türüne göre oluştur.

    Medya kilidi silme işlemiyle çakışmayı önler, semafor üretim sayısını
    sınırlar. wait=False iken gerekli izin alınamazsa ThumbnailBusyError yükselir.
    """
    storage_id = require_storage_id(storage_id)
    lifecycle_lock = storage_lifecycle_lock(storage_id, ctx=ctx)
    lifecycle_acquired = lifecycle_lock.acquire(blocking=wait)
    if not lifecycle_acquired:
        raise ThumbnailBusyError("thumbnail lifecycle is busy")
    try:
        asset_path = asset_path_for(item_hash, extension, mime_type, storage_id=storage_id, ctx=ctx)
        if not asset_path.exists():
            raise FileNotFoundError(f"Asset file not found: {asset_path}")
        thumb_path = _expected_thumbnail_path(item_hash, mime_type, storage_id, ctx)
        if _thumbnail_is_fresh(thumb_path, asset_path):
            return thumb_path
        generation_acquired = _GENERATION_SEMAPHORE.acquire(blocking=wait)
        if not generation_acquired:
            raise ThumbnailBusyError("thumbnail generation is busy")
        try:
            thumb_path = _expected_thumbnail_path(item_hash, mime_type, storage_id, ctx)
            if _thumbnail_is_fresh(thumb_path, asset_path):
                return thumb_path
            if (mime_type or "").startswith("video/"):
                return generate_video_thumbnail(asset_path, item_hash, storage_id, ctx)
            return generate_image_thumbnail(asset_path, item_hash, storage_id, ctx)
        finally:
            _GENERATION_SEMAPHORE.release()
    except Exception as e:
        log_system("ERROR", "Thumbnail generation failed", hash=item_hash, error=str(e), exc_info=True)
        raise
    finally:
        lifecycle_lock.release()


def get_or_generate_thumbnail(item_hash: str, extension: str | None, mime_type: str | None, storage_id: str | None = None, ctx: WorkspaceContext | None = None) -> Path | None:
    try:
        return ensure_thumbnail(item_hash, extension, mime_type, wait=False, storage_id=storage_id, ctx=ctx)
    except Exception:
        return None


def repair_missing_thumbnails(conn, limit: int = 100, ctx: WorkspaceContext | None = None) -> dict:
    limit = max(1, int(limit or 100))
    generated = 0
    skipped = 0
    failed = 0
    checked = 0
    stale_removed = 0
    cleanup_errors = []
    rows = conn.execute(
        """
        SELECT hash, file_extension, mime_type, storage_id
        FROM items
        WHERE mime_type LIKE 'image/%' OR mime_type LIKE 'video/%'
        ORDER BY date_added DESC
        """
    )
    for item_hash, extension, mime_type, storage_id in rows:
        if checked >= limit:
            break
        checked += 1
        with storage_lifecycle_lock(storage_id, ctx=ctx):
            asset_path = asset_path_for(item_hash, extension, mime_type, storage_id=storage_id, ctx=ctx)
            thumb_path = _expected_thumbnail_path(item_hash, mime_type, storage_id, ctx)
            removed, errors = remove_stale_derived_files(
                _thumbnail_dir(ctx),
                storage_id,
                thumb_path,
                {f"{storage_id}.jpg", f"{storage_id}_video.jpg"},
            )
            stale_removed += removed
            cleanup_errors.extend(errors)
            if not asset_path.exists():
                skipped += 1
                continue
            if _thumbnail_is_fresh(thumb_path, asset_path):
                skipped += 1
                continue
            try:
                if ctx is None:
                    thumb_result = ensure_thumbnail(item_hash, extension, mime_type, wait=True, storage_id=storage_id)
                else:
                    thumb_result = ensure_thumbnail(item_hash, extension, mime_type, wait=True, storage_id=storage_id, ctx=ctx)
                if thumb_result:
                    conn.execute(
                        "UPDATE items SET thumbnail_status = 'ready', thumbnail_error = NULL WHERE hash = ?",
                        (item_hash,)
                    )
                    generated += 1
                else:
                    conn.execute(
                        "UPDATE items SET thumbnail_status = 'failed', thumbnail_error = 'Thumbnail result was empty' WHERE hash = ?",
                        (item_hash,)
                    )
                    failed += 1
            except Exception as exc:
                conn.execute(
                    "UPDATE items SET thumbnail_status = 'failed', thumbnail_error = ? WHERE hash = ?",
                    (str(exc), item_hash)
                )
                failed += 1
    conn.commit()
    log_system("INFO", "Thumbnail repair batch finished", checked=checked, generated=generated, skipped=skipped, failed=failed, stale_removed=stale_removed, cleanup_errors=len(cleanup_errors))
    return {"checked": checked, "generated": generated, "skipped": skipped, "failed": failed, "stale_removed": stale_removed, "cleanup_errors": cleanup_errors}
