import contextlib
import hashlib
import shutil
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml


MANIFEST_NAME = "lmz-package.yaml"
PACKAGE_VERSION = 1
BACKUP_PACKAGE_TYPE = "lmz_vault_backup"
EXPORT_PACKAGE_TYPE = "lmz_vault_export"
VALID_PACKAGE_TYPES = {BACKUP_PACKAGE_TYPE, EXPORT_PACKAGE_TYPE}

_PACKAGE_LOCKS: dict[str, threading.Lock] = {}
_PACKAGE_LOCKS_GUARD = threading.Lock()
_BACKUP_STATE_LOCK = threading.Lock()
_backup_running = False
_active_writes = 0


class VaultPackageError(ValueError):
    pass


class BackupBusyError(VaultPackageError):
    pass


@contextlib.contextmanager
def vault_write_window():
    """Register a write without holding a thread lock while the work runs."""
    global _active_writes
    with _BACKUP_STATE_LOCK:
        if _backup_running:
            raise BackupBusyError("Vault backup is running. Try again when it finishes.")
        _active_writes += 1
    try:
        yield
    finally:
        with _BACKUP_STATE_LOCK:
            _active_writes -= 1


@contextlib.contextmanager
def vault_backup_window():
    global _backup_running
    with _BACKUP_STATE_LOCK:
        if _backup_running or _active_writes:
            raise BackupBusyError("Cannot start backup while another backup or write is running. Try again shortly.")
        _backup_running = True
    try:
        yield
    finally:
        with _BACKUP_STATE_LOCK:
            _backup_running = False


def utc_package_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def operation_id() -> str:
    return uuid.uuid4().hex


def package_fingerprint(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *,
    package_type: str,
    source_vault_id: str,
    source_vault_name: str,
    contents: dict,
    item_count: int,
    file_count: int,
    created_at: str | None = None,
) -> dict:
    manifest = {
        "package_type": package_type,
        "package_version": PACKAGE_VERSION,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "source_vault": {
            "id": str(source_vault_id or "").strip(),
            "name": str(source_vault_name or "").strip(),
        },
        "contents": dict(contents or {}),
        "counts": {
            "items": int(item_count or 0),
            "files": int(file_count or 0),
        },
    }
    validate_manifest(manifest)
    return manifest


def manifest_bytes(manifest: dict) -> bytes:
    validate_manifest(manifest)
    return yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8")


def load_manifest_from_archive(archive: zipfile.ZipFile, *, expected_type: str | None = None) -> dict:
    try:
        raw = archive.read(MANIFEST_NAME)
    except KeyError as exc:
        raise VaultPackageError(f"missing package manifest: {MANIFEST_NAME}") from exc
    try:
        manifest = yaml.safe_load(raw.decode("utf-8")) or {}
    except Exception as exc:
        raise VaultPackageError("invalid package manifest") from exc
    if not isinstance(manifest, dict):
        raise VaultPackageError("package manifest must be an object")
    validate_manifest(manifest, expected_type=expected_type)
    return manifest


def validate_manifest(manifest: dict, *, expected_type: str | None = None) -> None:
    if not isinstance(manifest, dict):
        raise VaultPackageError("package manifest must be an object")
    package_type = manifest.get("package_type")
    if package_type not in VALID_PACKAGE_TYPES:
        raise VaultPackageError(f"unsupported package type: {package_type or '<missing>'}")
    if expected_type and package_type != expected_type:
        raise VaultPackageError(f"expected package type {expected_type}, got {package_type}")
    if manifest.get("package_version") != PACKAGE_VERSION:
        raise VaultPackageError(f"unsupported package version: {manifest.get('package_version')}")

    source = manifest.get("source_vault")
    if not isinstance(source, dict) or not str(source.get("id") or "").strip() or not str(source.get("name") or "").strip():
        raise VaultPackageError("package manifest requires source_vault id and name")
    if not str(manifest.get("created_at") or "").strip():
        raise VaultPackageError("package manifest requires created_at")
    if not isinstance(manifest.get("contents"), dict):
        raise VaultPackageError("package manifest requires contents")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise VaultPackageError("package manifest requires counts")
    for key in ("items", "files"):
        value = counts.get(key)
        if not isinstance(value, int) or value < 0:
            raise VaultPackageError(f"package manifest count must be a non-negative integer: {key}")
    _reject_absolute_path_fields(manifest)


