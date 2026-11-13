import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from config_repository import ConfigReadError
from jxl_preview import register_jxl_decoder
from logger import log_system
from media_lifecycle import storage_lifecycle_lock
from runtime_context import WorkspaceContext, get_runtime_context
from thumbnail_store import read_thumbnail, write_thumbnail
from utils import asset_path_for, get_app_settings, require_storage_id, storage_shard_for_hash

TARGET_WIDTH = 600
MAX_HEIGHT = 800
JPEG_QUALITY = 80
GENERATION_CONCURRENCY = 2
_GENERATION_SEMAPHORE = threading.Semaphore(GENERATION_CONCURRENCY)
_BACKGROUND = ThreadPoolExecutor(max_workers=GENERATION_CONCURRENCY, thread_name_prefix="thumbnail")
_BACKGROUND_LOCK = threading.Lock()
_PENDING: set[tuple[Path, str]] = set()
_MAX_PENDING = 32


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


def thumbnail_db_path(ctx: WorkspaceContext | None = None) -> Path:
    return thumbnail_db_path_for_vault((ctx or get_runtime_context()).active_vault.root)


def thumbnail_db_path_for_vault(root: Path) -> Path:
    return root / "ui_cache" / "thumbs.db"


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


def _configured_format() -> str:
    try:
        return get_app_settings()["ingestion"]["processing"]["thumbnail_format"]
    except (ConfigReadError, KeyError):
        return "jpeg"


def _encode_thumbnail(image: Image.Image, output_format: str) -> bytes:
    with BytesIO() as output:
        if output_format == "jpeg":
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
        else:
            image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
            image.save(output, output_format.upper())
        return output.getvalue()


def generate_image_thumbnail(asset_path: Path, output_format: str = "jpeg") -> bytes:
    if asset_path.suffix.lower() == ".jxl":
        register_jxl_decoder()
    with Image.open(asset_path) as image:
        image.thumbnail((TARGET_WIDTH, MAX_HEIGHT))
        return _encode_thumbnail(image, output_format)


