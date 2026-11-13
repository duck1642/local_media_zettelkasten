
import sys
import copy
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from runtime_context import WorkspaceContext, get_runtime_context, try_get_runtime_context
from app_paths import get_app_paths
from config_repository import SettingsRepository, WorkspaceConfigRepository

SRC_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = SRC_DIR.parent
AUTH_COOKIE_PLATFORMS = ("x", "instagram", "pinterest", "pixiv", "youtube")


def app_auth_root() -> Path:
    override = os.environ.get("LMZ_AUTH_ROOT")
    if override:
        path = Path(override).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return get_app_paths().secrets_dir / "auth"

def _resolve_config_relative(path_str: str) -> Path:
    config_root = get_runtime_context().root
    p = Path(path_str)
    return p.resolve() if p.is_absolute() else (config_root / p).resolve()

_DYNAMIC_CONSTANTS = {
    "CONFIG_PATH": lambda: get_runtime_context().config_path,
    "CONFIG_ROOT": lambda: get_runtime_context().root,
    "VAULTS_CONFIGURED": lambda: get_runtime_context().vaults_configured,
    "ACTIVE_VAULT_ID": lambda: get_runtime_context().active_vault.id,
    "ACTIVE_VAULT_NAME": lambda: get_runtime_context().active_vault.name,
    "ACTIVE_VAULT_ROOT": lambda: get_runtime_context().active_vault.root,

    "VAULT_DIR": lambda: get_runtime_context().active_vault.vault_dir,
    "INPUT_DIR": lambda: get_runtime_context().active_vault.input_dir,
    "REVIEW_DIR": lambda: get_runtime_context().active_vault.review_dir,
    "LOCAL_INGEST_DIR": lambda: get_runtime_context().active_vault.local_ingest_dir,
    "ONLINE_INGEST_DIR": lambda: get_runtime_context().active_vault.online_ingest_dir,
    "QUEUES_DIR": lambda: get_runtime_context().active_vault.queues_dir,
    "BATCHES_DIR": lambda: get_runtime_context().active_vault.batches_dir,
    "MODELS_DIR": lambda: get_runtime_context().models_dir,
    "WD_TAGS_DIR": lambda: get_runtime_context().active_vault.wd_tags_dir,
    "TOPICS_DIR": lambda: get_runtime_context().topics_dir,

    "OUTPUT_DIR": lambda: get_runtime_context().active_vault.vault_dir,
    "ASSETS_DIR": lambda: get_runtime_context().active_vault.assets_dir,
    "NOTES_DIR": lambda: get_runtime_context().active_vault.notes_dir,

    "DB_PATH": lambda: get_runtime_context().active_vault.db_path,

    "LOGS_DIR": lambda: get_runtime_context().active_vault.logs_dir,
}

def __getattr__(name: str):
    if name in _DYNAMIC_CONSTANTS:
        return _DYNAMIC_CONSTANTS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__():
    return sorted(list(globals().keys()) + list(_DYNAMIC_CONSTANTS.keys()))

EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/jxl": ".jxl",
    "image/jfif": ".jpg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
}

def storage_shard_for_hash(item_hash: str) -> str:
    return str(item_hash or "")[:2] or "00"

def require_storage_id(storage_id: str | None) -> str:
    value = str(storage_id or "").strip()
    if not value:
        raise ValueError("storage_id is required for compact storage paths")
    return value

def _ctx(ctx: WorkspaceContext | None = None) -> WorkspaceContext:
    return ctx or get_runtime_context()


def storage_asset_path_for(item_hash: str, storage_id: str, extension: str | None, mime_type: str | None = None, ctx: WorkspaceContext | None = None) -> Path:
    storage_id = require_storage_id(storage_id)
    ext = extension or EXT_MAP.get(mime_type or "", ".jpg")
    return _ctx(ctx).active_vault.assets_dir / storage_shard_for_hash(item_hash) / f"{storage_id}{ext}"

