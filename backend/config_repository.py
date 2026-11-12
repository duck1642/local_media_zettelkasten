from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app_paths import AppPaths, app_paths_for_root
from config_schema import (
    AppSettings,
    WorkspaceConfig,
    WorkspaceRegistry,
    default_app_settings,
    default_workspace_config,
    default_workspace_registry,
)
from vault_layout import ensure_vault_directories


ModelT = TypeVar("ModelT", bound=BaseModel)
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ConfigError(RuntimeError):
    pass


class ConfigReadError(ConfigError):
    pass


class ConfigAlreadyExistsError(ConfigError):
    pass


class SettingsConflictError(ConfigError):
    pass


@dataclass(frozen=True)
class VersionedValue(Generic[ModelT]):
    value: ModelT
    etag: str


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(resolved, threading.RLock())


def _etag(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _serialize(value: BaseModel) -> bytes:
    data = value.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class YamlRepository(Generic[ModelT]):
    def __init__(self, path: Path, model_type: type[ModelT]):
        self.path = Path(path)
        self.model_type = model_type
        self._lock = _lock_for(self.path)

    def read(self) -> VersionedValue[ModelT]:
        with self._lock:
            with _file_lock(self.path):
                if not self.path.is_file():
                    raise ConfigReadError(f"configuration file does not exist: {self.path}")
                try:
                    payload = self.path.read_bytes()
                    raw = yaml.safe_load(payload.decode("utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("YAML document must be a mapping")
                    value = self.model_type.model_validate(raw)
                except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
                    raise ConfigReadError(f"invalid configuration at {self.path}: {exc}") from exc
                return VersionedValue(value=value, etag=_etag(payload))

    def create(self, value: ModelT) -> VersionedValue[ModelT]:
        validated = self.model_type.model_validate(value.model_dump(mode="python"))
        payload = _serialize(validated)
        with self._lock:
            with _file_lock(self.path):
                if self.path.exists():
                    raise ConfigAlreadyExistsError(f"refusing to replace existing configuration: {self.path}")
                _atomic_write(self.path, payload)
                return VersionedValue(value=validated, etag=_etag(payload))

    def replace(self, value: ModelT, *, expected_etag: str) -> VersionedValue[ModelT]:
        validated = self.model_type.model_validate(value.model_dump(mode="python"))
        payload = _serialize(validated)
        with self._lock:
            with _file_lock(self.path):
                if not self.path.is_file():
                    raise ConfigReadError(f"configuration file does not exist: {self.path}")
                current_payload = self.path.read_bytes()
                if _etag(current_payload) != expected_etag:
                    raise SettingsConflictError(f"configuration changed since it was read: {self.path}")
                _atomic_write(self.path.with_suffix(f"{self.path.suffix}.bak"), current_payload)
                _atomic_write(self.path, payload)
                return VersionedValue(value=validated, etag=_etag(payload))


class SettingsRepository(YamlRepository[AppSettings]):
    def __init__(self, path: Path):
        super().__init__(path, AppSettings)


class WorkspaceRegistryRepository(YamlRepository[WorkspaceRegistry]):
    def __init__(self, path: Path):
        super().__init__(path, WorkspaceRegistry)


class WorkspaceConfigRepository(YamlRepository[WorkspaceConfig]):
    def __init__(self, path: Path):
        super().__init__(path, WorkspaceConfig)


def _data_home_repositories(paths: AppPaths):
    return (
        SettingsRepository(paths.settings_path),
        WorkspaceRegistryRepository(paths.registry_path),
        WorkspaceConfigRepository(paths.default_workspace_config),
    )


def _create_data_home(paths: AppPaths) -> None:
    paths.app_dir.mkdir(parents=True, exist_ok=True)
    for directory in (paths.secrets_dir, paths.logs_dir, paths.models_dir, paths.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (
        paths.default_workspace_dir / "data" / "topics",
        paths.default_workspace_dir / "backups",
        paths.default_workspace_dir / "exports",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    ensure_vault_directories(paths.default_workspace_dir / "data" / "vaults" / "default")

    SettingsRepository(paths.settings_path).create(default_app_settings())
    WorkspaceRegistryRepository(paths.registry_path).create(default_workspace_registry())
    WorkspaceConfigRepository(paths.default_workspace_config).create(default_workspace_config())


def bootstrap_data_home(paths: AppPaths) -> None:
    paths.data_root.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(paths.data_root):
        if paths.data_root.exists():
            for repository in _data_home_repositories(paths):
                repository.read()
            for directory in (paths.secrets_dir, paths.logs_dir, paths.models_dir, paths.cache_dir):
                directory.mkdir(parents=True, exist_ok=True)
            return

        stage = paths.data_root.parent / f"{paths.data_root.name}-bootstrap-{uuid.uuid4().hex}"
        try:
            _create_data_home(app_paths_for_root(stage))
            os.replace(stage, paths.data_root)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise
