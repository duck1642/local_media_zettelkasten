"""Kullanıcıya ait uygulama verilerinin ve varsayılan çalışma alanının yolları."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DATA_ROOT_ENV = "LMZ_DATA_ROOT"


@dataclass(frozen=True)
class AppPaths:
    """Çalışma alanı seçilmeden de kullanılabilen uygulama düzeyi yollar."""

    data_root: Path
    app_dir: Path
    settings_path: Path
    registry_path: Path
    secrets_dir: Path
    logs_dir: Path
    models_dir: Path
    cache_dir: Path
    default_workspace_dir: Path
    default_workspace_config: Path


def resolve_data_root() -> Path:
    """LMZ_DATA_ROOT verilmişse onu, yoksa kullanıcının .lmz dizinini çözümle."""
    override = os.environ.get(DATA_ROOT_ENV, "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".lmz"
    return root.resolve()


def get_app_paths() -> AppPaths:
    """Geçerli veri kökünden uygulama yollarını hesapla."""
    return app_paths_for_root(resolve_data_root())


def app_paths_for_root(data_root: str | Path) -> AppPaths:
    """Verilen kökün standart yollarını hesapla; diskte dizin oluşturma."""
    data_root = Path(data_root).expanduser().resolve()
    app_dir = data_root / "app"
    default_workspace_dir = data_root / "default"
    return AppPaths(
        data_root=data_root,
        app_dir=app_dir,
        settings_path=app_dir / "settings.yaml",
        registry_path=app_dir / "workspaces.yaml",
        secrets_dir=app_dir / "secrets",
        logs_dir=app_dir / "logs",
        models_dir=app_dir / "models",
        cache_dir=app_dir / "cache",
        default_workspace_dir=default_workspace_dir,
        default_workspace_config=default_workspace_dir / "config.yaml",
    )