def _reject_absolute_path_fields(value, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            path_key = key_text in {"path", "root", "dir"} or key_text.endswith(("_path", "_root", "_dir"))
            if path_key and isinstance(child, str) and _looks_absolute_path(child):
                raise VaultPackageError(f"package manifest contains absolute path field: {'.'.join(child_path)}")
            _reject_absolute_path_fields(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_absolute_path_fields(child, (*path, str(index)))


def _looks_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return Path(text).is_absolute() or text.startswith(("/", "\\")) or (len(text) > 2 and text[1] == ":" and text[2] in ("/", "\\"))


def validate_archive_members(archive: zipfile.ZipFile, *, allowed_roots: Iterable[str]) -> list[zipfile.ZipInfo]:
    """Manifesti zorunlu tut; çıkartılacak dosyaları izin verilen klasörlerle sınırla."""
    allowed = {root.strip("/") for root in allowed_roots if str(root or "").strip("/")}
    if not allowed:
        raise VaultPackageError("archive validation requires allowed roots")
    payload_members: list[zipfile.ZipInfo] = []
    seen_manifest = False
    for member in archive.infolist():
        filename = member.filename
        if filename == MANIFEST_NAME:
            seen_manifest = True
            continue
        if member.is_dir():
            continue
        safe_name = validate_member_name(filename, allowed_roots=allowed)
        payload_members.append(member)
        member.filename = safe_name
    if not seen_manifest:
        raise VaultPackageError(f"missing package manifest: {MANIFEST_NAME}")
    return payload_members


def validate_member_name(filename: str, *, allowed_roots: Iterable[str]) -> str:
    raw = str(filename or "")
    if not raw:
        raise VaultPackageError("empty archive member path")
    if "\\" in raw:
        raise VaultPackageError(f"unsafe archive path: {filename}")
    if raw.startswith("/") or raw.startswith("\\") or (len(raw) > 2 and raw[1] == ":" and raw[2] == "/"):
        raise VaultPackageError(f"unsafe archive path: {filename}")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise VaultPackageError(f"unsafe archive path: {filename}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise VaultPackageError(f"unsafe archive path: {filename}")
    allowed = {root.strip("/") for root in allowed_roots if str(root or "").strip("/")}
    if not path.parts or path.parts[0] not in allowed:
        raise VaultPackageError(f"unsupported archive path: {filename}")
    return path.as_posix()


def extract_members(archive: zipfile.ZipFile, destination: Path, members: Iterable[zipfile.ZipInfo]) -> None:
    """Dosyaları hedefe akış halinde yaz; çözümlenen yolun hedef dışına çıkmasını engelle."""
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for member in members:
        target = (root / member.filename).resolve()
        if root not in target.parents and target != root:
            raise VaultPackageError(f"unsafe package path: {member.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def snapshot_sqlite_database(source_db: Path, target_db: Path) -> None:
    """Ana dosyayı kopyalamak yerine SQLite ile tutarlı bir veritabanı anlık görüntüsü al."""
    if not source_db.exists():
        raise VaultPackageError(f"database does not exist: {source_db}")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_db)
    try:
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


@contextlib.contextmanager
def package_operation_lock(key: str):
    lock_key = str(key or "global")
    with _PACKAGE_LOCKS_GUARD:
        lock = _PACKAGE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _PACKAGE_LOCKS[lock_key] = lock
    if not lock.acquire(blocking=False):
        raise VaultPackageError(f"package operation already running: {lock_key}")
    try:
        yield
    finally:
        lock.release()
