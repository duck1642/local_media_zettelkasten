import sys
import importlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_app_paths_use_lmz_data_root_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    data_root = tmp_path / "isolated-lmz"
    monkeypatch.setenv("LMZ_DATA_ROOT", str(data_root))

    from app_paths import get_app_paths

    paths = get_app_paths()

    assert paths.data_root == data_root.resolve()
    assert paths.settings_path == data_root / "app" / "settings.yaml"
    assert paths.registry_path == data_root / "app" / "workspaces.yaml"
    assert paths.models_dir == data_root / "app" / "models"
    assert paths.default_workspace_dir == data_root / "default"
    assert paths.default_workspace_config == data_root / "default" / "config.yaml"


def test_strict_schemas_reject_unknown_and_mixed_scope_fields():
    from config_schema import AppSettings, WorkspaceConfig, WorkspaceRegistry

    with pytest.raises(ValidationError):
        AppSettings.model_validate({"schema_version": 1, "mystery": True})

    with pytest.raises(ValidationError):
        WorkspaceConfig.model_validate(
            {
                "schema_version": 1,
                "active_vault": "default",
                "vaults": {
                    "default": {
                        "name": "Default",
                        "root": "data/vaults/default",
                    }
                },
                "tagging": {"enabled": True},
            }
        )

    with pytest.raises(ValidationError):
        WorkspaceConfig.model_validate(
            {
                "schema_version": 1,
                "active_vault": "default",
                "vaults": {
                    "default": {
                        "name": "Default",
                        "root": "data/vaults/default",
                    }
                },
                "storage": {"hash_algorithm": "sha256"},
            }
        )

    with pytest.raises(ValidationError):
        WorkspaceRegistry.model_validate(
            {
                "schema_version": 1,
                "active_workspace": "default",
                "workspaces": {"default": {"name": "Default", "config_path": "../outside/config.yaml"}},
            }
        )

    with pytest.raises(ValidationError):
        WorkspaceConfig.model_validate(
            {
                "schema_version": 1,
                "active_vault": "default",
                "vaults": {"default": {"name": "Default", "root": "."}},
            }
        )


def test_defaults_are_complete_and_keep_hash_algorithm_out_of_yaml():
    from config_schema import default_app_settings, default_workspace_config

    settings = default_app_settings()
    workspace = default_workspace_config()

    assert settings.schema_version == 1
    assert settings.ingestion.accepted_media.extensions
    assert settings.ingestion.accepted_media.mime_types
    assert settings.tagging.video.frame_count == 5
    assert settings.webview.devtools_enabled is False
    assert "hash_algorithm" not in settings.model_dump_json()
    assert workspace.vaults["default"].root == "data/vaults/default"


def test_downloader_media_filter_uses_app_settings_schema(monkeypatch, tmp_path: Path):
    from config_schema import default_app_settings
    from downloaders import media_filter

    image = tmp_path / "accepted.jpg"
    ignored = tmp_path / "ignored.gif"
    image.write_bytes(b"image")
    ignored.write_bytes(b"gif")
    monkeypatch.setattr(media_filter, "get_mime_type", lambda path: "image/jpeg")

    config = default_app_settings().model_dump(mode="json")
    assert media_filter.valid_media_files(tmp_path, config) == [image]


def test_repository_rejects_malformed_yaml_without_replacing_it(tmp_path: Path):
    from config_repository import ConfigReadError, SettingsRepository

    path = tmp_path / "settings.yaml"
    original = "schema_version: [\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigReadError):
        SettingsRepository(path).read()

    assert path.read_text(encoding="utf-8") == original


def test_settings_repository_requires_current_etag(tmp_path: Path):
    from config_repository import SettingsConflictError, SettingsRepository
    from config_schema import default_app_settings

    path = tmp_path / "settings.yaml"
    repository = SettingsRepository(path)
    repository.create(default_app_settings())
    first = repository.read()

    changed = first.value.model_copy(deep=True)
    changed.logging.level = "DEBUG"
    second = repository.replace(changed, expected_etag=first.etag)
    assert second.value.logging.level == "DEBUG"
    assert path.with_suffix(".yaml.bak").is_file()

    stale_change = first.value.model_copy(deep=True)
    stale_change.logging.level = "ERROR"
    with pytest.raises(SettingsConflictError):
        repository.replace(stale_change, expected_etag=first.etag)

    assert repository.read().value.logging.level == "DEBUG"
    assert path.with_name(".settings.yaml.lock").stat().st_size == 1


