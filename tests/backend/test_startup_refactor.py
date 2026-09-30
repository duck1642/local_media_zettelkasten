import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def fresh_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)
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
            "workspaces",
            "queue_service",
            "md_generator",
            "metadata_index",
            "metadata_maintenance",
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
        } or name.startswith(("api.", "logger", "db.", "tagging", "downloaders")):
            del sys.modules[name]
    app_module = importlib.import_module("api.app")
    common = importlib.import_module("api.common")
    from app_paths import get_app_paths
    from config_repository import bootstrap_data_home

    bootstrap_data_home(get_app_paths())
    monkeypatch.setattr(common, "_api_key_path", lambda: tmp_path / "secrets" / ".api_key")
    return app_module


def write_registry(path: Path, active: str, workspaces: dict):
    for entry in workspaces.values():
        if entry.get("config_path") == "config/config.yaml":
            entry["config_path"] = "default/config.yaml"
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "active_workspace": active, "workspaces": workspaces}, sort_keys=False),
        encoding="utf-8",
    )


def workspace_config(path: Path, vault_root: str = "data/vaults/default"):
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "active_vault": "default",
                "vaults": {"default": {"name": "Default", "root": vault_root}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def api_key(client: TestClient) -> str:
    response = client.get("/api/session-key")
    assert response.status_code == 200
    return response.json()["key"]


def patch_runtime_services(monkeypatch: pytest.MonkeyPatch):
    metadata_index = importlib.import_module("metadata_index")
    search_manager_module = importlib.import_module("db.search_manager")
    monkeypatch.setattr(metadata_index, "restart_metadata_watchdog", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata_index, "start_metadata_repair_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(search_manager_module.search_manager, "reset_all", lambda *args, **kwargs: None)
    monkeypatch.setattr(search_manager_module.search_manager, "hydrate", lambda *args, **kwargs: None)


def _prepare_workspace_switch_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app_module = fresh_api(monkeypatch, tmp_path)
    from app_paths import get_app_paths

    runtime_context = importlib.import_module("runtime_context")
    runtime_api = importlib.import_module("api.runtime")
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"

    ready_root = tmp_path / "ready-workspace"
    ready_root.mkdir()
    ready_config = ready_root / "config.yaml"
    workspace_config(ready_config)
    (ready_root / "data" / "vaults" / "default").mkdir(parents=True)

    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "ready": {"name": "Ready", "config_path": str(ready_config)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    patch_runtime_services(monkeypatch)

    client = TestClient(app_module.app)
    key = api_key(client)
    loaded = client.post("/api/workspaces/default/load", headers={"X-LMZ-API-KEY": key})
    assert loaded.status_code == 200
    assert loaded.json()["status"] == "success"
    assert runtime_context.get_runtime_context().config_path == get_app_paths().default_workspace_config
    return client, key, runtime_api, runtime_context, workspaces, ready_config


def test_direct_and_active_workspace_loads_share_preflight(monkeypatch, tmp_path):
    client, key, runtime_api, runtime_context, workspaces, ready_config = _prepare_workspace_switch_fixture(monkeypatch, tmp_path)
    runtime_transitions = importlib.import_module("runtime_transitions")
    vaults = importlib.import_module("vaults")
    vaults.create_vault("Delete Target")
    previous_ctx = runtime_context.get_runtime_context()
    sentinel = str(tmp_path / "override-config.yaml")
    monkeypatch.setenv("LMZ_CONFIG_PATH", sentinel)
    monkeypatch.setattr(
        runtime_transitions,
        "runtime_switch_preflight",
        lambda *args, **kwargs: {"allowed": False, "blockers": ["test_switch_blocked"]},
    )

    direct = client.post("/api/workspaces/ready/load", headers={"X-LMZ-API-KEY": key})
    active = client.post(
        "/api/workspaces/active",
        json={"id": "ready"},
        headers={"X-LMZ-API-KEY": key},
    )
    vault = client.post(
        "/api/vaults/active",
        json={"id": "default"},
        headers={"X-LMZ-API-KEY": key},
    )
    create_vault = client.post(
        "/api/vaults",
        json={"name": "Blocked Vault"},
        headers={"X-LMZ-API-KEY": key},
    )
    delete_vault = client.delete(
        "/api/vaults/delete-target?confirm=true",
        headers={"X-LMZ-API-KEY": key},
    )
    delete_workspace = client.delete(
        "/api/workspaces/ready?mode=unregister",
        headers={"X-LMZ-API-KEY": key},
    )

    nested_blocked_detail = {
        "detail": {
            "detail": "Runtime switch blocked",
            "blockers": ["test_switch_blocked"],
        }
    }
    flat_blocked_detail = {
        "detail": "Runtime switch blocked",
        "blockers": ["test_switch_blocked"],
    }
    assert direct.status_code == 409
    assert active.status_code == 409
    assert vault.status_code == 409
    assert direct.json() == nested_blocked_detail
    assert active.json() == nested_blocked_detail
    assert vault.json() == nested_blocked_detail
    assert create_vault.status_code == 409
    assert create_vault.json() == vault.json()
    assert delete_vault.status_code == 409
    assert delete_vault.json() == nested_blocked_detail
    assert delete_workspace.status_code == 409
    assert delete_workspace.json() == flat_blocked_detail
    assert runtime_context.get_runtime_context() == previous_ctx
    assert workspaces.load_workspace_registry()["active_workspace"] == "default"
    assert os.environ["LMZ_CONFIG_PATH"] == sentinel
    assert ready_config.exists()


def test_candidate_hydration_failure_restores_previous_context_registry_and_env(monkeypatch, tmp_path):
    client, key, runtime_api, runtime_context, workspaces, ready_config = _prepare_workspace_switch_fixture(monkeypatch, tmp_path)
    search_manager_module = importlib.import_module("db.search_manager")
    original_hydrate = search_manager_module.search_manager.hydrate
    previous_ctx = runtime_context.get_runtime_context()
    sentinel = str(tmp_path / "override-config.yaml")
    monkeypatch.setenv("LMZ_CONFIG_PATH", sentinel)

    def fail_candidate_hydration(conn):
        current = runtime_context.try_get_runtime_context()
        if current is not None and current.config_path == ready_config:
            raise RuntimeError("candidate hydration failed")
        return original_hydrate(conn)

    monkeypatch.setattr(search_manager_module.search_manager, "hydrate", fail_candidate_hydration)
    response = client.post("/api/workspaces/ready/load", headers={"X-LMZ-API-KEY": key})

    assert response.status_code == 500
    assert runtime_context.get_runtime_context() == previous_ctx
    assert workspaces.load_workspace_registry()["active_workspace"] == "default"
    assert os.environ["LMZ_CONFIG_PATH"] == sentinel


def test_service_activation_failure_restores_previous_runtime(monkeypatch, tmp_path):
    client, key, runtime_api, runtime_context, workspaces, ready_config = _prepare_workspace_switch_fixture(monkeypatch, tmp_path)
    metadata_index = importlib.import_module("metadata_index")
    original_restart = metadata_index.restart_metadata_watchdog
    previous_ctx = runtime_context.get_runtime_context()

    def fail_candidate_watchdog(ctx):
        if ctx.config_path == ready_config:
            raise RuntimeError("candidate watchdog activation failed")
        return original_restart(ctx)

    monkeypatch.setattr(metadata_index, "restart_metadata_watchdog", fail_candidate_watchdog)
    response = client.post("/api/workspaces/ready/load", headers={"X-LMZ-API-KEY": key})

    assert response.status_code == 500
    assert runtime_context.get_runtime_context() == previous_ctx
    assert workspaces.load_workspace_registry()["active_workspace"] == "default"


def test_registry_commit_failure_restores_registry_env_and_rehydrates_previous_services(monkeypatch, tmp_path):
    client, key, runtime_api, runtime_context, workspaces, ready_config = _prepare_workspace_switch_fixture(monkeypatch, tmp_path)
    original_save = workspaces.save_workspace_registry
    original_activate = runtime_api.activate_runtime_context
    runtime_activation = importlib.import_module("runtime_activation")
    previous_ctx = runtime_context.get_runtime_context()
    sentinel = str(tmp_path / "override-config.yaml")
    calls = []
    monkeypatch.setenv("LMZ_CONFIG_PATH", sentinel)

    def fail_candidate_registry_save(registry):
        if registry.get("active_workspace") == "ready":
            raise OSError("registry write failed")
        return original_save(registry)

    def record_activation(ctx, *args, **kwargs):
        calls.append((ctx.config_path, kwargs.get("hydrate", True)))
        return original_activate(ctx, *args, **kwargs)

    monkeypatch.setattr(workspaces, "save_workspace_registry", fail_candidate_registry_save)
    monkeypatch.setattr(runtime_api, "activate_runtime_context", record_activation)
    monkeypatch.setattr(runtime_activation, "activate_runtime_context", record_activation)
    response = client.post("/api/workspaces/ready/load", headers={"X-LMZ-API-KEY": key})

    assert response.status_code == 500
    assert runtime_context.get_runtime_context() == previous_ctx
    assert workspaces.load_workspace_registry()["active_workspace"] == "default"
    assert os.environ["LMZ_CONFIG_PATH"] == sentinel
    assert (previous_ctx.config_path, True) in calls


def test_workspace_switches_are_serialized_by_process_wide_lock(monkeypatch, tmp_path):
    client, key, runtime_api, runtime_context, workspaces, ready_config = _prepare_workspace_switch_fixture(monkeypatch, tmp_path)
    other_root = tmp_path / "other-workspace"
    other_root.mkdir()
    other_config = other_root / "config.yaml"
    workspace_config(other_config)
    (other_root / "data" / "vaults" / "default").mkdir(parents=True)
    registry = workspaces.load_workspace_registry()
    registry["workspaces"]["other"] = {"name": "Other", "config_path": str(other_config)}
    workspaces.save_workspace_registry(registry)

    original_activate = runtime_api.activate_runtime_context
    entered = threading.Event()
    release = threading.Event()
    call_count = 0
    call_guard = threading.Lock()

    def block_first_activation(ctx, *args, **kwargs):
        nonlocal call_count
        with call_guard:
            call_count += 1
            first = call_count == 1
        if first:
            entered.set()
            assert release.wait(5), "first workspace switch did not release"
        return original_activate(ctx, *args, **kwargs)

    monkeypatch.setattr(runtime_api, "activate_runtime_context", block_first_activation)
    results = {}

    first = threading.Thread(target=lambda: results.setdefault("first", runtime_api._load_workspace_sync("ready")))
    second = threading.Thread(target=lambda: results.setdefault("second", runtime_api._load_workspace_sync("other")))
    first.start()
    assert entered.wait(5), "first switch did not enter activation"
    second.start()
    time.sleep(0.2)
    assert "second" not in results
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results["first"]["status"] == "success"
    assert results["second"]["status"] == "success"
    assert workspaces.load_workspace_registry()["active_workspace"] == "other"
    assert runtime_context.get_runtime_context().config_path == other_config


def test_launcher_mode_serves_recovery_routes_without_workspace(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    runtime_context = importlib.import_module("runtime_context")
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    missing_config = tmp_path / "missing" / "config.yaml"
    write_registry(
        registry_path,
        "missing",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "missing": {"name": "Missing", "config_path": str(missing_config)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)

    client = TestClient(app_module.app)
    assert runtime_context.has_runtime_context() is False
    assert client.get("/").status_code == 200

    settings_response = client.get("/api/app/settings")
    assert settings_response.status_code == 200
    assert client.get("/api/runtime/session").json() == {"loaded": False}

    workspace_response = client.get("/api/workspaces")
    assert workspace_response.status_code == 200
    missing = [item for item in workspace_response.json()["items"] if item["id"] == "missing"][0]
    assert missing["exists"] is False

    logs_response = client.get("/api/logs/location")
    assert logs_response.status_code == 200
    assert logs_response.json()["mode"] == "startup"

    ui_log_response = client.post(
        "/api/logs/ui",
        json={"level": "INFO", "message": "launcher log probe", "extra": {}},
        headers={"X-LMZ-API-KEY": api_key(client)},
    )
    assert ui_log_response.status_code == 200
    assert ui_log_response.json()["status"] == "ok"


def test_launcher_mode_creates_lmz_workspace(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    write_registry(
        registry_path,
        "default",
        {"default": {"name": "Default", "config_path": "config/config.yaml"}},
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)

    client = TestClient(app_module.app)
    key = api_key(client)
    parent = (Path(tempfile.gettempdir()) / f"lmz-api-workspace-{time.time_ns()}").resolve()

    try:
        response = client.post(
            "/api/workspaces",
            json={"path": str(parent), "name": "API Workspace"},
            headers={"X-LMZ-API-KEY": key},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "success"
        config_path = parent / "lmz" / "config.yaml"
        assert Path(payload["workspace"]["config_path"]) == config_path
        saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved_config["vaults"]["default"]["root"] == "data/vaults/default"
        assert "paths" not in saved_config
        assert set(saved_config) == {"schema_version", "active_vault", "vaults"}
        assert Path(payload["workspace"]["marker_path"]).exists()
        assert payload["workspace"]["managed"] is True
        assert not (parent / "lmz" / "data" / "models").exists()
        assert not (parent / "lmz" / "data" / "secrets").exists()
        load = client.post("/api/workspaces/api-workspace/load", headers={"X-LMZ-API-KEY": key})
        assert load.status_code == 200
        assert load.json()["status"] == "success"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_workspace_delete_api_uses_explicit_full_mode(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    write_registry(
        registry_path,
        "default",
        {"default": {"name": "Default", "config_path": "config/config.yaml"}},
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    client = TestClient(app_module.app)
    key = api_key(client)
    parent = (Path(tempfile.gettempdir()) / f"lmz-api-delete-{time.time_ns()}").resolve()

    try:
        loaded = client.post("/api/workspaces/default/load", headers={"X-LMZ-API-KEY": key})
        assert loaded.status_code == 200
        created = client.post(
            "/api/workspaces",
            json={"path": str(parent), "name": "Delete Me"},
            headers={"X-LMZ-API-KEY": key},
        )
        assert created.status_code == 200
        workspace_id = next(item["id"] for item in created.json()["items"] if item["name"] == "Delete Me")

        deleted = client.delete(
            f"/api/workspaces/{workspace_id}?mode=all",
            headers={"X-LMZ-API-KEY": key},
        )

        assert deleted.status_code == 200
        assert deleted.json()["mode"] == "all"
        assert deleted.json()["cleanup_status"] == "complete"
        assert not (parent / "lmz").exists()
    finally:
        shutil.rmtree(parent, ignore_errors=True)



def test_missing_workspace_config_load_does_not_persist_active(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    missing_config = tmp_path / "offline" / "config.yaml"
    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "offline": {"name": "Offline", "config_path": str(missing_config)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/workspaces/offline/load",
        headers={"X-LMZ-API-KEY": api_key(client)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "relocate_workspace"
    assert workspaces.load_workspace_registry()["active_workspace"] == "default"


def test_missing_vault_root_load_enables_vault_relocation(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    runtime_context = importlib.import_module("runtime_context")
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    config_path = ws_root / "config.yaml"
    workspace_config(config_path)
    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "offline-vault": {"name": "Offline Vault", "config_path": str(config_path)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/workspaces/offline-vault/load",
        headers={"X-LMZ-API-KEY": api_key(client)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "relocate_vault"
    assert runtime_context.has_runtime_context() is True
    vaults_response = client.get("/api/vaults")
    assert vaults_response.status_code == 200
    assert vaults_response.json()["items"][0]["exists"] is False
    assert client.get("/api/logs/location").json()["mode"] == "startup"
    # Recovery mode is a committed workspace transition: the runtime context and
    # registry must agree even while the active vault path is offline.
    assert workspaces.load_workspace_registry()["active_workspace"] == "offline-vault"


def test_fresh_clone_default_workspace_initializes_missing_data(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    runtime_context = importlib.import_module("runtime_context")
    workspaces = importlib.import_module("workspaces")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    workspace_config(config_path)
    registry_path = config_dir / "workspaces.yaml"
    write_registry(
        registry_path,
        "default",
        {"default": {"name": "Default", "config_path": str(config_path)}},
    )
    monkeypatch.setattr(workspaces, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(runtime_context, "PROJECT_ROOT", tmp_path)
    patch_runtime_services(monkeypatch)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/workspaces/default/load",
        headers={"X-LMZ-API-KEY": api_key(client)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert (config_dir / "data" / "vaults" / "default" / "vault" / "assets").is_dir()
    assert (config_dir / "data" / "vaults" / "default" / "db" / "lmz_main.db").is_file()


def test_valid_workspace_load_persists_active_after_services_start(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    ws_root = tmp_path / "workspace"
    vault_root = ws_root / "data" / "vaults" / "default"
    (vault_root / "vault" / "assets").mkdir(parents=True)
    config_path = ws_root / "config.yaml"
    workspace_config(config_path)
    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "ready": {"name": "Ready", "config_path": str(config_path)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    patch_runtime_services(monkeypatch)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/workspaces/ready/load",
        headers={"X-LMZ-API-KEY": api_key(client)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["active_workspace"] == "ready"
    assert response.json()["active_vault"] == "default"
    assert client.get("/api/logs/location").json()["mode"] == "vault"
    assert workspaces.load_workspace_registry()["active_workspace"] == "ready"


def test_vault_relocation_activates_runtime_and_vault_logs(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    config_path = ws_root / "config.yaml"
    workspace_config(config_path)
    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "offline-vault": {"name": "Offline Vault", "config_path": str(config_path)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    patch_runtime_services(monkeypatch)

    client = TestClient(app_module.app)
    key = api_key(client)
    response = client.post("/api/workspaces/offline-vault/load", headers={"X-LMZ-API-KEY": key})
    assert response.json()["status"] == "relocate_vault"

    vault_root = ws_root / "data" / "vaults" / "default"
    vault_root.mkdir(parents=True)
    relocate = client.post(
        "/api/vaults/relocate",
        json={"vault_id": "default", "new_vault_root": str(vault_root)},
        headers={"X-LMZ-API-KEY": key},
    )

    assert relocate.status_code == 200
    assert relocate.json()["status"] == "success"
    saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved_config["vaults"]["default"]["root"] == "data/vaults/default"
    location = client.get("/api/logs/location").json()
    assert location["mode"] == "vault"
    assert location["vault"] == "default"


def test_vault_relocation_rejects_outside_workspace(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    config_path = ws_root / "config.yaml"
    workspace_config(config_path)
    vault_root = ws_root / "data" / "vaults" / "default"
    vault_root.mkdir(parents=True)
    outside_root = tmp_path / "outside-vault"
    outside_root.mkdir()
    write_registry(
        registry_path,
        "default",
        {
            "default": {"name": "Default", "config_path": "config/config.yaml"},
            "ready": {"name": "Ready", "config_path": str(config_path)},
        },
    )
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    patch_runtime_services(monkeypatch)

    client = TestClient(app_module.app)
    key = api_key(client)
    response = client.post("/api/workspaces/ready/load", headers={"X-LMZ-API-KEY": key})
    assert response.json()["status"] == "success"

    relocate = client.post(
        "/api/vaults/relocate",
        json={"vault_id": "default", "new_vault_root": str(outside_root)},
        headers={"X-LMZ-API-KEY": key},
    )

    assert relocate.status_code == 400
    saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved_config["vaults"]["default"]["root"] == "data/vaults/default"


def test_sidecar_health_requires_and_echoes_launch_nonce(monkeypatch, tmp_path):
    app_module = fresh_api(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        monkeypatch.delenv("LMZ_STARTUP_NONCE", raising=False)
        not_ready = client.get("/api/runtime/health")
        assert not_ready.status_code == 200
        assert not_ready.json() == {
            "service": "lmz-api",
            "ready": False,
            "protocol_version": 1,
            "nonce": "",
        }

        monkeypatch.setenv("LMZ_STARTUP_NONCE", "test-launch-nonce")
        ready = client.get("/api/runtime/health")
        assert ready.status_code == 200
        assert ready.json() == {
            "service": "lmz-api",
            "ready": True,
            "protocol_version": 1,
            "nonce": "test-launch-nonce",
        }
