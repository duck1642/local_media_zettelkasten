import importlib
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FIXTURE = ROOT / "tests" / "fixtures" / "mock-vault"


def fresh_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *module_names: str):
    work = tmp_path / "mock-vault"
    shutil.copytree(FIXTURE, work)
    legacy = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    (work / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "active_vault": legacy.get("active_vault", "default"),
                "vaults": legacy["vaults"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LMZ_CONFIG_PATH", str(work / "config.yaml"))
    monkeypatch.setenv("LMZ_DATA_ROOT", str(tmp_path / ".lmz"))
    monkeypatch.setenv("LMZ_AUTH_ROOT", str(tmp_path / "app-auth"))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    for name in list(sys.modules):
        if name in {
            "api",
            "utils",
            "runtime_context",
            "runtime_activation",
            "runtime_transitions",
            "queue_service",
            "md_generator",
            "metadata_index",
            "processor",
            "external_ingestion",
            "thumbnails",
            "fingerprint",
            "artists",
            "platforms",
            "review_cache",
            "topics",
            "vaults",
            "workspace_db",
            "ingest_control",
            "workspaces",
        } or name.startswith(("api.", "logger", "db.", "tagging", "downloaders")):
            del sys.modules[name]
    from app_paths import get_app_paths
    from config_repository import bootstrap_data_home

    bootstrap_data_home(get_app_paths())
    return [importlib.import_module(name) for name in module_names]


def test_dynamic_vault_switching(monkeypatch, tmp_path):
    utils, runtime_context, vaults = fresh_backend(
        monkeypatch, tmp_path, "utils", "runtime_context", "vaults"
    )
    work = tmp_path / "mock-vault"

    # 1. Verify initially active vault is "default"
    assert utils.ACTIVE_VAULT_ID == "default"
    assert utils.DB_PATH == work / "data" / "vaults" / "default" / "db" / "lmz_main.db"

    # Ensure default directories exist
    (work / "data" / "vaults" / "default" / "db").mkdir(parents=True, exist_ok=True)
    (work / "data" / "vaults" / "default" / "vault" / "notes").mkdir(parents=True, exist_ok=True)
    (work / "data" / "vaults" / "default" / "wd-tags").mkdir(parents=True, exist_ok=True)

    # 2. Add an "alt" vault to config.yaml
    config_file = work / "config.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config["vaults"]["alt"] = {
        "name": "Alternative Vault",
        "root": "data/vaults/alt",
    }
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")

    # Create alt vault directories
    (work / "data" / "vaults" / "alt" / "db").mkdir(parents=True, exist_ok=True)
    (work / "data" / "vaults" / "alt" / "vault" / "notes").mkdir(parents=True, exist_ok=True)
    (work / "data" / "vaults" / "alt" / "wd-tags").mkdir(parents=True, exist_ok=True)

    # Reload runtime context to pick up manual config change
    runtime_context.reload_runtime_context()

    # 3. Call vaults.set_active_vault("alt")
    res = vaults.set_active_vault("alt")

    # 4. Assert response payload
    assert res["status"] == "success"
    assert res["active"] == "alt"
    assert res["restart_required"] is False

    # 5. Assert utils constants updated dynamically
    assert utils.ACTIVE_VAULT_ID == "alt"
    assert utils.DB_PATH == work / "data" / "vaults" / "alt" / "db" / "lmz_main.db"

    # 6. Verify context paths shifted
    ctx = runtime_context.get_runtime_context()
    assert ctx.active_vault.id == "alt"
    assert ctx.active_vault.db_path == work / "data" / "vaults" / "alt" / "db" / "lmz_main.db"


def test_dynamic_workspace_switching(monkeypatch, tmp_path):
    # Mock registry file path
    mock_registry_yaml = tmp_path / "workspaces.yaml"

    # Import modules
    utils, runtime_context, workspaces, runtime_api = fresh_backend(
        monkeypatch, tmp_path, "utils", "runtime_context", "workspaces", "api.runtime"
    )

    # Clear LMZ_CONFIG_PATH so reload_runtime_context uses active_workspace_config_path()
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)

    # Patch the registry path
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", mock_registry_yaml)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "default",
        "workspaces": {"default": {"name": "Default", "config_path": str(tmp_path / "mock-vault" / "config.yaml")}},
    })

    # Create two workspace config directories and files
    ws1_dir = tmp_path / "workspace_1"
    ws1_dir.mkdir()
    ws1_config = ws1_dir / "config.yaml"

    ws2_dir = tmp_path / "workspace_2"
    ws2_dir.mkdir()
    ws2_config = ws2_dir / "config.yaml"

    default_config = {
        "schema_version": 1,
        "active_vault": "default",
        "vaults": {
            "default": {
                "name": "Default",
                "root": "data/vaults/default",
            }
        },
    }

    ws1_config.write_text(yaml.safe_dump(default_config), encoding="utf-8")
    ws2_config.write_text(yaml.safe_dump(default_config), encoding="utf-8")

    # Create the db/notes subdirs to bypass existence checks
    (ws1_dir / "data" / "vaults" / "default" / "db").mkdir(parents=True, exist_ok=True)
    (ws1_dir / "data" / "vaults" / "default" / "vault" / "notes").mkdir(parents=True, exist_ok=True)
    (ws1_dir / "data" / "vaults" / "default" / "wd-tags").mkdir(parents=True, exist_ok=True)

    (ws2_dir / "data" / "vaults" / "default" / "db").mkdir(parents=True, exist_ok=True)
    (ws2_dir / "data" / "vaults" / "default" / "vault" / "notes").mkdir(parents=True, exist_ok=True)
    (ws2_dir / "data" / "vaults" / "default" / "wd-tags").mkdir(parents=True, exist_ok=True)

    # Register the two workspaces
    workspaces.register_workspace("Workspace 1", ws1_config, workspace_id="ws1", set_active=True)
    workspaces.register_workspace("Workspace 2", ws2_config, workspace_id="ws2", set_active=False)

    # Initialize runtime context to Workspace 1
    runtime_context.reload_runtime_context()

    # Assert initial states
    assert runtime_context.get_runtime_context().config_path == ws1_config
    assert utils.DB_PATH == ws1_dir / "data" / "vaults" / "default" / "db" / "lmz_main.db"
    workspace_1_auth_path = utils.platform_cookie_path("x")

    # Switch workspace to Workspace 2 dynamically
    res = runtime_api._set_workspace_active_sync({"id": "ws2"})

    # Assert return payload
    assert res["status"] == "success"
    assert res["active"] == "ws2"
    assert res["restart_required"] is False

    # Assert runtime context and utils updated dynamically to Workspace 2!
    assert runtime_context.get_runtime_context().config_path == ws2_config
    assert utils.DB_PATH == ws2_dir / "data" / "vaults" / "default" / "db" / "lmz_main.db"
    assert utils.platform_cookie_path("x") == workspace_1_auth_path == tmp_path / "app-auth" / "x" / "cookies.txt"


