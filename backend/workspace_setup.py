import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from config_repository import WorkspaceConfigRepository
from config_schema import default_workspace_config
from vault_layout import ensure_vault_directories
from workspaces import WORKSPACE_MARKER_NAME, WORKSPACE_MARKER_PAYLOAD


if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {
    PROJECT_ROOT,
    PROJECT_ROOT / "data",
    PROJECT_ROOT / "config",
    PROJECT_ROOT / "logs",
    PROJECT_ROOT / "secrets",
}


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _guard_workspace_parent_path(parent_path: Path):
    resolved = _resolve(parent_path)
    forbidden = {_resolve(path) for path in FORBIDDEN}
    if resolved in forbidden:
        raise ValueError(f"refusing unsafe workspace parent path: {resolved}")
    for path in forbidden:
        if _is_relative_to(resolved, path):
            raise ValueError(f"refusing workspace parent inside runtime path: {resolved}")


def lmz_workspace_config() -> dict:
    return default_workspace_config().model_dump(mode="json")


def setup_lmz_workspace(parent_path: str | Path, overwrite_config: bool = False) -> dict:
    workspace_parent = _resolve(parent_path)
    _guard_workspace_parent_path(workspace_parent)
    workspace = workspace_parent / "lmz"
    workspace_preexisted = workspace.exists()
    config_path = workspace / "config.yaml"
    topics_dir = workspace / "data" / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    directories = [topics_dir, *ensure_vault_directories(workspace / "data" / "vaults" / "default")]
    wrote_config = False
    repository = WorkspaceConfigRepository(config_path)
    if overwrite_config and config_path.exists():
        current = repository.read()
        repository.replace(default_workspace_config(), expected_etag=current.etag)
        wrote_config = True
    elif not config_path.exists():
        repository.create(default_workspace_config())
        wrote_config = True
    else:
        repository.read()
    marker_path = workspace / WORKSPACE_MARKER_NAME
    if not workspace_preexisted:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=workspace,
                prefix=f".{WORKSPACE_MARKER_NAME}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(WORKSPACE_MARKER_PAYLOAD, handle)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, marker_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
    return {
        "workspace_parent": str(workspace_parent),
        "workspace": str(workspace),
        "config_path": str(config_path),
        "wrote_config": wrote_config,
        "marker_path": str(marker_path),
        "managed": marker_path.exists(),
        "directories": [str(path) for path in directories],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create an LMZ workspace under a parent folder.")
    parser.add_argument("workspace_parent_path")
    parser.add_argument("--overwrite-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = setup_lmz_workspace(args.workspace_parent_path, overwrite_config=args.overwrite_config)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"ERROR {exc}", file=sys.stderr)
        return 2
    payload["ok"] = True
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload["config_path"])
        print(f"PowerShell: $env:LMZ_CONFIG_PATH=\"{payload['config_path']}\"")
    return 0