def test_bootstrap_creates_canonical_data_home_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    data_root = tmp_path / ".lmz"
    monkeypatch.setenv("LMZ_DATA_ROOT", str(data_root))

    from app_paths import get_app_paths
    from config_repository import SettingsRepository, bootstrap_data_home

    paths = get_app_paths()
    bootstrap_data_home(paths)

    assert paths.settings_path.is_file()
    assert paths.registry_path.is_file()
    assert paths.default_workspace_config.is_file()
    assert paths.secrets_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.models_dir.is_dir()
    assert paths.cache_dir.is_dir()
    assert (paths.default_workspace_dir / "data").is_dir()
    assert (paths.default_workspace_dir / "data" / "vaults" / "default" / "vault" / "assets").is_dir()
    assert (paths.default_workspace_dir / "data" / "vaults" / "default" / "db").is_dir()
    assert (paths.default_workspace_dir / "data" / "vaults" / "default" / "logs" / "structured").is_dir()
    assert (paths.default_workspace_dir / "backups").is_dir()
    assert (paths.default_workspace_dir / "exports").is_dir()

    registry = yaml.safe_load(paths.registry_path.read_text(encoding="utf-8"))
    assert registry["active_workspace"] == "default"
    assert registry["workspaces"]["default"]["config_path"] == "default/config.yaml"

    repository = SettingsRepository(paths.settings_path)
    current = repository.read()
    edited = current.value.model_copy(deep=True)
    edited.ui.privacy_blur = False
    repository.replace(edited, expected_etag=current.etag)

    bootstrap_data_home(paths)
    assert repository.read().value.ui.privacy_blur is False


def test_bootstrap_rejects_missing_config_in_existing_data_home(monkeypatch, tmp_path: Path):
    data_root = tmp_path / ".lmz"
    monkeypatch.setenv("LMZ_DATA_ROOT", str(data_root))

    from app_paths import get_app_paths
    from config_repository import ConfigReadError, bootstrap_data_home

    paths = get_app_paths()
    bootstrap_data_home(paths)
    paths.settings_path.unlink()

    with pytest.raises(ConfigReadError, match="does not exist"):
        bootstrap_data_home(paths)

    assert not paths.settings_path.exists()


def _fresh_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("LMZ_DATA_ROOT", str(tmp_path / ".lmz"))
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)
    for name in list(sys.modules):
        if (
            name in {"workspaces", "runtime_context", "runtime_activation"}
            or name == "api"
            or name.startswith(("api.", "logger"))
        ):
            del sys.modules[name]
    app_module = importlib.import_module("api.app")
    common = importlib.import_module("api.common")
    monkeypatch.setattr(common, "_api_key_path", lambda: tmp_path / "auth" / ".api_key")
    return app_module.app


def test_app_settings_api_is_available_without_runtime_and_rejects_stale_put(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        session_key = client.get("/api/session-key").json()["key"]
        initial = client.get(
            "/api/app/settings",
            headers={"Origin": "http://tauri.localhost"},
        )
        assert initial.status_code == 200
        assert initial.headers["etag"]
        exposed_headers = {
            value.strip().lower()
            for value in initial.headers.get("access-control-expose-headers", "").split(",")
        }
        assert "etag" in exposed_headers

        body = initial.json()
        body["ui"]["privacy_blur"] = True
        updated = client.put(
            "/api/app/settings",
            headers={
                "If-Match": initial.headers["etag"],
                "X-LMZ-API-KEY": session_key,
            },
            json=body,
        )
        assert updated.status_code == 200
        assert updated.json()["ui"]["privacy_blur"] is True
        assert updated.headers["etag"] != initial.headers["etag"]

        stale = client.put(
            "/api/app/settings",
            headers={
                "If-Match": initial.headers["etag"],
                "X-LMZ-API-KEY": session_key,
            },
            json=body,
        )
        assert stale.status_code == 412


def test_runtime_session_reports_launcher_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/runtime/session")
    assert response.status_code == 200
    assert response.json() == {"loaded": False}


def test_runtime_context_uses_strict_workspace_config_and_app_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    data_root = tmp_path / ".lmz"
    monkeypatch.setenv("LMZ_DATA_ROOT", str(data_root))
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)

    from app_paths import get_app_paths
    from config_repository import ConfigReadError, bootstrap_data_home
    from runtime_context import build_runtime_context

    paths = get_app_paths()
    bootstrap_data_home(paths)
    context = build_runtime_context(paths.default_workspace_config)

    assert context.root == paths.default_workspace_dir
    assert context.models_dir == paths.models_dir
    assert context.active_vault.root == paths.default_workspace_dir / "data" / "vaults" / "default"

    paths.default_workspace_config.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "active_vault": "default",
                "vaults": {"default": {"name": "Default", "root": "data/vaults/default"}},
                "ui": {"privacy_blur": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigReadError):
        build_runtime_context(paths.default_workspace_config)


def test_workspace_registry_resolves_relative_paths_from_data_root_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    data_root = tmp_path / ".lmz"
    monkeypatch.setenv("LMZ_DATA_ROOT", str(data_root))

    from app_paths import get_app_paths
    from config_repository import ConfigReadError, bootstrap_data_home
    import workspaces

    paths = get_app_paths()
    bootstrap_data_home(paths)
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", paths.registry_path)

    registry = workspaces.load_workspace_registry()
    assert registry["active_workspace"] == "default"
    assert workspaces.active_workspace_config_path() == paths.default_workspace_config

    paths.registry_path.write_text("active_workspace: [\n", encoding="utf-8")
    with pytest.raises(ConfigReadError):
        workspaces.load_workspace_registry()


def test_fresh_bootstrap_default_workspace_loads_through_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        key = client.get("/api/session-key").json()["key"]
        loaded = client.post(
            "/api/workspaces/default/load",
            headers={"X-LMZ-API-KEY": key},
        )
        session = client.get("/api/runtime/session")

    assert loaded.status_code == 200
    assert loaded.json()["status"] == "success"
    assert session.status_code == 200
    assert session.json()["loaded"] is True