def test_vault_list_uses_current_runtime_workspace_after_switch(monkeypatch, tmp_path):
    mock_registry_yaml = tmp_path / "workspaces.yaml"
    utils, runtime_context, workspaces, vaults, runtime_api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "runtime_context",
        "workspaces",
        "vaults",
        "api.runtime",
    )
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", mock_registry_yaml)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "default",
        "workspaces": {"default": {"name": "Default", "config_path": str(tmp_path / "mock-vault" / "config.yaml")}},
    })

    ws1_dir = tmp_path / "workspace_1"
    ws2_dir = tmp_path / "workspace_2"
    ws1_dir.mkdir()
    ws2_dir.mkdir()
    ws1_config = ws1_dir / "config.yaml"
    ws2_config = ws2_dir / "config.yaml"
    ws1_config.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "active_vault": "default",
            "vaults": {"default": {"name": "Default", "root": "data/vaults/default"}},
        }),
        encoding="utf-8",
    )
    ws2_config.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "active_vault": "default",
            "vaults": {
                "default": {"name": "Default", "root": "data/vaults/default"},
                "second": {"name": "Second", "root": "data/vaults/second"},
            },
        }),
        encoding="utf-8",
    )
    for root in (
        ws1_dir / "data" / "vaults" / "default",
        ws2_dir / "data" / "vaults" / "default",
        ws2_dir / "data" / "vaults" / "second",
    ):
        (root / "db").mkdir(parents=True, exist_ok=True)
        (root / "vault" / "notes").mkdir(parents=True, exist_ok=True)
        (root / "wd-tags").mkdir(parents=True, exist_ok=True)

    workspaces.register_workspace("Workspace 1", ws1_config, workspace_id="ws1", set_active=True)
    workspaces.register_workspace("Workspace 2", ws2_config, workspace_id="ws2", set_active=False)
    runtime_context.reload_runtime_context()

    assert [item["id"] for item in vaults.vault_list()] == ["default"]

    runtime_api._set_workspace_active_sync({"id": "ws2"})

    listed = vaults.vault_list()
    assert [item["id"] for item in listed] == ["default", "second"]
    assert {Path(item["root"]).parent.parent for item in listed} == {ws2_dir / "data"}
    assert runtime_api._get_vaults_sync()["active"] == "default"