def generate_video_thumbnail(asset_path: Path, output_format: str = "jpeg") -> bytes:
    with TemporaryDirectory(prefix="lmz-thumb-") as temp_dir:
        temp_path = Path(temp_dir) / ("thumbnail.jpg" if output_format == "jpeg" else "thumbnail.png")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", "00:00:01", "-i", str(asset_path),
            "-vf", f"scale='min({TARGET_WIDTH},iw)':-2",
            "-frames:v", "1", str(temp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not temp_path.exists():
            err_msg = result.stderr or "Output file not created"
            raise RuntimeError(f"Video thumbnail generation failed: {err_msg}")
        if output_format == "jpeg":
            return temp_path.read_bytes()
        with Image.open(temp_path) as image:
            return _encode_thumbnail(image, output_format)


def ensure_thumbnail(item_hash: str, extension: str | None, mime_type: str | None, wait: bool = True, storage_id: str | None = None, ctx: WorkspaceContext | None = None, *, rebuild: bool = False, cache_only: bool = False, require_store: bool = False, persist_legacy: bool = True) -> tuple[bytes, str] | None:
    """Return a cached or newly generated thumbnail without relying on legacy files.

    Medya kilidi silme işlemiyle çakışmayı önler, semafor üretim sayısını
    sınırlar. wait=False iken gerekli izin alınamazsa ThumbnailBusyError yükselir.
    """
    ctx = ctx or get_runtime_context()
    storage_id = require_storage_id(storage_id)
    asset_path = asset_path_for(item_hash, extension, mime_type, storage_id=storage_id, ctx=ctx)
    db_path = thumbnail_db_path(ctx)
    if not rebuild:
        source_stat = asset_path.stat()
        cached = read_thumbnail(db_path, storage_id, source_stat.st_mtime_ns, source_stat.st_size)
        if cached is not None:
            return cached
    lifecycle_lock = storage_lifecycle_lock(storage_id, ctx=ctx)
    lifecycle_acquired = lifecycle_lock.acquire(blocking=wait)
    if not lifecycle_acquired:
        raise ThumbnailBusyError("thumbnail lifecycle is busy")
    try:
        source_stat = asset_path.stat()
        key = storage_id, source_stat.st_mtime_ns, source_stat.st_size
        if not rebuild:
            cached = read_thumbnail(db_path, *key)
            if cached is not None:
                return cached
            # Migration copies the exact old JPEG and leaves it in place.
            legacy = _expected_thumbnail_path(item_hash, mime_type, storage_id, ctx)
            try:
                if legacy.stat().st_mtime_ns >= source_stat.st_mtime_ns:
                    data = legacy.read_bytes()
                    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
                        if not persist_legacy or write_thumbnail(db_path, *key, data, "image/jpeg") or not require_store:
                            return data, "image/jpeg"
            except OSError:
                pass
        if cache_only:
            return None
        generation_acquired = _GENERATION_SEMAPHORE.acquire(blocking=wait)
        if not generation_acquired:
            raise ThumbnailBusyError("thumbnail generation is busy")
        try:
            output_format = _configured_format()
            generate = generate_video_thumbnail if (mime_type or "").startswith("video/") else generate_image_thumbnail
            result = generate(asset_path, output_format), f"image/{output_format}"
            if not write_thumbnail(db_path, *key, *result) and (rebuild or require_store):
                raise OSError("Could not update thumbnail cache")
            return result
        finally:
            _GENERATION_SEMAPHORE.release()
    except ThumbnailBusyError:
        raise
    except Exception as e:
        log_system("ERROR", "Thumbnail generation failed", hash=item_hash, error=str(e), exc_info=True)
        raise
    finally:
        lifecycle_lock.release()


def get_or_generate_thumbnail(item_hash: str, extension: str | None, mime_type: str | None, storage_id: str | None = None, ctx: WorkspaceContext | None = None) -> tuple[bytes, str] | None:
    try:
        return ensure_thumbnail(item_hash, extension, mime_type, wait=False, storage_id=storage_id, ctx=ctx)
    except ThumbnailBusyError:
        raise
    except Exception:
        return None


def queue_thumbnail(item_hash: str, extension: str | None, mime_type: str | None,
                    storage_id: str, ctx: WorkspaceContext) -> None:
    key = thumbnail_db_path(ctx), storage_id
    with _BACKGROUND_LOCK:
        if key in _PENDING or len(_PENDING) >= _MAX_PENDING:
            return
        _PENDING.add(key)

    def generate():
        from db.sqlite_operator import connect_database
        from vault_packages import BackupBusyError, vault_write_window

        try:
            with vault_write_window(), storage_lifecycle_lock(storage_id, ctx=ctx):
                with closing(connect_database(ctx=ctx)) as conn:
                    row = conn.execute(
                        "SELECT thumbnail_status FROM items WHERE hash = ? AND storage_id = ?",
                        (item_hash, storage_id),
                    ).fetchone()
                    if not row or row[0] == "failed":
                        return
                    try:
                        # A later request can only read the worker's output from disk.
                        ensure_thumbnail(item_hash, extension, mime_type, storage_id=storage_id,
                                         ctx=ctx, require_store=True)
                    except Exception:
                        status, error = "failed", "Thumbnail generation failed"
                    else:
                        status, error = "ready", None
                    with conn:
                        conn.execute(
                            "UPDATE items SET thumbnail_status = ?, thumbnail_error = ? WHERE hash = ? AND storage_id = ?",
                            (status, error, item_hash, storage_id),
                        )
        except BackupBusyError:
            pass  # The next thumbnail request can queue it again after backup.
        except Exception:
            log_system("ERROR", "Thumbnail background task failed", hash=item_hash, exc_info=True)
        finally:
            with _BACKGROUND_LOCK:
                _PENDING.discard(key)

    try:
        _BACKGROUND.submit(generate)
    except RuntimeError:
        with _BACKGROUND_LOCK:
            _PENDING.discard(key)


def repair_missing_thumbnails(conn, limit: int = 100, ctx: WorkspaceContext | None = None, *, rebuild: bool = False) -> dict:
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
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for item_hash, extension, mime_type, storage_id in rows:
        if checked >= limit:
            break
        checked += 1
        with storage_lifecycle_lock(storage_id, ctx=ctx):
            asset_path = asset_path_for(item_hash, extension, mime_type, storage_id=storage_id, ctx=ctx)
            try:
                source_stat = asset_path.stat()
            except FileNotFoundError:
                skipped += 1
                continue
            if not rebuild:
                cached = read_thumbnail(
                    thumbnail_db_path(ctx), storage_id, source_stat.st_mtime_ns, source_stat.st_size
                )
                thumb_path = _expected_thumbnail_path(item_hash, mime_type, storage_id, ctx)
                if cached is not None or _thumbnail_is_fresh(thumb_path, asset_path):
                    skipped += 1
                    continue
            try:
                if rebuild:
                    thumb_result = ensure_thumbnail(item_hash, extension, mime_type, storage_id=storage_id,
                                                    ctx=ctx, rebuild=True)
                elif ctx is None:
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
                if not rebuild:
                    conn.execute(
                        "UPDATE items SET thumbnail_status = 'failed', thumbnail_error = ? WHERE hash = ?",
                        (str(exc), item_hash)
                    )
                failed += 1
            # Do not hold the main database write lock while rebuilding the rest of the vault.
            conn.commit()
    conn.commit()
    log_system("INFO", "Thumbnail repair batch finished", checked=checked, generated=generated, skipped=skipped, failed=failed, stale_removed=stale_removed, cleanup_errors=len(cleanup_errors))
    return {"checked": checked, "generated": generated, "skipped": skipped, "failed": failed, "stale_removed": stale_removed, "cleanup_errors": cleanup_errors}