def asset_path_for(item_hash: str, extension: str | None, mime_type: str | None, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:
    return storage_asset_path_for(item_hash, storage_id, extension, mime_type, ctx=ctx)

def asset_url_for(item_hash: str, extension: str | None, mime_type: str | None = None, storage_id: str | None = None) -> str:
    storage_id = require_storage_id(storage_id)
    ext = extension or EXT_MAP.get(mime_type or "", ".jpg")
    return f"/vault/{storage_shard_for_hash(item_hash)}/{storage_id}{ext}"

DEFAULT_ALLOWED_MIMES = {
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
    'video/mp4',
    'video/webm',
    'video/ogg'
}


def setup_directories(ctx: WorkspaceContext | None = None):
    runtime = _ctx(ctx)
    vault = runtime.active_vault

    dirs_to_create = [
        vault.input_dir,
        vault.review_dir,
        vault.local_ingest_dir,
        vault.online_ingest_dir,
        vault.queues_dir,
        vault.batches_dir,
        runtime.models_dir,
        vault.wd_tags_dir,
        runtime.topics_dir,
        vault.vault_dir,
        vault.assets_dir,
        vault.notes_dir,
        vault.db_path.parent,
        vault.logs_dir,
    ]

    for directory in dirs_to_create:
        directory.mkdir(parents=True, exist_ok=True)
    for platform in AUTH_COOKIE_PLATFORMS:
        (app_auth_root() / platform).mkdir(parents=True, exist_ok=True)

def note_path_for(file_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:

    storage_id = require_storage_id(storage_id)
    return _ctx(ctx).active_vault.notes_dir / storage_shard_for_hash(file_hash) / f"{storage_id}.md"

def wd_tag_cache_path_for(file_hash: str, storage_id: str, ctx: WorkspaceContext | None = None) -> Path:

    storage_id = require_storage_id(storage_id)
    return _ctx(ctx).active_vault.wd_tags_dir / storage_shard_for_hash(file_hash) / f"{storage_id}.json"

def get_app_settings() -> dict:
    return SettingsRepository(get_app_paths().settings_path).read().value.model_dump(mode="json")


def get_workspace_config(ctx: WorkspaceContext | None = None) -> dict:
    runtime = _ctx(ctx)
    return WorkspaceConfigRepository(runtime.config_path).read().value.model_dump(mode="json")

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def utc_now_str() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(path: Path, text: str, encoding: str = "utf-8"):

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="lmztmp-", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.replace(path)
        except PermissionError:
            if path.exists():
                path.unlink()
            shutil.move(str(temp_path), str(path))
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise

def calculate_file_hash(filepath: Path) -> str:

    hasher = hashlib.sha256()

    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)

    return hasher.hexdigest()

def get_normalization_color(proc_config: dict) -> tuple:

    preset = proc_config.get('background_preset', 'white').lower()

    presets = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (128, 128, 128)
    }

    if preset == "custom":
        return tuple(proc_config.get('custom_color', [255, 255, 255]))

    return presets.get(preset, (255, 255, 255))

def flatten_image(img, background_color=(255, 255, 255)):

    from PIL import Image
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):

        background = Image.new('RGB', img.size, background_color)
        if img.mode == 'P':
            img = img.convert('RGBA')


        background.paste(img, (0, 0), img)
        return background
    return img.convert('RGB')

def calculate_phash(filepath: Path) -> Optional[str]:

    try:
        from PIL import Image
        import imagehash

        settings = get_app_settings()
        proc_config = settings.get('ingestion', {}).get('processing', {})
        do_flatten = proc_config.get('flatten_transparency', False)
        bg_color = get_normalization_color(proc_config)

        with Image.open(filepath) as raw_img:
            img = flatten_image(raw_img, bg_color) if do_flatten else raw_img
            try:
                hash_obj = imagehash.phash(img)
                return str(hash_obj)
            finally:
                if img is not raw_img:
                    img.close()
    except Exception:
        from logger import log_system
        log_system("WARNING", "Image perceptual hash failed", file=str(filepath), exc_info=True)
        return None

def _auth_platform_id(platform: str) -> str:
    clean = str(platform or "").strip().casefold()
    aliases = {
        "twitter": "x",
        "twitter.com": "x",
        "x.com": "x",
        "yt": "youtube",
        "google": "youtube",
        "google.com": "youtube",
        "youtube.com": "youtube",
    }
    return aliases.get(clean, clean)


def platform_cookie_path(platform: str) -> Path:
    platform_id = _auth_platform_id(platform)
    return app_auth_root() / platform_id / "cookies.txt"


def pixiv_refresh_token_path() -> Path:
    return app_auth_root() / "pixiv" / "refresh_token.txt"


def get_pixiv_refresh_token() -> dict:
    token_path = pixiv_refresh_token_path()
    file_status = "missing"
    if token_path.exists():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return {
                    "token": token,
                    "status": "available",
                    "source": "file",
                    "path": str(token_path),
                }
        except (OSError, UnicodeError):
            file_status = "unreadable"

    return {
        "token": "",
        "status": file_status,
        "source": file_status,
        "path": str(token_path) if token_path.exists() else "",
    }


def _cookie_file_status(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return "unreadable"
    return "available" if any(line.strip() and not line.lstrip().startswith("#") for line in lines) else "missing"


def get_platform_cookie_path(platform: str) -> dict:
    platform_id = _auth_platform_id(platform)
    platform_path = platform_cookie_path(platform_id)
    if platform_id in AUTH_COOKIE_PLATFORMS and platform_path.exists():
        status = _cookie_file_status(platform_path)
        source = "platform" if status == "available" else status
        return {"platform": platform_id, "status": status, "source": source, "path": str(platform_path)}

    return {"platform": platform_id, "status": "missing", "source": "missing", "path": ""}

def resolve_project_path(path_str: str) -> Path:

    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()

def resolve_config_path(path_str: str) -> Path:

    config_root = get_runtime_context().root
    path = Path(path_str)
    if not path.is_absolute():
        path = config_root / path
    return path.resolve()

def get_cookie_auth_status() -> dict:
    platform_details = {
        platform: get_platform_cookie_path(platform)
        for platform in AUTH_COOKIE_PLATFORMS
    }
    statuses = {detail["status"] for detail in platform_details.values()}
    aggregate = "available" if "available" in statuses else "unreadable" if "unreadable" in statuses else "missing"
    return {
        "cookies": aggregate,
        "platform_details": platform_details,
    }
