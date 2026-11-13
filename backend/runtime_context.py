"""Çalışma alanı ile aktif kasanın yollarını tek bir bağlamda toplar."""

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from app_paths import get_app_paths
from config_repository import WorkspaceConfigRepository


import sys

SRC_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = SRC_DIR.parent


# frozen=True, oluşturulan bağlamın alanlarına sonradan atama yapılmasını önler.
# Kasa değiştirirken mevcut nesneyi parça parça değiştirmek yerine yenisi yayınlanır.
@dataclass(frozen=True)
class VaultContext:
    """Bir kasaya özel dosya, veritabanı, önbellek ve kuyruk yolları."""

    id: str
    name: str
    root: Path
    vault_dir: Path
    assets_dir: Path
    notes_dir: Path
    db_path: Path
    review_dir: Path
    queues_dir: Path
    local_ingest_dir: Path
    online_ingest_dir: Path
    batches_dir: Path
    input_dir: Path
    wd_tags_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class WorkspaceContext:
    """Çalışma alanının ortak yolları ve o anda seçili kasanın bağlamı."""

    config_path: Path
    root: Path
    topics_dir: Path
    models_dir: Path
    workspace_db_path: Path
    active_vault: VaultContext
    vaults_configured: bool


_CONTEXT_LOCK = threading.RLock()
_RUNTIME_CONTEXT: WorkspaceContext | None = None


class RuntimeNotLoadedError(RuntimeError):
    """Henüz bir çalışma alanı seçilmeden kasa bağlamı istendi."""

    pass


def _slug_vault_id(value: str) -> str:
    cleaned = "".join(ch.casefold() if ch.isalnum() else "-" for ch in str(value or "").strip())
    return "-".join(part for part in cleaned.split("-") if part) or "default"


def _resolve_config_path(config_path: str | Path | None = None) -> Path:
    """Önce açıkça verilen yolu, sonra ortam ayarını, en son kayıt defterini kullan."""
    data_root = get_app_paths().data_root
    if config_path is not None:
        candidate = Path(config_path).expanduser()
        return (candidate if candidate.is_absolute() else data_root / candidate).resolve()

    env_path = os.environ.get("LMZ_CONFIG_PATH")
    if env_path:
        candidate = Path(env_path).expanduser()
        return (candidate if candidate.is_absolute() else data_root / candidate).resolve()

    from workspaces import active_workspace_config_path

    return active_workspace_config_path()


def _config_root_for(config_path: Path) -> Path:
    return config_path.parent


def _load_config(config_path: Path):
    return WorkspaceConfigRepository(config_path).read().value


def _resolve_from_root(root: Path, path_str: str | Path) -> Path:
    value = Path(path_str)
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def build_vault_context(vault_id: str, name: str, vault_root: Path) -> VaultContext:
    """Kasa yollarını hesapla; klasör oluşturma veya aktif kasayı değiştirme."""
    vault_dir = vault_root / "vault"
    return VaultContext(
        id=vault_id,
        name=name,
        root=vault_root,
        vault_dir=vault_dir,
        assets_dir=vault_dir / "assets",
        notes_dir=vault_dir / "notes",
        db_path=vault_root / "db" / "lmz_main.db",
        review_dir=vault_root / "review",
        queues_dir=vault_root / "queues",
        local_ingest_dir=vault_root / "local_ingest",
        online_ingest_dir=vault_root / "online_ingest",
        batches_dir=vault_root / "batches",
        input_dir=vault_root / "input",
        wd_tags_dir=vault_root / "wd-tags",
        logs_dir=vault_root / "logs",
    )


def build_runtime_context(config_path: str | Path | None = None, active_vault_id: str | None = None) -> WorkspaceContext:
    """Ayarları oku ve çalışma alanı dışına çıkmayan bir kasa bağlamı kur.

    Kasa kimliği verilmezse ayarlardaki aktif kasa kullanılır. Bu işlev
    bağlamı sadece hazırlar; çalışan servisleri veya süreç durumunu değiştirmez.
    """
    resolved_config_path = _resolve_config_path(config_path)
    root = _config_root_for(resolved_config_path)
    config = _load_config(resolved_config_path)
    vaults = config.vaults
    active_id = _slug_vault_id(str(active_vault_id or config.active_vault))
    if active_id not in vaults:
        raise ValueError(f"unknown vault: {active_id}")
    entry = vaults[active_id]
    vault_root = _resolve_from_root(root, entry.root)
    if vault_root == root or not vault_root.is_relative_to(root):
        raise ValueError(f"vault root escapes workspace: {entry.root}")
    active_vault = build_vault_context(active_id, entry.name, vault_root)

    return WorkspaceContext(
        config_path=resolved_config_path,
        root=root,
        topics_dir=(root / "data" / "topics").resolve(),
        models_dir=get_app_paths().models_dir,
        workspace_db_path=root / "data" / "workspace.db",
        active_vault=active_vault,
        vaults_configured=bool(vaults),
    )


def has_runtime_context() -> bool:
    with _CONTEXT_LOCK:
        return _RUNTIME_CONTEXT is not None


def try_get_runtime_context() -> WorkspaceContext | None:
    """Bağlamı döndür; henüz yüklenmediyse yeni bir bağlam oluşturmadan None ver."""
    with _CONTEXT_LOCK:
        return _RUNTIME_CONTEXT


def clear_runtime_context():
    """Süreçteki bağlam referansını kaldır; kasa dosyalarına dokunma."""
    global _RUNTIME_CONTEXT
    with _CONTEXT_LOCK:
        _RUNTIME_CONTEXT = None


def set_runtime_context(ctx: WorkspaceContext) -> WorkspaceContext:
    """Hazır bağlamı süreç genelinde yayınla; bağlı servisleri yeniden başlatma."""
    global _RUNTIME_CONTEXT
    with _CONTEXT_LOCK:
        _RUNTIME_CONTEXT = ctx
        return _RUNTIME_CONTEXT


def get_runtime_context() -> WorkspaceContext:
    """Bağlamı döndür veya LMZ_CONFIG_PATH varsa ilk erişimde yükle.

    Çalışma alanı seçilmemiş ve ortamda yol verilmemişse RuntimeNotLoadedError
    yükselir. Yüklemeyi tetiklememek için try_get_runtime_context kullanılır.
    """
    global _RUNTIME_CONTEXT
    with _CONTEXT_LOCK:
        if _RUNTIME_CONTEXT is None:
            env_path = os.environ.get("LMZ_CONFIG_PATH")
            if env_path:
                _RUNTIME_CONTEXT = build_runtime_context(env_path)
                return _RUNTIME_CONTEXT
            raise RuntimeNotLoadedError("Workspace not loaded")
        return _RUNTIME_CONTEXT


def reload_runtime_context(config_path: str | Path | None = None, active_vault_id: str | None = None) -> WorkspaceContext:
    """Ayarları yeniden okuyup süreç bağlamını değiştir; servisleri etkinleştirme."""
    global _RUNTIME_CONTEXT
    with _CONTEXT_LOCK:
        _RUNTIME_CONTEXT = build_runtime_context(config_path, active_vault_id=active_vault_id)
        return _RUNTIME_CONTEXT


def get_active_vault_context() -> VaultContext:
    return get_runtime_context().active_vault
