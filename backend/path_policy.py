"""Kasa köklerini ve çalışma alanına göre yazılan yapılandırma yollarını doğrular."""

from pathlib import Path


def is_relative_to(path: Path, root: Path) -> bool:
    """Bağlantılar çözüldükten sonra yolun kökün içinde veya kökün kendisi olduğunu denetle."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def workspace_relative_path(path: str | Path, workspace_root: Path, *, label: str, allow_equal: bool = False) -> str:
    """Yolu yapılandırmada saklanabilecek göreli biçime çevir; dışarı çıkarsa hata ver."""
    resolved = Path(path).expanduser().resolve()
    root = workspace_root.resolve()
    if not is_relative_to(resolved, root):
        raise ValueError(f"{label} must be inside workspace root: {resolved}")
    if not allow_equal and resolved == root:
        raise ValueError(f"{label} cannot be the workspace root")
    return resolved.relative_to(root).as_posix()


def _resolve_config_value(value: str | Path, workspace_root: Path) -> Path:
    raw = Path(value)
    return raw.resolve() if raw.is_absolute() else (workspace_root / raw).resolve()


def _validate_relative_value(value: object, workspace_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a relative path string")
    raw = Path(value)
    if raw.is_absolute():
        raise ValueError(f"{label} must be relative to workspace root")
    resolved = _resolve_config_value(raw, workspace_root)
    if not is_relative_to(resolved, workspace_root):
        raise ValueError(f"{label} cannot escape workspace root")
    return resolved


def validate_vault_root_value(value: object, workspace_root: Path, label: str) -> Path:
    """Göreli kasa kökünü çözümle; çalışma alanının kendisini ve ayrılmış dizinleri reddet."""
    resolved = _validate_relative_value(value, workspace_root, label)
    root = workspace_root.resolve()
    if resolved == root:
        raise ValueError(f"{label} cannot be the workspace root")
    reserved = {
        root / "config",
        root / "secrets",
        root / "models",
        root / "data" / "secrets",
        root / "data" / "models",
    }
    if resolved in reserved:
        raise ValueError(f"{label} cannot use reserved workspace directory: {resolved}")
    return resolved


def validate_workspace_config_paths(config: dict, workspace_root: Path):
    """Kasa yollarını ve artık desteklenmeyen model/gizli veri ayarlarını denetle."""
    paths = config.get("paths")
    if isinstance(paths, dict):
        if "models" in paths:
            raise ValueError("paths.models is no longer supported; models are stored in app data/models")
        if "secrets" in paths:
            raise ValueError("paths.secrets is no longer supported; authentication is app-scoped")

    vaults = config.get("vaults")
    if isinstance(vaults, dict):
        for vault_id, entry in vaults.items():
            if isinstance(entry, dict):
                validate_vault_root_value(entry.get("root") or "data/vaults/default", workspace_root, f"vaults.{vault_id}.root")


def vault_root_is_usable(root: Path, workspace_root: Path) -> bool:
    """Kökün yol kurallarına uyduğunu ve diskte bulunduğunu denetle."""
    if not vault_root_is_inside_workspace(root, workspace_root):
        return False
    return root.exists()


def vault_root_is_inside_workspace(root: Path, workspace_root: Path) -> bool:
    """Kökün varlığını değil, çalışma alanı içindeki geçerli konumunu denetle."""
    try:
        validate_vault_root_value(workspace_relative_path(root, workspace_root, label="vault root"), workspace_root, "vault root")
    except ValueError:
        return False
    return True
