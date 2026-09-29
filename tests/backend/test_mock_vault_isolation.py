import asyncio
import concurrent.futures
import importlib
import inspect
import io
import json
import logging
import os
import re
import runpy
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException


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
        if name in {"api", "utils", "runtime_context", "runtime_activation", "queue_service", "md_generator", "media_lifecycle", "metadata_index", "metadata_maintenance", "processor", "external_ingestion", "thumbnails", "fingerprint", "artists", "platforms", "review_cache", "topics", "vaults", "vault_packages", "workspace_db", "ingest_control", "workspaces"} or name.startswith(("api.", "logger", "db.", "tagging", "downloaders")):
            del sys.modules[name]
    from app_paths import get_app_paths
    from config_repository import SettingsRepository, bootstrap_data_home

    paths = get_app_paths()
    bootstrap_data_home(paths)
    repository = SettingsRepository(paths.settings_path)
    current = repository.read()
    settings = current.value
    firewall = legacy.get("firewall", {})
    settings.ingestion.accepted_media.extensions = list(firewall.get("allowed_extensions") or settings.ingestion.accepted_media.extensions)
    settings.ingestion.accepted_media.mime_types = list(firewall.get("allowed_mimes") or settings.ingestion.accepted_media.mime_types)
    if isinstance(legacy.get("processing"), dict):
        settings.ingestion.processing = settings.ingestion.processing.model_validate(legacy["processing"])
    if isinstance(legacy.get("tagging"), dict):
        settings.tagging = settings.tagging.model_validate(legacy["tagging"])
    if isinstance(legacy.get("ingestion_concurrency"), dict):
        settings.ingestion.concurrency = settings.ingestion.concurrency.model_validate(legacy["ingestion_concurrency"])
    repository.replace(settings, expected_etag=current.etag)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "default",
        "workspaces": {"default": {"name": "Default", "config_path": str(work / "config.yaml")}},
    })
    modules = []
    for name in module_names:
        if name == "api":
            # The package exposes its real route modules after loading the app composition root.
            importlib.import_module("api.app")
            modules.append(importlib.import_module("api"))
        else:
            modules.append(importlib.import_module(name))
    return modules


def insert_mock_item(sqlite_operator, item_hash: str, artist: str = "DB Artist", date_added: str = "2026-01-02 03:04:05"):
    conn = sqlite_operator.init_database()
    storage_id = sqlite_operator.allocate_storage_id(conn)
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, ?, '', '', 'local', ?, '')
        """,
        (item_hash, storage_id, f"{item_hash}.jpg", date_added, artist),
    )
    conn.commit()
    return conn


def storage_id_for(conn, item_hash: str) -> str:
    row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
    assert row and row[0]
    return row[0]


def app_settings_config(mime_types: list[str], extensions: list[str], tagging: dict | None = None) -> dict:
    return {
        "ingestion": {
            "accepted_media": {"mime_types": mime_types, "extensions": extensions},
            "processing": {},
        },
        "tagging": tagging or {},
    }


def frontmatter_from_markdown(text: str) -> dict:
    return yaml.safe_load(text.split("---", 2)[1]) or {}


def load_maintenance_tool(name: str):
    tool_path = ROOT / "tools" / "maintenance" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_maintenance_script(name: str):
    return load_maintenance_tool(name)


def write_cookie_file(path: Path, domain: str, name: str = "sessionid"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\tmock-value\n", encoding="utf-8")


def write_compact_note(utils, conn, item_hash: str, text: str):
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(text, encoding="utf-8")
    return note_path


def test_config_override_resolves_paths_inside_mock_vault(monkeypatch, tmp_path):
    utils, runtime_context = fresh_backend(monkeypatch, tmp_path, "utils", "runtime_context")
    ctx = runtime_context.get_runtime_context()

    assert utils.CONFIG_PATH == tmp_path / "mock-vault" / "config.yaml"
    assert ctx.config_path == utils.CONFIG_PATH
    assert ctx.root == utils.CONFIG_ROOT
    assert ctx.models_dir == tmp_path / ".lmz" / "app" / "models"
    assert utils.MODELS_DIR == tmp_path / ".lmz" / "app" / "models"
    assert ctx.active_vault.id == utils.ACTIVE_VAULT_ID
    assert ctx.active_vault.root == utils.ACTIVE_VAULT_ROOT
    assert ctx.active_vault.db_path == utils.DB_PATH
    assert ctx.topics_dir == utils.TOPICS_DIR
    assert utils.VAULT_DIR == tmp_path / "mock-vault" / "data" / "vaults" / "default" / "vault"
    assert utils.DB_PATH == tmp_path / "mock-vault" / "data" / "vaults" / "default" / "db" / "lmz_main.db"
    assert utils.LOCAL_INGEST_DIR == tmp_path / "mock-vault" / "data" / "vaults" / "default" / "local_ingest"
    assert utils.ONLINE_INGEST_DIR == tmp_path / "mock-vault" / "data" / "vaults" / "default" / "online_ingest"
    assert utils.THUMBNAILS_DIR == tmp_path / "mock-vault" / "data" / "vaults" / "default" / "ui_cache" / "thumbnails"
    assert utils.TOPICS_DIR == tmp_path / "mock-vault" / "data" / "topics"
    assert utils.platform_cookie_path("x") == tmp_path / "app-auth" / "x" / "cookies.txt"
    assert str(ROOT / "data") not in str(utils.VAULT_DIR)

    utils.setup_directories()
    for platform in utils.AUTH_COOKIE_PLATFORMS:
        assert (tmp_path / "app-auth" / platform).is_dir()


def test_platform_cookie_path_is_canonical(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    platform_path = utils.platform_cookie_path("x")
    write_cookie_file(platform_path, ".x.com", "ct0")

    info = utils.get_platform_cookie_path("x")
    status = utils.get_cookie_auth_status()

    assert info["status"] == "available"
    assert info["source"] == "platform"
    assert info["path"] == str(platform_path)
    assert status["platform_details"]["x"]["source"] == "platform"
    assert "legacy_cookies_used" not in status


def test_app_auth_root_defaults_to_lmz_app_secrets(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    monkeypatch.delenv("LMZ_AUTH_ROOT")

    assert utils.app_auth_root() == tmp_path / ".lmz" / "app" / "secrets" / "auth"


def test_shared_cookie_path_is_ignored(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    legacy_path = utils.CONFIG_ROOT / "secrets" / "cookies.txt"
    write_cookie_file(legacy_path, ".instagram.com", "sessionid")
    info = utils.get_platform_cookie_path("instagram")
    status = utils.get_cookie_auth_status()

    assert info == {"platform": "instagram", "status": "missing", "source": "missing", "path": ""}
    assert status["platform_details"]["instagram"]["source"] == "missing"


def test_empty_platform_cookie_file_reports_missing(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    cookie_path = utils.platform_cookie_path("x")
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    info = utils.get_platform_cookie_path("x")

    assert info == {"platform": "x", "status": "missing", "source": "missing", "path": str(cookie_path)}


def test_unreadable_platform_cookie_file_reports_unreadable(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    cookie_path = utils.platform_cookie_path("youtube")
    write_cookie_file(cookie_path, ".youtube.com", "SID")
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == cookie_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    info = utils.get_platform_cookie_path("youtube")

    assert info == {"platform": "youtube", "status": "unreadable", "source": "unreadable", "path": str(cookie_path)}


def test_pixiv_refresh_token_file_is_canonical(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    token_path = utils.pixiv_refresh_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" file-token\n", encoding="utf-8")
    info = utils.get_pixiv_refresh_token()

    assert info == {
        "token": "file-token",
        "status": "available",
        "source": "file",
        "path": str(token_path),
    }


def test_pixiv_refresh_token_empty_file_is_missing(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    token_path = utils.pixiv_refresh_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("  \n", encoding="utf-8")

    info = utils.get_pixiv_refresh_token()

    assert info["token"] == ""
    assert info["status"] == "missing"
    assert info["source"] == "missing"
    assert info["path"] == str(token_path)


def test_pixiv_refresh_token_unreadable_file_returns_unreadable(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    token_path = utils.pixiv_refresh_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("file-token", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == token_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    info = utils.get_pixiv_refresh_token()

    assert info["token"] == ""
    assert info["status"] == "unreadable"
    assert info["source"] == "unreadable"


def test_pixiv_refresh_token_missing_when_file_not_exists(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")

    info = utils.get_pixiv_refresh_token()

    assert info["token"] == ""
    assert info["status"] == "missing"
    assert info["source"] == "missing"


def test_auth_scan_reports_only_canonical_sources(monkeypatch, tmp_path):
    common, utils = fresh_backend(monkeypatch, tmp_path, "api.common", "utils")
    auth_logs = []
    x_path = utils.platform_cookie_path("x")
    youtube_path = utils.platform_cookie_path("youtube")
    write_cookie_file(x_path, ".twitter.com", "auth_token")
    write_cookie_file(youtube_path, ".youtube.com", "SID")
    monkeypatch.setattr(common, "get_pixiv_refresh_token", lambda: {
        "token": "secret-token",
        "status": "available",
        "source": "file",
        "path": "ignored",
    })
    monkeypatch.setattr(common, "log_auth", lambda *args, **kwargs: auth_logs.append((args, kwargs)))

    auth = common._scan_auth_status_sync()

    assert auth["cookies"] == "available"
    assert "cookies_path" not in auth
    assert "legacy_cookies_path" not in auth
    assert "legacy_cookies_used" not in auth
    assert auth["platforms"]["X"]["cookie_source"] == "platform"
    assert auth["platforms"]["X"]["cookies_path"] == str(x_path)
    assert auth["platforms"]["YouTube"]["cookie_source"] == "platform"
    assert auth["platforms"]["YouTube"]["cookies_path"] == str(youtube_path)
    assert auth["platforms"]["Pixiv"]["token"] == "available"
    assert auth["platforms"]["Pixiv"]["token_source"] == "file"
    assert "secret-token" not in str(auth)
    assert "secret-token" not in str(auth_logs)


def test_missing_platform_cookie_reports_missing(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")

    info = utils.get_platform_cookie_path("pinterest")

    assert info["status"] == "missing"
    assert info["source"] == "missing"
    assert info["path"] == ""


def test_injected_runtime_context_paths_and_databases(monkeypatch, tmp_path):
    utils, runtime_context, sqlite_operator, workspace_db = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "runtime_context",
        "db.sqlite_operator",
        "workspace_db",
    )
    work = tmp_path / "mock-vault"
    injected_root = tmp_path / "injected-workspace"
    injected_root.mkdir()
    config = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    config["active_vault"] = "alt"
    config["vaults"] = {
        "alt": {
            "name": "Alt",
            "root": "data/vaults/alt",
        }
    }
    injected_config = injected_root / "config.yaml"
    injected_config.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    injected_ctx = runtime_context.build_runtime_context(injected_config)

    assert injected_ctx.config_path == injected_config
    assert injected_ctx.root == injected_root
    assert injected_ctx.active_vault.id == "alt"
    assert injected_ctx.active_vault.db_path == injected_root / "data" / "vaults" / "alt" / "db" / "lmz_main.db"
    assert utils.DB_PATH == work / "data" / "vaults" / "default" / "db" / "lmz_main.db"
    assert utils.note_path_for("ab" * 32, "000000000001", ctx=injected_ctx) == injected_root / "data" / "vaults" / "alt" / "vault" / "notes" / "ab" / "000000000001.md"
    assert utils.note_path_for("ab" * 32, "000000000001") == work / "data" / "vaults" / "default" / "vault" / "notes" / "ab" / "000000000001.md"

    vault_conn = sqlite_operator.init_database(ctx=injected_ctx)
    try:
        assert vault_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        vault_conn.close()
    assert injected_ctx.active_vault.db_path.exists()
    assert utils.DB_PATH.resolve() != injected_ctx.active_vault.db_path.resolve()

    ws_conn = workspace_db.connect_workspace_database(injected_ctx)
    try:
        assert ws_conn.execute("SELECT COUNT(*) FROM platforms").fetchone()[0] >= 1
    finally:
        ws_conn.close()
    assert injected_ctx.workspace_db_path.exists()


def injected_context_for(runtime_context, tmp_path: Path):
    work = tmp_path / "mock-vault"
    injected_root = tmp_path / "injected-workspace"
    injected_root.mkdir(exist_ok=True)
    config = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    config["active_vault"] = "alt"
    config["vaults"] = {"alt": {"name": "Alt", "root": "data/vaults/alt"}}
    injected_config = injected_root / "config.yaml"
    injected_config.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return runtime_context.build_runtime_context(injected_config)


def test_queue_service_context_isolates_vault_queues(monkeypatch, tmp_path):
    utils, runtime_context, queue_service = fresh_backend(monkeypatch, tmp_path, "utils", "runtime_context", "queue_service")
    injected_ctx = injected_context_for(runtime_context, tmp_path)

    queue_service.write_queue("normal", "https://default.example/item\n")
    queue_service.write_queue("normal", "https://injected.example/item\n", ctx=injected_ctx)

    assert queue_service.read_queue("normal") == "https://default.example/item\n"
    assert queue_service.read_queue("normal", ctx=injected_ctx) == "https://injected.example/item\n"
    assert queue_service.queue_path("normal").is_relative_to(utils.QUEUES_DIR)
    assert queue_service.queue_path("normal", ctx=injected_ctx).is_relative_to(injected_ctx.active_vault.queues_dir)
    assert queue_service.queue_path("normal") != queue_service.queue_path("normal", ctx=injected_ctx)


def test_review_cache_context_isolates_vaults(monkeypatch, tmp_path):
    utils, runtime_context, review_cache = fresh_backend(monkeypatch, tmp_path, "utils", "runtime_context", "review_cache")
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    default_hash = "11" * 32
    injected_hash = "22" * 32
    default_file = utils.REVIEW_DIR / "default.jpg"
    injected_file = injected_ctx.active_vault.review_dir / "injected.jpg"
    default_file.parent.mkdir(parents=True, exist_ok=True)
    injected_file.parent.mkdir(parents=True, exist_ok=True)
    default_file.write_bytes(b"default")
    injected_file.write_bytes(b"injected")
    default_file.with_suffix(".jpg.json").write_text(json.dumps({"state": "pending", "file_hash": default_hash}), encoding="utf-8")
    injected_file.with_suffix(".jpg.json").write_text(json.dumps({"state": "pending", "file_hash": injected_hash}), encoding="utf-8")

    assert review_cache.pending_review_match(default_hash)["file_hash"] == default_hash
    assert review_cache.pending_review_match(injected_hash) is None
    assert review_cache.pending_review_match(injected_hash, ctx=injected_ctx)["file_hash"] == injected_hash
    assert review_cache.pending_review_match(default_hash, ctx=injected_ctx) is None


def test_logger_reconfigure_writes_to_context_logs(monkeypatch, tmp_path):
    runtime_context, lmz_logger = fresh_backend(monkeypatch, tmp_path, "runtime_context", "logger")
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    injected_ctx.active_vault.root.mkdir(parents=True, exist_ok=True)

    try:
        lmz_logger.reconfigure_logging(injected_ctx)
        lmz_logger.log_system("INFO", "context logger test")
        log_file = injected_ctx.active_vault.logs_dir / "structured" / "system.jsonl"
        assert log_file.exists()
        assert "context logger test" in log_file.read_text(encoding="utf-8")
    finally:
        lmz_logger.reconfigure_logging(default_ctx)


def test_api_dynamic_media_routes_use_active_context_and_block_traversal(monkeypatch, tmp_path):
    utils, api = fresh_backend(monkeypatch, tmp_path, "utils", "api")
    asset = utils.ASSETS_DIR / "aa" / "item.jpg"
    review = utils.REVIEW_DIR / "review.jpg"
    asset.parent.mkdir(parents=True, exist_ok=True)
    review.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")
    review.write_bytes(b"review")

    assert Path(api.common._file_response_under(utils.ASSETS_DIR, "aa/item.jpg").path) == asset
    assert Path(api.common._file_response_under(utils.REVIEW_DIR, "review.jpg").path) == review
    with pytest.raises(HTTPException):
        api.common._file_response_under(utils.ASSETS_DIR, "../config.yaml")


def test_metadata_watchdog_uses_injected_notes_and_wd_dirs(monkeypatch, tmp_path):
    runtime_context, metadata_index = fresh_backend(monkeypatch, tmp_path, "runtime_context", "metadata_index")
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    scheduled: list[tuple[str, bool]] = []

    class FakeHandler:
        pass

    class FakeObserver:
        daemon = False

        def schedule(self, handler, path, recursive=False):
            scheduled.append((path, recursive))

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setitem(sys.modules, "watchdog", types.ModuleType("watchdog"))
    events_module = types.ModuleType("watchdog.events")
    events_module.FileSystemEventHandler = FakeHandler
    observers_module = types.ModuleType("watchdog.observers")
    observers_module.Observer = FakeObserver
    monkeypatch.setitem(sys.modules, "watchdog.events", events_module)
    monkeypatch.setitem(sys.modules, "watchdog.observers", observers_module)

    try:
        assert metadata_index.start_metadata_watchdog(ctx=injected_ctx)["status"] == "started"
        assert (str(injected_ctx.active_vault.notes_dir), True) in scheduled
        assert (str(injected_ctx.active_vault.wd_tags_dir), True) in scheduled
    finally:
        metadata_index.reset_metadata_watchdog_state(ctx=injected_ctx)


def test_search_manager_context_isolates_ram_indexes(monkeypatch, tmp_path):
    runtime_context, sqlite_operator, search_manager_module = fresh_backend(
        monkeypatch,
        tmp_path,
        "runtime_context",
        "db.sqlite_operator",
        "db.search_manager",
    )
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    manager = search_manager_module.SearchManager()
    manager.reset_all()
    default_phash = "0" * 16
    injected_phash = "f" * 16

    default_conn = sqlite_operator.init_database(ctx=default_ctx)
    injected_conn = sqlite_operator.init_database(ctx=injected_ctx)
    try:
        default_conn.execute(
            """
            INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, source_url, source_url_norm, platform, source_artist, phash)
            VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, ?, ?, 'local', 'artist', ?)
            """,
            ("default-search", "000000000101", "default.jpg", "https://default.test/item", "https://default.test/item", default_phash),
        )
        default_conn.commit()
        injected_conn.execute(
            """
            INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, source_url, source_url_norm, platform, source_artist, phash)
            VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, ?, ?, 'local', 'artist', ?)
            """,
            ("injected-search", "000000000102", "injected.jpg", "https://injected.test/item", "https://injected.test/item", injected_phash),
        )
        injected_conn.commit()

        manager.hydrate(default_conn, ctx=default_ctx)
        manager.hydrate(injected_conn, ctx=injected_ctx)
    finally:
        default_conn.close()
        injected_conn.close()

    assert manager.query_image(default_phash, ctx=default_ctx)[0][0] == "default-search"
    assert manager.query_image(default_phash, ctx=injected_ctx) == []
    assert manager.query_image(injected_phash, ctx=injected_ctx)[0][0] == "injected-search"
    assert manager.query_image(injected_phash, ctx=default_ctx) == []
    assert manager.url_exists("https://default.test/item", ctx=default_ctx)
    assert not manager.url_exists("https://default.test/item", ctx=injected_ctx)


def test_local_ingest_state_and_stop_events_are_context_isolated(monkeypatch, tmp_path):
    runtime_context, api = fresh_backend(monkeypatch, tmp_path, "runtime_context", "api")
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)

    api.common.reset_local_ingest_state(default_ctx)
    api.common.reset_local_ingest_state(injected_ctx)
    with api.common.local_ingest_lock(default_ctx):
        api.common.local_ingest_state(default_ctx)["running"] = True
        api.common.local_ingest_state(default_ctx)["failed_paths"] = ["default.jpg"]
    with api.common.local_ingest_lock(injected_ctx):
        api.common.local_ingest_state(injected_ctx)["running"] = False
        api.common.local_ingest_state(injected_ctx)["failed_paths"] = ["injected.jpg"]

    api.common.local_ingest_stop_event(default_ctx).set()

    assert api.ingestion._snapshot_local_ingest_state(default_ctx)["running"] is True
    assert api.ingestion._snapshot_local_ingest_state(injected_ctx)["running"] is False
    assert api.ingestion._snapshot_local_ingest_state(default_ctx)["failed_paths"] == ["default.jpg"]
    assert api.ingestion._snapshot_local_ingest_state(injected_ctx)["failed_paths"] == ["injected.jpg"]
    assert api.common.local_ingest_stop_event(default_ctx).is_set()
    assert not api.common.local_ingest_stop_event(injected_ctx).is_set()


def test_online_stop_event_helper_is_context_isolated(monkeypatch, tmp_path):
    runtime_context, ingest_control = fresh_backend(monkeypatch, tmp_path, "runtime_context", "ingest_control")
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)

    ingest_control.clear_stop_flags(default_ctx)
    ingest_control.clear_stop_flags(injected_ctx)
    ingest_control.online_stop_event(default_ctx).set()

    assert ingest_control.online_stop_event(default_ctx).is_set()
    assert not ingest_control.online_stop_event(injected_ctx).is_set()


def test_metadata_repair_status_is_context_isolated(monkeypatch, tmp_path):
    runtime_context, metadata_index = fresh_backend(monkeypatch, tmp_path, "runtime_context", "metadata_index")
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    default_state = metadata_index._runtime_state(default_ctx)
    injected_state = metadata_index._runtime_state(injected_ctx)

    with default_state.repair_lock:
        default_state.repair_running = True
    with injected_state.repair_lock:
        injected_state.repair_running = False
    metadata_index._reset_maintenance_rebuild_job("full", ctx=default_ctx)

    assert metadata_index.metadata_repair_running(default_ctx) is True
    assert metadata_index.metadata_repair_running(injected_ctx) is False
    assert metadata_index.maintenance_rebuild_status(default_ctx)["running"] is True
    assert metadata_index.maintenance_rebuild_status(injected_ctx)["running"] is False

    with default_state.repair_lock:
        default_state.repair_running = False


def test_metadata_repair_worker_uses_captured_context(monkeypatch, tmp_path):
    runtime_context, sqlite_operator, metadata_index = fresh_backend(
        monkeypatch,
        tmp_path,
        "runtime_context",
        "db.sqlite_operator",
        "metadata_index",
    )
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    captured = []

    class FakeConn:
        def close(self):
            pass

        def commit(self):
            pass

    def fake_init_database(ctx=None, db_path=None):
        captured.append(ctx)
        return FakeConn()

    monkeypatch.setattr(sqlite_operator, "init_database", fake_init_database)
    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)
    monkeypatch.setattr(metadata_index, "reindex_stale_metadata_batch", lambda conn, batch_size, allow_scan=False: {"queued": 0, "source": "scan"})

    metadata_index._repair_worker(full=False, maintenance=False, ctx=injected_ctx)

    assert captured == [injected_ctx]
    assert metadata_index.metadata_repair_running(injected_ctx) is False


def test_metadata_watchdog_restart_clears_old_state_and_uses_new_context(monkeypatch, tmp_path):
    runtime_context, metadata_index = fresh_backend(monkeypatch, tmp_path, "runtime_context", "metadata_index")
    default_ctx = runtime_context.get_runtime_context()
    injected_ctx = injected_context_for(runtime_context, tmp_path)
    scheduled: list[tuple[str, bool]] = []

    class FakeHandler:
        pass

    class FakeObserver:
        daemon = False

        def schedule(self, handler, path, recursive=False):
            scheduled.append((path, recursive))

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setitem(sys.modules, "watchdog", types.ModuleType("watchdog"))
    events_module = types.ModuleType("watchdog.events")
    events_module.FileSystemEventHandler = FakeHandler
    observers_module = types.ModuleType("watchdog.observers")
    observers_module.Observer = FakeObserver
    monkeypatch.setitem(sys.modules, "watchdog.events", events_module)
    monkeypatch.setitem(sys.modules, "watchdog.observers", observers_module)

    try:
        assert metadata_index.start_metadata_watchdog(ctx=default_ctx)["status"] == "started"
        metadata_index._watchdog_pending.add("old-hash")
        metadata_index._watchdog_storage_map["old-storage"] = "old-hash"
        scheduled.clear()
        assert metadata_index.restart_metadata_watchdog(ctx=injected_ctx)["status"] == "started"
        assert metadata_index._watchdog_pending == set()
        assert "old-storage" not in metadata_index._watchdog_storage_map
        assert metadata_index._watchdog_ctx == injected_ctx
        assert (str(injected_ctx.active_vault.notes_dir), True) in scheduled
        assert (str(injected_ctx.active_vault.wd_tags_dir), True) in scheduled
    finally:
        metadata_index.reset_metadata_watchdog_state()


def test_runtime_switch_preflight_reports_runtime_blockers(monkeypatch, tmp_path):
    runtime_context, metadata_index, api = fresh_backend(monkeypatch, tmp_path, "runtime_context", "metadata_index", "api")
    ctx = runtime_context.get_runtime_context()
    api.common.reset_local_ingest_state(ctx)

    assert api.ingestion.runtime_switch_preflight(ctx) == {"allowed": True, "blockers": []}

    with api.common.local_ingest_lock(ctx):
        api.common.local_ingest_state(ctx)["running"] = True
    result = api.ingestion.runtime_switch_preflight(ctx)
    assert result["allowed"] is False
    assert "local_ingest_running" in result["blockers"]

    with api.common.local_ingest_lock(ctx):
        api.common.local_ingest_state(ctx)["running"] = False
    state = metadata_index._runtime_state(ctx)
    with state.repair_lock:
        state.repair_running = True
    result = api.ingestion.runtime_switch_preflight(ctx)
    assert result["allowed"] is False
    assert "metadata_repair_running" in result["blockers"]
    with state.repair_lock:
        state.repair_running = False

    assert api.ingestion.INGESTION_LOCK.acquire(blocking=False)
    try:
        result = api.ingestion.runtime_switch_preflight(ctx)
        assert result["allowed"] is False
        assert "online_ingest_running" in result["blockers"]
    finally:
        api.ingestion.INGESTION_LOCK.release()


def test_workspace_registry_resolves_active_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("LMZ_CONFIG_PATH", raising=False)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    for name in ["utils", "runtime_context", "workspaces"]:
        sys.modules.pop(name, None)
    workspaces = importlib.import_module("workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    custom_root = tmp_path / "custom-workspace"
    custom_root.mkdir()
    custom_config = custom_root / "config.yaml"
    topology = {
        "schema_version": 1,
        "active_vault": "default",
        "vaults": {"default": {"name": "Default", "root": "data/vaults/default"}},
    }
    custom_config.write_text(yaml.safe_dump(topology), encoding="utf-8")
    override_config = tmp_path / "override.yaml"
    override_config.write_text(yaml.safe_dump(topology), encoding="utf-8")
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "custom",
        "workspaces": {
            "default": {"name": "Default", "config_path": str(override_config)},
            "custom": {"name": "Custom", "config_path": str(custom_config)},
        },
    })

    utils = importlib.import_module("utils")
    runtime_context = importlib.import_module("runtime_context")
    runtime_context.reload_runtime_context()
    assert utils.CONFIG_PATH == custom_config
    assert utils.CONFIG_ROOT == custom_root

    monkeypatch.setenv("LMZ_CONFIG_PATH", str(override_config))
    sys.modules.pop("utils", None)
    sys.modules.pop("runtime_context", None)
    utils = importlib.import_module("utils")
    runtime_context = importlib.import_module("runtime_context")
    runtime_context.reload_runtime_context()
    assert utils.CONFIG_PATH == override_config


def test_workspace_setup_creates_lmz_layout_and_resolves_paths(monkeypatch, tmp_path):
    setup_tool = load_maintenance_script("setup_workspace")
    workspace_parent = (Path(tempfile.gettempdir()) / f"lmz-workspace-test-{time.time_ns()}").resolve()

    payload = setup_tool.setup_lmz_workspace(workspace_parent)
    config_path = Path(payload["config_path"])

    assert config_path == workspace_parent / "lmz" / "config.yaml"
    for relative in [
        "data/topics",
        "data/vaults/default/vault/notes",
        "data/vaults/default/vault/assets",
        "data/vaults/default/db",
        "data/vaults/default/logs/raw",
        "data/vaults/default/logs/structured",
        "data/vaults/default/review",
        "data/vaults/default/wd-tags",
    ]:
        assert (workspace_parent / "lmz" / relative).exists()
    assert not (workspace_parent / "lmz" / "data" / "secrets" / "auth").exists()
    assert not (workspace_parent / "lmz" / "data" / "secrets").exists()
    assert not (workspace_parent / "lmz" / "data" / "models").exists()
    assert Path(payload["marker_path"]).exists()
    assert payload["managed"] is True

    monkeypatch.setenv("LMZ_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LMZ_DATA_ROOT", str(tmp_path / ".lmz"))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    for name in list(sys.modules):
        if name in {"utils", "runtime_context", "db.sqlite_operator", "api", "topics", "vaults", "metadata_index", "md_generator", "artists", "platforms", "workspace_db", "review_cache"} or name.startswith(("api.", "logger", "db.", "tagging")):
            del sys.modules[name]
    utils = importlib.import_module("utils")
    from app_paths import get_app_paths
    from config_repository import bootstrap_data_home
    bootstrap_data_home(get_app_paths())
    sqlite_operator = importlib.import_module("db.sqlite_operator")
    library = importlib.import_module("api.library")

    assert utils.CONFIG_ROOT == workspace_parent / "lmz"
    assert utils.MODELS_DIR == tmp_path / ".lmz" / "app" / "models"
    assert utils.TOPICS_DIR == workspace_parent / "lmz" / "data" / "topics"
    assert utils.VAULT_DIR == workspace_parent / "lmz" / "data" / "vaults" / "default" / "vault"
    assert utils.DB_PATH == workspace_parent / "lmz" / "data" / "vaults" / "default" / "db" / "lmz_main.db"
    assert "external_tools" not in utils.get_app_settings()
    assert set(utils.get_workspace_config()) == {"schema_version", "active_vault", "vaults"}
    conn = sqlite_operator.init_database()
    item_hash = "97" * 32
    storage_id = sqlite_operator.allocate_storage_id(conn)
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:00', '', '', 'local', 'artist', '')
        """,
        (item_hash, storage_id, f"{item_hash}.jpg"),
    )
    conn.commit()
    conn.close()

    detail = library._update_item_sync(item_hash, library.ItemUpdate(topics=["obsidian topic"]))
    assert detail["topics"] == ["obsidian_topic"]
    assert (workspace_parent / "lmz" / "data" / "topics" / "obsidian_topic.md").exists()
    assert utils.note_path_for(item_hash, storage_id).exists()
    runtime = utils.get_runtime_context()
    assert runtime.root == workspace_parent / "lmz"
    assert runtime.active_vault.id == "default"
    shutil.rmtree(workspace_parent, ignore_errors=True)


def test_workspace_setup_does_not_claim_preexisting_unmarked_folder(tmp_path):
    setup_tool = load_maintenance_script("setup_workspace")
    parent = Path(tempfile.gettempdir()) / f"lmz-existing-parent-{time.time_ns()}"
    workspace = parent / "lmz"
    workspace.mkdir(parents=True)
    (workspace / "personal.txt").write_text("mine", encoding="utf-8")

    try:
        payload = setup_tool.setup_lmz_workspace(parent)
        assert payload["managed"] is False
        assert not Path(payload["marker_path"]).exists()
        assert (workspace / "personal.txt").read_text(encoding="utf-8") == "mine"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_workspace_config_rejects_legacy_models_path(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    from config_schema import WorkspaceConfig
    config = utils.get_workspace_config()
    config["paths"] = {"models": "data/models"}

    with pytest.raises(ValueError, match="paths"):
        WorkspaceConfig.model_validate(config)


def test_runtime_context_rejects_legacy_models_path(monkeypatch, tmp_path):
    runtime_context, = fresh_backend(monkeypatch, tmp_path, "runtime_context")
    from config_repository import ConfigReadError
    workspace_root = tmp_path / "bad-workspace"
    workspace_root.mkdir()
    config_path = workspace_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "active_vault": "default",
                "vaults": {"default": {"name": "Default", "root": "data/vaults/default"}},
                "paths": {"models": "data/models", "secrets": "data/secrets"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigReadError, match="paths"):
        runtime_context.build_runtime_context(config_path)


def test_tagger_model_lookup_uses_runtime_models_dir(monkeypatch, tmp_path):
    service, = fresh_backend(monkeypatch, tmp_path, "tagging.service")
    expected_dir = tmp_path / "app-models"
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return str(Path(kwargs["local_dir"]) / kwargs["filename"])

    monkeypatch.setattr(service, "get_runtime_context", lambda: types.SimpleNamespace(models_dir=expected_dir))

    model_path, tags_path = service._ensure_model_files("org/test-model", fake_download)

    assert calls
    assert {call["local_dir"] for call in calls} == {str(expected_dir / "test-model")}
    assert model_path == expected_dir / "test-model" / "model.onnx"
    assert tags_path == expected_dir / "test-model" / "selected_tags.csv"


def test_workspace_setup_refuses_runtime_paths(tmp_path):
    setup_tool = load_maintenance_script("setup_workspace")
    for dangerous in [ROOT, ROOT / "data", ROOT / "config", ROOT / "logs", ROOT / "secrets"]:
        with pytest.raises(ValueError):
            setup_tool.setup_lmz_workspace(dangerous)


def test_workspace_api_lists_registers_and_sets_active(monkeypatch, tmp_path):
    api, workspaces = fresh_backend(monkeypatch, tmp_path, "api", "workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "default",
        "workspaces": {
            "default": {"name": "Default", "config_path": str(tmp_path / "mock-vault" / "config.yaml")},
        },
    })
    workspace_parent = (Path(tempfile.gettempdir()) / f"lmz-api-test-{time.time_ns()}").resolve()
    try:
        initial = api.runtime._get_workspaces_sync()
        assert initial["active"] == "default"
        assert initial["items"][0]["id"] == "default"

        added = api.runtime._create_workspace_sync({"path": str(workspace_parent), "name": "API Workspace"})
        assert any(item["name"] == "API Workspace" for item in added["items"])

        workspace_id = next(item["id"] for item in added["items"] if item["name"] == "API Workspace")
        active = api.runtime._set_workspace_active_sync({"id": workspace_id})
        assert active["restart_required"] is False
        assert active["active"] == workspace_id
    finally:
        shutil.rmtree(workspace_parent, ignore_errors=True)


def test_vault_api_creates_sets_active_and_rejects_active_delete(monkeypatch, tmp_path):
    api, vaults = fresh_backend(monkeypatch, tmp_path, "api", "vaults")

    created = api.runtime._create_vault_sync({"name": "Second Vault"})
    second = next(item for item in created["items"] if item["id"] == "second-vault")

    assert Path(second["root"]).exists()
    assert Path(second["db_path"]).exists()

    active = api.runtime._set_vault_active_sync({"id": "second-vault"})
    assert active["restart_required"] is False
    assert active["active"] == "second-vault"

    with pytest.raises(HTTPException) as exc:
        api.runtime._delete_vault_sync("second-vault", confirm=True)
    assert exc.value.status_code == 400


def test_vault_rename_delete_confirm_and_missing_errors(monkeypatch, tmp_path):
    api, vaults = fresh_backend(monkeypatch, tmp_path, "api", "vaults")

    api.runtime._create_vault_sync({"name": "Temporary Vault"})
    renamed = api.runtime._rename_vault_sync("temporary-vault", {"name": "Renamed Vault"})
    assert any(item["id"] == "temporary-vault" and item["name"] == "Renamed Vault" for item in renamed["items"])

    with pytest.raises(HTTPException) as missing:
        api.runtime._rename_vault_sync("missing-vault", {"name": "Missing"})
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as needs_confirm:
        api.runtime._delete_vault_sync("temporary-vault", confirm=False)
    assert needs_confirm.value.status_code == 400

    metadata_index = importlib.import_module("metadata_index")
    state = metadata_index._runtime_state(vaults._ctx())
    deadline = time.monotonic() + 5
    while state.repair_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state.repair_running is False

    deleted = api.runtime._delete_vault_sync("temporary-vault", confirm=True)
    assert all(item["id"] != "temporary-vault" for item in deleted["items"])

    with pytest.raises(HTTPException) as missing_delete:
        api.runtime._delete_vault_sync("temporary-vault", confirm=True)
    assert missing_delete.value.status_code == 404


def test_vault_health_reports_orphans_and_stale_index_rows(monkeypatch, tmp_path):
    vaults, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "vaults", "db.sqlite_operator")
    conn = insert_mock_item(sqlite_operator, "cd" * 32)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO item_topics(item_hash, topic, topic_norm, topic_rel, topic_key) VALUES (?, 'Ghost', 'ghost', '', 'plain:ghost')", ("missing",))
    conn.commit()
    conn.close()

    default = next(item for item in vaults.vault_list() if item["id"] == "default")
    orphan = Path(default["root"]) / "vault" / "assets" / "zz" / "orphan.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    report = vaults.audit_vault_health("default")

    assert report["status"] == "success"
    assert report["stale_index_rows"]["topics"] == 1
    assert str(orphan) in report["orphans"]["assets"]
    assert report["issue_count"] >= 2


def test_vault_repair_wd_tagging_creates_cache_and_reindexes(monkeypatch, tmp_path):
    vaults, sqlite_operator, utils = fresh_backend(monkeypatch, tmp_path, "vaults", "db.sqlite_operator", "utils")
    item_hash = "ef" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    asset_path = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"not a real image")
    write_compact_note(utils, conn, item_hash, "---\ntopics: []\n---\n")
    conn.close()

    service = importlib.import_module("tagging.service")
    real_result = service.TagResult(
        hash=item_hash,
        status="ok",
        model="fake",
        threshold=0.35,
        created_at="2026-01-01 00:00:00",
        rating={"label": "safe"},
        character_tags=[{"name": "character one"}],
        tags=[{"name": "general one"}],
    )

    def fake_tag_media(media_path, item_hash=None, config=None, storage_id=None, ctx=None):
        cache_path = utils.wd_tag_cache_path_for(item_hash, storage_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(real_result.to_dict()), encoding="utf-8")
        return real_result

    monkeypatch.setattr(service, "tag_media", fake_tag_media)

    result = vaults.repair_vault("default", actions=["wd_tagging"])

    conn = sqlite_operator.init_database()
    try:
        rows = conn.execute("SELECT tag_type, tag FROM item_wd_tags WHERE item_hash = ? ORDER BY tag_type, tag", (item_hash,)).fetchall()
    finally:
        conn.close()
    note_data = frontmatter_from_markdown(utils.note_path_for(item_hash, storage_id).read_text(encoding="utf-8"))

    assert result["wd_tagging"]["tagged"] == 1
    assert utils.wd_tag_cache_path_for(item_hash, storage_id).exists()
    assert note_data["wd_rating"] == "safe"
    assert note_data["wd_character_tags"] == ["character one"]
    assert note_data["wd_tags"] == ["general one"]
    assert ("general", "general one") in rows
    assert ("character", "character one") in rows


def test_vault_wd_repair_reports_locked_wrong_shard_and_cleans_after_retry(monkeypatch, tmp_path):
    utils, sqlite_operator, vaults = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "vaults",
    )
    item_hash = "c5" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()

    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")

    canonical = utils.wd_tag_cache_path_for(item_hash, storage_id)
    stale = canonical.parent.parent / "dd" / canonical.name
    canonical.parent.mkdir(parents=True, exist_ok=True)
    stale.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("{}", encoding="utf-8")
    stale.write_text("{}", encoding="utf-8")

    real_unlink = Path.unlink

    def fail_stale_unlink(self, *args, **kwargs):
        if self == stale:
            raise OSError("stale WD cache locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)
    ctx = vaults._ctx_for_vault("default")
    conn = sqlite_operator.init_database()
    try:
        first = vaults._repair_missing_wd_cache(conn, ctx)
    finally:
        conn.close()

    assert canonical.exists()
    assert stale.exists()
    assert first["stale_removed"] == 0
    assert first["cleanup_errors"]
    assert first["cleanup_errors"][0]["path"] == str(stale)

    monkeypatch.setattr(Path, "unlink", real_unlink)
    conn = sqlite_operator.init_database()
    try:
        second = vaults._repair_missing_wd_cache(conn, ctx)
    finally:
        conn.close()

    assert second["stale_removed"] == 1
    assert second["cleanup_errors"] == []
    assert canonical.exists()
    assert not stale.exists()


def test_vault_backup_export_and_import_package(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    source = vaults.create_vault("Portable Vault")
    source_root = Path(next(item["root"] for item in source["items"] if item["id"] == "portable-vault"))
    marker = source_root / "vault" / "notes" / "aa" / "marker.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("portable", encoding="utf-8")
    review_marker = source_root / "review" / "pending.json"
    review_marker.parent.mkdir(parents=True, exist_ok=True)
    review_marker.write_text("review-state", encoding="utf-8")

    with pytest.raises(ValueError, match="confirmation"):
        vaults.backup_vault("portable-vault")

    backup = vaults.backup_vault("portable-vault", confirm=True)
    with pytest.raises(ValueError, match="confirmation"):
        vaults.export_vault("portable-vault")
    exported = vaults.export_vault("portable-vault", confirm=True)
    exported_with_review = vaults.export_vault("portable-vault", confirm=True, include_review=True)
    assert Path(backup["package_path"]).exists()
    assert Path(exported["package_path"]).exists()
    assert Path(exported["package_path"]).name.endswith(".lmzvault.zip")
    assert "exports" in Path(exported["package_path"]).parts
    assert Path(backup["package_path"]).name.endswith(".lmzbackup.zip")
    assert "backups" in Path(backup["package_path"]).parts

    with zipfile.ZipFile(backup["package_path"], "r") as archive:
        manifest = yaml.safe_load(archive.read("lmz-package.yaml").decode("utf-8"))
        assert manifest["package_type"] == "lmz_vault_backup"
        assert manifest["source_vault"]["id"] == "portable-vault"
        assert manifest["contents"]["logs"] is True
        assert "db/lmz_main.db" in archive.namelist()
        assert "vault/notes/aa/marker.md" in archive.namelist()

    with zipfile.ZipFile(exported["package_path"], "r") as archive:
        manifest = yaml.safe_load(archive.read("lmz-package.yaml").decode("utf-8"))
        names = archive.namelist()
        assert manifest["package_type"] == "lmz_vault_export"
        assert manifest["contents"]["review"] is False
        assert "db/lmz_main.db" in names
        assert "vault/notes/aa/marker.md" in names
        assert not any(name.startswith("logs/") for name in names)
        assert not any(name.startswith("queues/") for name in names)
        assert not any(name.startswith("review/") for name in names)

    with zipfile.ZipFile(exported_with_review["package_path"], "r") as archive:
        manifest = yaml.safe_load(archive.read("lmz-package.yaml").decode("utf-8"))
        assert manifest["contents"]["review"] is True
        assert any(name.startswith("review/") for name in archive.namelist())

    with pytest.raises(ValueError, match="expected package type"):
        vaults.preview_import_vault_package(backup["package_path"])
    with pytest.raises(ValueError, match="expected package type"):
        vaults.preview_restore_backup_package(exported["package_path"])

    preview = vaults.preview_import_vault_package(exported["package_path"])
    assert preview["package_type"] == "lmz_vault_export"
    assert preview["target_exists"] is True
    assert "target_vault_exists" in preview["warnings"]

    with pytest.raises(ValueError, match="confirmation"):
        vaults.import_vault_package(exported["package_path"], target_name="Imported Portable", package_fingerprint_value=preview["package_fingerprint"])
    with pytest.raises(ValueError, match="fingerprint"):
        vaults.import_vault_package(exported["package_path"], target_name="Imported Portable", package_fingerprint_value="bad", confirm=True)

    imported = vaults.import_vault_package(
        exported["package_path"],
        target_name="Imported Portable",
        package_fingerprint_value=preview["package_fingerprint"],
        confirm=True,
    )
    imported_root = Path(next(item["root"] for item in imported["items"] if item["id"] == "imported-portable"))

    assert (imported_root / "vault" / "notes" / "aa" / "marker.md").read_text(encoding="utf-8") == "portable"

    restore_preview = vaults.preview_restore_backup_package(backup["package_path"])
    assert restore_preview["package_type"] == "lmz_vault_backup"
    assert restore_preview["target_name"] == "Restored Portable Vault"
    assert restore_preview["target_id"] == "restored-portable-vault"
    with pytest.raises(ValueError, match="confirmation"):
        vaults.restore_backup_package(backup["package_path"], package_fingerprint_value=restore_preview["package_fingerprint"])
    with pytest.raises(ValueError, match="fingerprint"):
        vaults.restore_backup_package(backup["package_path"], package_fingerprint_value="bad", confirm=True)

    restored = vaults.restore_backup_package(
        backup["package_path"],
        package_fingerprint_value=restore_preview["package_fingerprint"],
        confirm=True,
    )
    restored_root = Path(next(item["root"] for item in restored["items"] if item["id"] == "restored-portable-vault"))
    assert (restored_root / "vault" / "notes" / "aa" / "marker.md").read_text(encoding="utf-8") == "portable"
    assert (restored_root / "review" / "pending.json").read_text(encoding="utf-8") == "review-state"

    second_restore_preview = vaults.preview_restore_backup_package(backup["package_path"])
    assert second_restore_preview["target_name"] == "Restored Portable Vault 2"
    assert second_restore_preview["target_id"] == "restored-portable-vault-2"


def test_vault_package_skips_symlinked_files(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    source = vaults.create_vault("Symlink Source")
    source_root = Path(next(item["root"] for item in source["items"] if item["id"] == "symlink-source"))
    asset_dir = source_root / "vault" / "assets" / "aa"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "normal.txt").write_text("normal", encoding="utf-8")
    external_secret = tmp_path / "external-secret.txt"
    external_secret.write_text("do-not-package", encoding="utf-8")
    symlink = asset_dir / "secret-link.txt"
    try:
        symlink.symlink_to(external_secret)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    backup = vaults.backup_vault("symlink-source", confirm=True)
    exported = vaults.export_vault("symlink-source", confirm=True)

    for package_path in (backup["package_path"], exported["package_path"]):
        with zipfile.ZipFile(package_path, "r") as archive:
            names = archive.namelist()
            assert "vault/assets/aa/normal.txt" in names
            assert "vault/assets/aa/secret-link.txt" not in names
            assert all(archive.read(name) != b"do-not-package" for name in names if not name.endswith("/"))


def test_vault_import_preview_rejects_invalid_packages(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    missing_manifest = tmp_path / "missing-manifest.lmzvault.zip"
    with zipfile.ZipFile(missing_manifest, "w") as archive:
        archive.writestr("vault/assets/aa/file.jpg", "data")

    with pytest.raises(ValueError, match="missing package manifest"):
        vaults.preview_import_vault_package(missing_manifest)

    traversal = tmp_path / "traversal.lmzvault.zip"
    manifest = {
        "package_type": "lmz_vault_export",
        "package_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "source_vault": {"id": "source", "name": "Source"},
        "contents": {"db": False, "assets": True, "notes": True, "review": False},
        "counts": {"items": 0, "files": 1},
    }
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("lmz-package.yaml", yaml.safe_dump(manifest))
        archive.writestr("../escape.txt", "bad")

    with pytest.raises(ValueError, match="unsafe archive path"):
        vaults.preview_import_vault_package(traversal)

    backup_traversal = tmp_path / "backup-traversal.lmzbackup.zip"
    backup_manifest = {
        **manifest,
        "package_type": "lmz_vault_backup",
        "contents": {"db": False, "assets": True, "notes": True, "review": True, "logs": True},
    }
    with zipfile.ZipFile(backup_traversal, "w") as archive:
        archive.writestr("lmz-package.yaml", yaml.safe_dump(backup_manifest))
        archive.writestr("../escape.txt", "bad")

    with pytest.raises(ValueError, match="unsafe archive path"):
        vaults.preview_restore_backup_package(backup_traversal)


def test_vault_import_rolls_back_on_config_write_failure(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    source = vaults.create_vault("Rollback Source")
    source_root = Path(next(item["root"] for item in source["items"] if item["id"] == "rollback-source"))
    marker = source_root / "vault" / "notes" / "aa" / "marker.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rollback", encoding="utf-8")
    exported = vaults.export_vault("rollback-source", confirm=True)
    preview = vaults.preview_import_vault_package(exported["package_path"])
    ctx = vaults._ctx()
    target_root = ctx.root / "data" / "vaults" / "rollback-import"

    def fail_write_config(config, ctx=None):
        raise RuntimeError("config write failed")

    monkeypatch.setattr(vaults, "_write_config", fail_write_config)
    with pytest.raises(RuntimeError, match="config write failed"):
        vaults.import_vault_package(
            exported["package_path"],
            target_name="Rollback Import",
            package_fingerprint_value=preview["package_fingerprint"],
            confirm=True,
        )

    assert not target_root.exists()
    config = yaml.safe_load(ctx.config_path.read_text(encoding="utf-8"))
    assert "rollback-import" not in config["vaults"]
    imports_tmp = ctx.root / ".tmp" / "imports"
    assert not imports_tmp.exists() or not any(imports_tmp.iterdir())


def test_vault_restore_rolls_back_on_config_write_failure(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    source = vaults.create_vault("Restore Rollback Source")
    source_root = Path(next(item["root"] for item in source["items"] if item["id"] == "restore-rollback-source"))
    marker = source_root / "vault" / "notes" / "aa" / "marker.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("restore rollback", encoding="utf-8")
    backup = vaults.backup_vault("restore-rollback-source", confirm=True)
    preview = vaults.preview_restore_backup_package(backup["package_path"])
    ctx = vaults._ctx()
    target_root = ctx.root / "data" / "vaults" / "restored-restore-rollback-source"

    def fail_write_config(config, ctx=None):
        raise RuntimeError("config write failed")

    monkeypatch.setattr(vaults, "_write_config", fail_write_config)
    with pytest.raises(RuntimeError, match="config write failed"):
        vaults.restore_backup_package(
            backup["package_path"],
            package_fingerprint_value=preview["package_fingerprint"],
            confirm=True,
        )

    assert not target_root.exists()
    config = yaml.safe_load(ctx.config_path.read_text(encoding="utf-8"))
    assert "restored-restore-rollback-source" not in config["vaults"]
    restores_tmp = ctx.root / ".tmp" / "restores"
    assert not restores_tmp.exists() or not any(restores_tmp.iterdir())


def test_vault_import_rolls_back_on_final_move_failure(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    source = vaults.create_vault("Move Failure Source")
    source_root = Path(next(item["root"] for item in source["items"] if item["id"] == "move-failure-source"))
    marker = source_root / "vault" / "notes" / "aa" / "marker.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("move failure", encoding="utf-8")
    exported = vaults.export_vault("move-failure-source", confirm=True)
    preview = vaults.preview_import_vault_package(exported["package_path"])
    ctx = vaults._ctx()
    target_root = ctx.root / "data" / "vaults" / "move-failure-import"

    def fail_rename(stage_root, final_root):
        final_root.mkdir(parents=True)
        (final_root / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("final move failed")

    monkeypatch.setattr(vaults, "_rename_import_stage", fail_rename)
    with pytest.raises(RuntimeError, match="final move failed"):
        vaults.import_vault_package(
            exported["package_path"],
            target_name="Move Failure Import",
            package_fingerprint_value=preview["package_fingerprint"],
            confirm=True,
        )

    assert not target_root.exists()
    config = yaml.safe_load(ctx.config_path.read_text(encoding="utf-8"))
    assert "move-failure-import" not in config["vaults"]
    imports_tmp = ctx.root / ".tmp" / "imports"
    assert not imports_tmp.exists() or not any(imports_tmp.iterdir())


def test_active_workspace_and_vault_switches_are_preflight_guarded(monkeypatch, tmp_path):
    api, vaults, workspaces = fresh_backend(monkeypatch, tmp_path, "api", "vaults", "workspaces")
    registry_path = tmp_path / "workspaces.yaml"
    monkeypatch.setattr(workspaces, "REGISTRY_PATH", registry_path)
    workspaces.save_workspace_registry({
        "schema_version": 1,
        "active_workspace": "default",
        "workspaces": {
            "default": {"name": "Default", "config_path": str(tmp_path / "mock-vault" / "config.yaml")},
        },
    })
    workspace_parent = (Path(tempfile.gettempdir()) / f"lmz-switch-guard-test-{time.time_ns()}").resolve()
    try:
        created = api.runtime._create_vault_sync({"name": "Guard Target"})
        assert any(item["id"] == "guard-target" for item in created["items"])
        added = api.runtime._create_workspace_sync({"path": str(workspace_parent), "name": "Guard Workspace"})
        workspace_id = next(item["id"] for item in added["items"] if item["name"] == "Guard Workspace")

        ctx = api.common.get_runtime_context()
        with api.common.local_ingest_lock(ctx):
            api.common.local_ingest_state(ctx)["running"] = True
        try:
            blocker = api.runtime._runtime_switch_blocker()
            assert blocker is not None
            assert blocker.status_code == 409
            payload = json.loads(blocker.body.decode("utf-8"))
            assert payload["detail"] == "Runtime switch blocked"
            assert "local_ingest_running" in payload["blockers"]

            with pytest.raises(HTTPException) as vault_exc:
                api.runtime._set_vault_active_sync({"id": "guard-target"})
            assert vault_exc.value.status_code == 409
            assert "local_ingest_running" in vault_exc.value.detail["blockers"]

            with pytest.raises(HTTPException) as workspace_exc:
                api.runtime._set_workspace_active_sync({"id": workspace_id})
            assert workspace_exc.value.status_code == 409
            assert "local_ingest_running" in workspace_exc.value.detail["blockers"]
        finally:
            with api.common.local_ingest_lock(ctx):
                api.common.local_ingest_state(ctx)["running"] = False

        assert api.ingestion.INGESTION_LOCK.acquire(blocking=False)
        try:
            blocker = api.runtime._runtime_switch_blocker()
            payload = json.loads(blocker.body.decode("utf-8"))
            assert blocker.status_code == 409
            assert "online_ingest_running" in payload["blockers"]
        finally:
            api.ingestion.INGESTION_LOCK.release()

        metadata_index = importlib.import_module("metadata_index")
        state = metadata_index._runtime_state(ctx)
        with state.repair_lock:
            state.repair_running = True
        try:
            blocker = api.runtime._runtime_switch_blocker()
            payload = json.loads(blocker.body.decode("utf-8"))
            assert blocker.status_code == 409
            assert "metadata_repair_running" in payload["blockers"]
        finally:
            with state.repair_lock:
                state.repair_running = False
    finally:
        shutil.rmtree(workspace_parent, ignore_errors=True)


def test_create_merged_vault_skips_exact_duplicates_and_keeps_sources(monkeypatch, tmp_path):
    vaults, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "vaults", "db.sqlite_operator")
    vaults.create_vault("Source A")
    vaults.create_vault("Source B")
    items = {item["id"]: item for item in vaults.vault_list()}
    source_a_root = Path(items["source-a"]["root"])
    source_b_root = Path(items["source-b"]["root"])

    def add_item(vault_item, root: Path, item_hash: str, storage_id: str, filename: str):
        conn = sqlite_operator.init_database(Path(vault_item["db_path"]))
        conn.execute(
            """
            INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
            VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:00', '', '', 'Local', 'Artist', '')
            """,
            (item_hash, storage_id, filename),
        )
        conn.commit()
        conn.close()
        asset = root / "vault" / "assets" / item_hash[:2] / f"{storage_id}.jpg"
        note = root / "vault" / "notes" / item_hash[:2] / f"{storage_id}.md"
        asset.parent.mkdir(parents=True, exist_ok=True)
        note.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(filename.encode("utf-8"))
        note.write_text(f"---\nstorage_id: {storage_id}\n---\n", encoding="utf-8")

    duplicate_hash = "cd" * 32
    unique_hash = "ef" * 32
    add_item(items["source-a"], source_a_root, duplicate_hash, "source-a-old", "duplicate-a.jpg")
    add_item(items["source-b"], source_b_root, duplicate_hash, "source-b-old", "duplicate-b.jpg")
    add_item(items["source-b"], source_b_root, unique_hash, "source-b-unique", "unique-b.jpg")

    preview = vaults.preview_merged_vault("Merged Result", ["source-a", "source-b"])
    assert preview["total_items"] == 3
    assert preview["duplicates"] == 1
    assert preview["importable"] == 2
    assert preview["possible_similar"] == 0
    assert preview["similarity"] == "unsupported"

    result = vaults.merge_vaults_to_new("Merged Result", ["source-a", "source-b"])
    merged = {item["id"]: item for item in vaults.vault_list()}["merged-result"]
    merged_conn = sqlite3.connect(merged["db_path"])
    source_a_conn = sqlite3.connect(items["source-a"]["db_path"])
    source_b_conn = sqlite3.connect(items["source-b"]["db_path"])
    try:
        merged_rows = merged_conn.execute("SELECT hash, storage_id FROM items ORDER BY hash").fetchall()
        assert len(merged_rows) == 2
        assert {row[0] for row in merged_rows} == {duplicate_hash, unique_hash}
        assert all(row[1] not in {"source-a-old", "source-b-old", "source-b-unique"} for row in merged_rows)
        assert source_a_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert source_b_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    finally:
        merged_conn.close()
        source_a_conn.close()
        source_b_conn.close()

    assert result["status"] == "success"
    assert result["imported"] == 2
    assert result["skipped"] == 1
    assert source_a_root.exists()
    assert source_b_root.exists()


def test_create_merged_vault_rolls_back_new_vault_on_copy_failure(monkeypatch, tmp_path):
    vaults, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "vaults", "db.sqlite_operator")
    vaults.create_vault("Source A")
    vaults.create_vault("Source B")
    items = {item["id"]: item for item in vaults.vault_list()}
    item_hash = "12" * 32
    storage_id = "source-a-old"
    source_root = Path(items["source-a"]["root"])
    conn = sqlite_operator.init_database(Path(items["source-a"]["db_path"]))
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, 'source.jpg', '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:00', '', '', 'Local', 'Artist', '')
        """,
        (item_hash, storage_id),
    )
    conn.commit()
    conn.close()
    asset = source_root / "vault" / "assets" / item_hash[:2] / f"{storage_id}.jpg"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")

    def fail_copy(source, target):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(vaults, "_copy_if_exists", fail_copy)
    with pytest.raises(RuntimeError, match="copy failed"):
        vaults.merge_vaults_to_new("Merged Failure", ["source-a", "source-b"])

    current = {item["id"]: item for item in vaults.vault_list()}
    assert "merged-failure" not in current


def test_app_settings_accessor_returns_fresh_secret_free_documents(monkeypatch, tmp_path):
    (utils,) = fresh_backend(monkeypatch, tmp_path, "utils")
    first = utils.get_app_settings()
    first["ui"]["vault_layout_mode"] = "grid"
    second = utils.get_app_settings()

    assert second["ui"]["vault_layout_mode"] == "masonry"
    assert "external_tools" not in second
    assert "secrets" not in second
    assert "models_dir" not in second


def test_queue_service_uses_mock_vault_queues(monkeypatch, tmp_path):
    queue_service, utils = fresh_backend(monkeypatch, tmp_path, "queue_service", "utils")

    assert queue_service.queue_counts() == {"normal": 1, "force": 1, "failed": 1}
    moved = queue_service.move_failed_urls("normal")
    assert moved == 1
    assert queue_service.queue_counts() == {"normal": 2, "force": 1, "failed": 0}
    assert queue_service.queue_path("normal").is_relative_to(utils.QUEUES_DIR)


def test_queue_parser_groups_artist_platform_and_warnings(monkeypatch, tmp_path):
    queue_service, = fresh_backend(monkeypatch, tmp_path, "queue_service")

    parsed = queue_service.parse_queue_preview("""# pixiv artist
@artist: Alex Flores
# comment inside group
@platform: pixiv
https://site.test/a
https://site.test/b

---

@artist angel master
https://site.test/c # inline comment
@unknown value
random text
https://site.test/d
""")

    assert parsed["count"] == 3
    assert parsed["entries"][0]["artist"] == "Alex Flores"
    assert parsed["entries"][0]["platform"] == "pixiv"
    assert parsed["entries"][2]["artist"] == ""
    assert [group["url_count"] for group in parsed["groups"]] == [2, 1]
    assert {warning["code"] for warning in parsed["warnings"]} == {
        "inline_comment",
        "unknown_directive",
        "ignored_line",
    }


def test_queue_parser_rewrites_remaining_entries_with_metadata(monkeypatch, tmp_path):
    queue_service, external_ingestion = fresh_backend(monkeypatch, tmp_path, "queue_service", "external_ingestion")
    path = tmp_path / "links.md"
    entry_one = queue_service.QueueEntry("https://site.test/a", "Alex Flores", "pixiv", 0, 4)
    entry_two = queue_service.QueueEntry("https://site.test/b", "angel master", "", 1, 9)
    ingestor = external_ingestion.ExternalIngestor(str(path))

    ingestor._write_back([entry_two, entry_one])

    assert path.read_text(encoding="utf-8") == (
        "# Remaining links for LMZ Ingestion\n"
        "@artist: Alex Flores\n"
        "@platform: pixiv\n"
        "https://site.test/a\n"
        "\n---\n\n"
        "@artist: angel master\n"
        "https://site.test/b\n"
    )


def test_queue_parse_api_enriches_artist_preview_status(monkeypatch, tmp_path):
    artists_module, ingestion_api = fresh_backend(monkeypatch, tmp_path, "artists", "api.ingestion")
    conn = ingestion_api.connect_workspace_database()
    try:
        artists_module.resolve_artist_name(conn, "Known Artist", create=True)
        conn.commit()
    finally:
        conn.close()

    parsed = ingestion_api._parse_queue_content_sync(
        "normal",
        ingestion_api.QueueUpdate(content="""@artist: Known Artist
https://site.test/a
---
@artist: New Artist
https://site.test/b
---
https://site.test/c
"""),
    )

    assert [group["artist_status"] for group in parsed["groups"]] == ["existing", "new", "unknown"]
    assert [group["artist_label"] for group in parsed["groups"]] == ["Known Artist", "New Artist", ""]


def test_review_cleanup_state_and_orphan_sidecar(monkeypatch, tmp_path):
    api, utils = fresh_backend(monkeypatch, tmp_path, "api", "utils")

    assert api.common._normalize_review_state("cleanup_failed") == "pending_cleanup"
    orphan = utils.REVIEW_DIR / "orphan.jpg.json"
    orphan.write_text('{"state":"resolved_delete"}', encoding="utf-8")

    result = api.review._cleanup_review_resolved_sync()

    assert result["cleaned_orphans"] == 1
    assert not orphan.exists()


def test_api_startup_hydrates_search_manager(monkeypatch, tmp_path):
    runtime_context, api = fresh_backend(monkeypatch, tmp_path, "runtime_context", "api")
    runtime_context.reload_runtime_context()
    calls = []

    class FakeConnection:
        def close(self):
            calls.append("close")

    class FakeSearchManager:
        def hydrate(self, conn):
            calls.append(("hydrate", conn))

    fake_conn = FakeConnection()
    monkeypatch.setattr(api.app, "init_database", lambda: fake_conn)
    monkeypatch.setattr(api.app, "search_manager", FakeSearchManager())

    asyncio.run(api.app.startup_search_index())

    assert calls == [("hydrate", fake_conn), "close"]


def test_review_listing_does_not_auto_resolve_pending_db_hash(monkeypatch, tmp_path):
    api, utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "api", "utils", "db.sqlite_operator")
    item_hash = "1" * 64
    conn = insert_mock_item(sqlite_operator, item_hash)
    conn.close()

    review_file = utils.REVIEW_DIR / "pending_same_hash.jpg"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_bytes(b"review")
    sidecar = review_file.with_suffix(".jpg.json")
    sidecar.write_text(json.dumps({"state": "pending", "file_hash": item_hash, "original_name": "pending_same_hash.jpg"}), encoding="utf-8")

    items = api.review._get_review_items_sync()
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    target = next(item for item in items if item["filename"] == review_file.name)

    assert target["state"] == "pending"
    assert saved["state"] == "pending"
    assert "last_action" not in saved


def test_review_count_uses_cache_without_full_resolver(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")

    def fail_resolver():
        raise AssertionError("review count should not resolve all review entries")

    monkeypatch.setattr(api.review, "_resolve_review_entries", fail_resolver)
    count = api.review._get_review_count_sync(include_resolved=True)

    assert count["pending"] >= 1
    assert count["cleanup"] >= 1
    assert count["total"] >= count["pending"] + count["cleanup"]


def test_review_count_cache_ignores_resolved_variant(monkeypatch, tmp_path):
    api, utils = fresh_backend(monkeypatch, tmp_path, "api", "utils")
    resolved_file = utils.REVIEW_DIR / "resolved-variant.webp"
    resolved_file.write_bytes(b"resolved")
    resolved_file.with_suffix(".webp.json").write_text(
        json.dumps({"state": "resolved_variant", "file_hash": "12" * 32}),
        encoding="utf-8",
    )
    api.common.mark_review_cache_dirty()

    count = api.review._get_review_count_sync(include_resolved=True)
    items = api.review._get_review_items_sync(False)

    assert len([item for item in items if item["filename"] == resolved_file.name]) == 1
    assert count["pending"] == 1
    assert count["cleanup"] == 2


def test_delete_item_removes_ram_indexes(monkeypatch, tmp_path):
    sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "api")
    item_hash = "9a" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    conn.execute(
        "UPDATE items SET source_url = ?, source_url_norm = ? WHERE hash = ?",
        ("https://example.test/delete", sqlite_operator.normalize_source_url("https://example.test/delete"), item_hash),
    )
    conn.commit()
    conn.close()
    removed = []

    class FakeSearchManager:
        def remove_indexes_batch(self, items):
            removed.extend(items)

    monkeypatch.setattr(api.library, "search_manager", FakeSearchManager())

    result = api.library._delete_item_sync(item_hash)

    assert result["status"] == "success"
    assert removed == [{"hash": item_hash, "source_url": "https://example.test/delete"}]


def test_local_ingest_state_guards_and_result_cap(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")

    api.ingestion._prepare_local_ingest_run("run-1", {"artist": "A"}, True)
    with pytest.raises(HTTPException) as exc:
        api.ingestion._prepare_local_ingest_run("run-2", {}, False)
    assert exc.value.status_code == 409

    with api.common.LOCAL_INGEST_LOCK:
        api.common.LOCAL_INGEST_STATE["running"] = False
        api.common.LOCAL_INGEST_STATE["results"] = []
        for index in range(505):
            api.ingestion._append_local_ingest_result({"index": index})

    assert len(api.common.LOCAL_INGEST_STATE["results"]) == 500
    assert api.common.LOCAL_INGEST_STATE["results"][0]["index"] == 5
    assert api.common.LOCAL_INGEST_STATE["last_defaults"] == {"artist": "A"}
    assert api.common.LOCAL_INGEST_STATE["last_skip_similarity"] is True


def test_local_retry_preserves_defaults_and_skip_similarity(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    calls = []

    def fake_worker(paths, defaults, skip_similarity, run_id):
        calls.append((paths, defaults, skip_similarity, run_id))

    monkeypatch.setattr(api.ingestion, "_run_local_ingest_worker", fake_worker)
    with api.common.LOCAL_INGEST_LOCK:
        api.common.LOCAL_INGEST_STATE["running"] = False
        api.common.LOCAL_INGEST_STATE["failed_paths"] = ["failed-a.jpg"]
        api.common.LOCAL_INGEST_STATE["last_defaults"] = {"artist": "Retry Artist"}
        api.common.LOCAL_INGEST_STATE["last_skip_similarity"] = True

    result = asyncio.run(api.ingestion.local_ingest_retry_failed())

    assert result["status"] == "success"
    assert result["queued"] == 1
    assert calls[0][0] == ["failed-a.jpg"]
    assert calls[0][1] == {"artist": "Retry Artist", "platform": "Local", "source_url": ""}
    assert calls[0][2] is True


def test_local_worker_reports_wd_tagging_status_for_started_paths(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    source = tmp_path / "drop_ok.jpg"
    source.write_bytes(b"fake image")

    def fake_process_file(path, config, metadata=None, delete_source=False, skip_similarity=False, **kwargs):
        if delete_source:
            Path(path).unlink()
        return True, "Success: drop_ok.jpg -> item.jpg", {
            "file_hash": "7" * 64,
            "tagging_status": "ok",
            "tagging_tag_count": 12,
            "tagging_error": "",
        }

    monkeypatch.setattr(api.ingestion, "process_file", fake_process_file)
    monkeypatch.setattr(
        api.ingestion,
        "get_app_settings",
        lambda: {"ingestion": {"accepted_media": {"extensions": ["jpg"]}}},
    )

    api.ingestion._prepare_local_ingest_run("run-tags", {}, False, 1)
    api.ingestion._run_local_ingest_worker([str(source)], {}, False, "run-tags")

    result = api.ingestion._snapshot_local_ingest_state()["results"][-1]

    assert result["status"] == "ingested"
    assert "WD tags: ok (12)" in result["message"]


def test_local_ingest_expansion_is_streaming_not_sorted(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    source = inspect.getsource(api.ingestion._iter_local_ingest_paths)

    assert "sorted(" not in source
    assert ".rglob(\"*\")" in source


def test_local_drop_intake_accepts_supported_file_and_directory(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    sample_file = tmp_path / "drop_ok.jpg"
    sample_file.write_bytes(b"ok")
    sample_dir = tmp_path / "drop_dir"
    sample_dir.mkdir(parents=True, exist_ok=True)

    payload = api.ingestion.LocalIngestDropIntakeRequest(
        session_id="s1",
        source_tab="vault",
        paths=[str(sample_file), str(sample_dir)],
    )
    result = api.ingestion._local_drop_intake_sync(payload)

    assert result["session_id"] == "s1"
    assert result["summary"]["received"] == 2
    assert result["summary"]["accepted"] == 2
    assert result["summary"]["skipped"] == 0
    assert str(sample_file.resolve()) in result["accepted_paths"]
    assert str(sample_dir.resolve()) in result["accepted_paths"]


def test_local_drop_intake_skips_unsupported_extension_and_missing(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    bad_file = tmp_path / "drop_bad.txt"
    bad_file.write_text("x", encoding="utf-8")
    missing = tmp_path / "nope.jpg"

    payload = api.ingestion.LocalIngestDropIntakeRequest(
        session_id="s2",
        source_tab="vault",
        paths=[str(bad_file), str(missing)],
    )
    result = api.ingestion._local_drop_intake_sync(payload)
    reasons = {entry["reason"] for entry in result["skipped"]}

    assert result["summary"]["accepted"] == 0
    assert result["summary"]["skipped"] == 2
    assert "unsupported_extension" in reasons
    assert "missing_path" in reasons


def test_local_drop_intake_dedupes_paths(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    sample_file = tmp_path / "dup_ok.jpg"
    sample_file.write_bytes(b"dup")

    payload = api.ingestion.LocalIngestDropIntakeRequest(
        session_id="s3",
        source_tab="vault",
        paths=[str(sample_file), str(sample_file)],
    )
    result = api.ingestion._local_drop_intake_sync(payload)

    assert result["summary"]["accepted"] == 1
    assert result["summary"]["skipped"] == 1
    assert result["skipped"][0]["reason"] == "duplicate_path"


def test_local_drop_intake_blocks_when_local_ingest_running(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    with api.common.LOCAL_INGEST_LOCK:
        api.common.LOCAL_INGEST_STATE["running"] = True
    try:
        payload = api.ingestion.LocalIngestDropIntakeRequest(session_id="s4", source_tab="vault", paths=["C:/tmp/a.jpg"])
        with pytest.raises(HTTPException) as exc:
            api.ingestion._local_drop_intake_sync(payload)
        assert exc.value.status_code == 409
    finally:
        with api.common.LOCAL_INGEST_LOCK:
            api.common.LOCAL_INGEST_STATE["running"] = False


def test_local_drop_intake_blocks_when_online_ingest_running(monkeypatch, tmp_path):
    (api,) = fresh_backend(monkeypatch, tmp_path, "api")
    acquired = api.ingestion.INGESTION_LOCK.acquire(blocking=False)
    assert acquired is True
    try:
        payload = api.ingestion.LocalIngestDropIntakeRequest(session_id="s5", source_tab="vault", paths=["C:/tmp/a.jpg"])
        with pytest.raises(HTTPException) as exc:
            api.ingestion._local_drop_intake_sync(payload)
        assert exc.value.status_code == 409
    finally:
        if acquired:
            api.ingestion.INGESTION_LOCK.release()


def test_mime_detection_falls_back_when_magic_returns_error_text(monkeypatch, tmp_path):
    class FakeMagic:
        @staticmethod
        def from_file(path, mime=True):
            return f"cannot open `{path}' (no such file or directory)"

    monkeypatch.setitem(sys.modules, "magic", FakeMagic)
    (validators,) = fresh_backend(monkeypatch, tmp_path, "validators")

    sample = tmp_path / "WhatsApp Görsel 2024.jpg"
    sample.write_bytes(b"not actually jpeg")

    assert validators.get_mime_type(sample) == "image/jpeg"


def test_generate_markdown_mirrors_sqlite_identity_and_preserves_indexed_note_fields(monkeypatch, tmp_path):
    utils, sqlite_operator, md_generator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "md_generator")
    item_hash = "d" * 64
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\n"
        "artist: Manual Artist\n"
        "date_added: '2020-01-01 01:02:03'\n"
        "topics:\n  - manual-topic\n"
        "wd_rating: ''\n"
        "wd_character_tags: []\n"
        "wd_tags: []\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    cache_path = utils.wd_tag_cache_path_for(item_hash, storage_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"hash": item_hash, "status": "ok", "rating": {"label": "safe"}, "character_tags": [{"name": "cached_character"}], "tags": [{"name": "cached_tag"}]}),
        encoding="utf-8",
    )

    data = frontmatter_from_markdown(md_generator.generate_markdown(conn, item_hash))

    assert data["artist"] == "DB Artist"
    assert data["date_added"] == "2026-01-02 03:04:05"
    assert data["topics"] == ["manual-topic"]
    assert data["wd_rating"] == ""
    assert data["wd_character_tags"] == []
    assert data["wd_tags"] == []
    conn.close()


def test_generate_markdown_seeds_missing_wd_fields_from_cache(monkeypatch, tmp_path):
    utils, sqlite_operator, md_generator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "md_generator")
    item_hash = "e" * 64
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    cache_path = utils.wd_tag_cache_path_for(item_hash, storage_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"hash": item_hash, "status": "ok", "rating": {"label": "safe"}, "character_tags": [{"name": "cached_character", "display_name": "Cached Character"}], "tags": [{"name": "cached_tag", "display_name": "Cached Tag"}]}),
        encoding="utf-8",
    )

    data = frontmatter_from_markdown(md_generator.generate_markdown(conn, item_hash))

    assert data["wd_rating"] == "safe"
    assert data["wd_character_tags"] == ["Cached Character"]
    assert data["wd_tags"] == ["Cached Tag"]
    conn.close()


def test_generate_markdown_overwrites_existing_fields_if_forced(monkeypatch, tmp_path):
    utils, sqlite_operator, md_generator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "md_generator")
    item_hash = "f" * 64
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    
    note_path = utils.note_path_for(item_hash, storage_id)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\n"
        "artist: DB Artist\n"
        "date_added: '2026-01-02 03:04:05'\n"
        "topics: []\n"
        "wd_rating: 'old_rating'\n"
        "wd_character_tags: ['old_character']\n"
        "wd_tags: ['old_tag']\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    
    cache_path = utils.wd_tag_cache_path_for(item_hash, storage_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "hash": item_hash,
            "status": "ok",
            "rating": {"label": "new_rating"},
            "character_tags": [{"name": "new_character", "display_name": "New Character"}],
            "tags": [{"name": "new_tag", "display_name": "New Tag"}]
        }),
        encoding="utf-8",
    )

    data = frontmatter_from_markdown(md_generator.generate_markdown(conn, item_hash))
    assert data["wd_rating"] == "old_rating"
    assert data["wd_character_tags"] == ["old_character"]
    assert data["wd_tags"] == ["old_tag"]

    data_forced = frontmatter_from_markdown(md_generator.generate_markdown(conn, item_hash, force_wd_from_cache=True))
    assert data_forced["wd_rating"] == "new_rating"
    assert data_forced["wd_character_tags"] == ["New Character"]
    assert data_forced["wd_tags"] == ["New Tag"]
    
    conn.close()


def test_reindex_does_not_sync_markdown_identity_fields_to_sqlite(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    item_hash = "f" * 64
    conn = insert_mock_item(sqlite_operator, item_hash, artist="DB Artist", date_added="2026-01-01 00:00:00")
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\nartist: Manual Artist\ndate_added: '2020-01-01 01:02:03'\ntopics: []\n---\n",
        encoding="utf-8",
    )

    metadata_index.reindex_item_metadata(conn, item_hash)
    conn.commit()
    row = conn.execute("SELECT source_artist, date_added FROM items WHERE hash = ?", (item_hash,)).fetchone()

    assert row == ("DB Artist", "2026-01-01 00:00:00")
    conn.close()


def test_patch_rolls_back_db_when_markdown_write_fails(monkeypatch, tmp_path):
    utils, sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api")
    item_hash = "1" * 64
    conn = insert_mock_item(sqlite_operator, item_hash, artist="Original Artist")
    storage_id = storage_id_for(conn, item_hash)
    utils.note_path_for(item_hash, storage_id).parent.mkdir(parents=True, exist_ok=True)
    utils.note_path_for(item_hash, storage_id).write_text("---\nartist: Original Artist\ntopics: []\n---\n", encoding="utf-8")
    conn.close()

    def fail_write(path, text, encoding="utf-8"):
        raise OSError("disk full")

    monkeypatch.setattr(api.library, "atomic_write_text", fail_write)
    with pytest.raises(OSError):
        api.library._update_item_sync(item_hash, api.library.ItemUpdate(artist="New Artist"))

    conn = sqlite_operator.init_database()
    artist = conn.execute("SELECT source_artist FROM items WHERE hash = ?", (item_hash,)).fetchone()[0]
    assert artist == "Original Artist"
    conn.close()


def test_processor_uses_atomic_markdown_writes(monkeypatch, tmp_path):
    (processor,) = fresh_backend(monkeypatch, tmp_path, "processor")

    source = inspect.getsource(processor.process_file)
    assert "atomic_write_text(md_path, md_content)" in source
    assert "with open(md_path" not in source


def test_processor_skips_file_already_pending_review(monkeypatch, tmp_path):
    utils, processor = fresh_backend(monkeypatch, tmp_path, "utils", "processor")
    source = tmp_path / "pending.webp"
    source.write_bytes(b"pending review bytes")
    file_hash = utils.calculate_file_hash(source)
    review_file = utils.REVIEW_DIR / "queued.webp"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_bytes(b"existing review bytes")
    review_file.with_suffix(".webp.json").write_text(
        json.dumps({"state": "pending", "file_hash": file_hash, "original_name": "pending.webp"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/webp")

    ok, message, idx_data = processor.process_file(
        source,
        app_settings_config(["image/webp"], ["webp"]),
    )

    assert not ok
    assert message.startswith("Already pending review")
    assert idx_data is None


def test_processor_can_bypass_pending_review_guard_for_review_action(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    source = tmp_path / "pending.webp"
    source.write_bytes(b"pending review bytes")
    file_hash = utils.calculate_file_hash(source)
    review_file = utils.REVIEW_DIR / "queued.webp"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_bytes(b"existing review bytes")
    review_file.with_suffix(".webp.json").write_text(
        json.dumps({"state": "pending", "file_hash": file_hash, "original_name": "pending.webp"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/webp")
    monkeypatch.setattr(processor, "calculate_phash", lambda path: None)

    ok, message, idx_data = processor.process_file(
        source,
        app_settings_config(["image/webp"], ["webp"]),
        allow_pending_review=True,
    )

    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (file_hash,)).fetchone()
    conn.close()

    assert ok, message
    assert idx_data["file_hash"] == file_hash
    assert row is not None


def test_pending_review_match_uses_cache_without_sidecar_glob(monkeypatch, tmp_path):
    utils, processor = fresh_backend(monkeypatch, tmp_path, "utils", "processor")
    review_hash = "ab" * 32
    review_file = utils.REVIEW_DIR / "cached.webp"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_bytes(b"existing review bytes")
    review_file.with_suffix(".webp.json").write_text(
        json.dumps({"state": "pending", "file_hash": review_hash, "original_name": "cached.webp"}),
        encoding="utf-8",
    )

    class FakeReviewDir:
        def glob(self, *args, **kwargs):
            raise AssertionError("pending review match should not glob sidecars")

    monkeypatch.setattr(processor, "REVIEW_DIR", FakeReviewDir(), raising=False)

    assert processor._pending_review_match(review_hash)["original_name"] == "cached.webp"


def test_pending_review_guard_blocks_reingest_after_restart(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    existing_hash = "2" * 64
    reingest_source = tmp_path / "9ypbteld4je61.webp"
    reingest_source.write_bytes(b"same webp bytes")
    review_hash = utils.calculate_file_hash(reingest_source)
    conn = insert_mock_item(sqlite_operator, existing_hash)
    conn.close()

    review_file = utils.REVIEW_DIR / "pending_9ypbteld4je61.webp"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_bytes(b"same webp bytes")
    review_file.with_suffix(".webp.json").write_text(
        json.dumps({
            "state": "pending",
            "file_hash": review_hash,
            "phash": "deb02d123d1f218f",
            "best_match": existing_hash,
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/webp")

    ok, message, _ = processor.process_file(
        reingest_source,
        app_settings_config(["image/webp"], ["webp"]),
    )

    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (review_hash,)).fetchone()
    conn.close()

    assert not ok
    assert message.startswith("Already pending review")
    assert row is None


def test_ingest_seeds_markdown_artist_from_metadata_and_reindexes(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    item_hash = "5" * 64
    source = tmp_path / "source.jpg"
    source.write_bytes(b"fake image")

    class TagResult:
        status = "ok"
        error = ""

    monkeypatch.setattr(processor, "calculate_file_hash", lambda path: item_hash)
    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(processor, "calculate_phash", lambda path: None)
    monkeypatch.setattr(processor, "calculate_tiles", lambda path: [])
    monkeypatch.setattr(processor, "tag_media", lambda *args, **kwargs: TagResult())
    monkeypatch.setattr(processor, "_pending_review_match", lambda file_hash: None)

    ok, _, idx_data = processor.process_file(
        source,
        app_settings_config(["image/jpeg"], ["jpg"]),
        metadata={"artist": "Ingest Artist", "platform": "pixiv", "source_url": "https://example.test/item"},
        sync_index=False,
    )

    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT source_artist, storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
    note_data = frontmatter_from_markdown(utils.note_path_for(item_hash, row[1]).read_text(encoding="utf-8"))

    assert ok
    assert idx_data["file_hash"] == item_hash
    assert row[0] == "Ingest Artist"
    assert len(row[1]) == 12
    assert utils.storage_asset_path_for(item_hash, row[1], ".jpg", "image/jpeg").exists()
    assert utils.note_path_for(item_hash, row[1]).name == f"{row[1]}.md"
    assert note_data["artist"] == "Ingest Artist"
    conn.close()


def test_ingest_result_reports_wd_tagging_status(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    item_hash = "6" * 64
    source = tmp_path / "source.jpg"
    source.write_bytes(b"fake image")

    class TagResult:
        status = "ok"
        error = ""
        tags = [{"name": "tag-a"}]

    monkeypatch.setattr(processor, "calculate_file_hash", lambda path: item_hash)
    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(processor, "calculate_phash", lambda path: None)
    monkeypatch.setattr(processor, "calculate_tiles", lambda path: [])
    monkeypatch.setattr(processor, "tag_media", lambda *args, **kwargs: TagResult())
    monkeypatch.setattr(processor, "_pending_review_match", lambda file_hash: None)

    ok, _, idx_data = processor.process_file(
        source,
        app_settings_config(["image/jpeg"], ["jpg"]),
        metadata={"artist": "Ingest Artist", "platform": "local", "source_url": ""},
        sync_index=False,
    )

    conn = sqlite_operator.init_database()
    storage_id = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()[0]
    conn.close()

    assert ok
    assert idx_data["tagging_status"] == "ok"
    assert idx_data["tagging_tag_count"] == 1
    assert utils.note_path_for(item_hash, storage_id).exists()


def test_config_allowed_non_media_ingest_marks_thumbnail_skipped(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    source = tmp_path / "document.txt"
    source.write_text("document", encoding="utf-8")

    class TagResult:
        status = "skipped"
        error = "unsupported media type"
        tags = []

    monkeypatch.setattr(processor, "get_mime_type", lambda path: "text/plain")
    monkeypatch.setattr(processor, "tag_media", lambda *args, **kwargs: TagResult())
    monkeypatch.setattr(processor, "ensure_thumbnail", lambda *args, **kwargs: pytest.fail("non-media thumbnail generated"))

    ok, message, result = processor.process_file(
        source,
        app_settings_config(["text/plain"], ["txt"]),
        sync_index=False,
    )

    assert ok, message
    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT thumbnail_status FROM items WHERE hash = ?", (result["file_hash"],)).fetchone()
    conn.close()
    assert row == ("skipped",)


def test_ingest_index_update_ignores_wd_tagging_status(monkeypatch, tmp_path):
    utils, sqlite_operator, processor = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor")
    item_hash = "7" * 64
    source = tmp_path / "source.jpg"
    source.write_bytes(b"fake image")
    index_calls = []

    class TagResult:
        status = "ok"
        error = ""
        tags = [{"name": "tag-a"}]

    monkeypatch.setattr(processor, "calculate_file_hash", lambda path: item_hash)
    monkeypatch.setattr(processor, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(processor, "calculate_phash", lambda path: None)
    monkeypatch.setattr(processor, "calculate_tiles", lambda path: [])
    monkeypatch.setattr(processor, "tag_media", lambda *args, **kwargs: TagResult())
    monkeypatch.setattr(processor, "_pending_review_match", lambda file_hash: None)
    monkeypatch.setattr(processor.search_manager, "update_indexes", lambda **kwargs: index_calls.append(kwargs))

    ok, _, idx_data = processor.process_file(
        source,
        app_settings_config(["image/jpeg"], ["jpg"]),
        metadata={"artist": "Ingest Artist", "platform": "local", "source_url": ""},
    )

    assert ok
    assert idx_data["tagging_status"] == "ok"
    assert index_calls
    assert "tagging_status" not in index_calls[0]
    assert "tagging_error" not in index_calls[0]
    assert "tagging_tag_count" not in index_calls[0]


def test_storage_id_backfill_and_compact_asset_path(monkeypatch, tmp_path):
    utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator")
    item_hash = "a1" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    conn.close()

    conn = sqlite_operator.init_database()
    storage_id = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()[0]

    assert len(storage_id) == 12
    assert utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id).name == f"{storage_id}.jpg"
    conn.close()


def test_storage_id_allocation_does_not_rescan_counter(monkeypatch, tmp_path):
    sqlite_operator, = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator")
    conn = sqlite_operator.init_database()

    def fail_rescan(conn):
        raise AssertionError("_ensure_storage_counter should not run during allocation")

    monkeypatch.setattr(sqlite_operator, "_ensure_storage_counter", fail_rescan)
    storage_id = sqlite_operator.allocate_storage_id(conn)
    conn.close()

    assert len(storage_id) == 12


def test_manage_review_uses_quarantine_sidecar_for_artist(monkeypatch, tmp_path):
    (utils,) = fresh_backend(monkeypatch, tmp_path, "utils")
    tool_path = ROOT / "tools" / "maintenance" / "manage_review.py"
    spec = importlib.util.spec_from_file_location("manage_review_under_test", tool_path)
    manage_review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manage_review)
    review_file = utils.REVIEW_DIR / "queued.jpg"
    sidecar = review_file.with_suffix(".jpg.json")
    review_file.write_bytes(b"queued")
    sidecar.write_text(json.dumps({"metadata": {"artist": "Sidecar Artist"}}), encoding="utf-8")
    captured = {}

    def fake_process_file(file_path, config, metadata=None, delete_source=False, skip_similarity=False, **kwargs):
        captured["metadata"] = metadata
        if delete_source:
            file_path.unlink()
        return True, "ok", {}

    monkeypatch.setattr(manage_review, "get_app_settings", lambda: {})
    monkeypatch.setattr(manage_review, "process_file", fake_process_file)

    manage_review.approve_file("queued.jpg")

    assert captured["metadata"]["artist"] == "Sidecar Artist"
    assert not sidecar.exists()


def test_manual_metadata_migration_dry_run_and_apply(monkeypatch, tmp_path):
    utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator")
    tool_path = ROOT / "tools" / "maintenance" / "migrate_manual_metadata_to_markdown.py"
    spec = importlib.util.spec_from_file_location("migrate_manual_metadata_to_markdown", tool_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    item_hash = "2" * 64
    conn = insert_mock_item(sqlite_operator, item_hash, artist="DB Artist", date_added="2022-02-02 02:02:02")
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    note_path = utils.note_path_for(item_hash, storage_id)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text("---\ntopics: []\n---\nbody\n", encoding="utf-8")

    dry_run = migration.run(False)
    assert dry_run["changed"] == 1
    assert "artist" not in frontmatter_from_markdown(note_path.read_text(encoding="utf-8"))

    applied = migration.run(True)
    data = frontmatter_from_markdown(note_path.read_text(encoding="utf-8"))

    assert applied["changed"] == 1
    assert data["artist"] == "DB Artist"
    assert data["date_added"] == "2022-02-02 02:02:02"
    backup_path = Path(applied["backup"])
    assert backup_path.exists()
    assert backup_path.parent == utils.get_runtime_context().root / "backups"


def test_topic_normalize_backup_uses_workspace_root(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    normalize_topics = load_maintenance_tool("normalize_topics")

    backup_path = normalize_topics.backup_notes()

    assert backup_path.exists()
    assert backup_path.parent == utils.get_runtime_context().root / "backups"


def test_review_replace_preserves_old_sqlite_identity_and_manual_indexed_metadata(monkeypatch, tmp_path):
    utils, sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api")
    old_hash = "3" * 64
    new_hash = "4" * 64
    conn = insert_mock_item(sqlite_operator, old_hash, artist="Old DB Artist", date_added="2026-01-01 00:00:00")
    old_storage_id = storage_id_for(conn, old_hash)
    conn.close()
    old_note = utils.note_path_for(old_hash, old_storage_id)
    old_note.parent.mkdir(parents=True, exist_ok=True)
    old_note.write_text(
        "---\nartist: Manual Old\ndate_added: '2020-01-01 00:00:00'\ntopics:\n  - preserved\nwd_tags: []\n---\n",
        encoding="utf-8",
    )
    review_file = utils.REVIEW_DIR / "replacement.jpg"
    review_file.write_bytes(b"replacement")
    review_file.with_suffix(".jpg.json").write_text(json.dumps({"best_match": old_hash, "metadata": {"artist": "New Artist"}}), encoding="utf-8")

    def fake_process_file(path, config, metadata=None, delete_source=False, skip_similarity=False, sync_index=True, **kwargs):
        conn = insert_mock_item(sqlite_operator, new_hash, artist="New Artist", date_added="2026-02-02 00:00:00")
        new_storage_id = storage_id_for(conn, new_hash)
        md = api.common.generate_markdown(conn, new_hash)
        note_path = utils.note_path_for(new_hash, new_storage_id)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        utils.atomic_write_text(note_path, md)
        conn.close()
        if delete_source:
            path.unlink()
        return True, "ok", {"file_hash": new_hash}

    monkeypatch.setattr(api.review, "process_file", fake_process_file)
    monkeypatch.setattr(api.review, "_delete_item_after_replacement", lambda target_hash, **kwargs: {"hash": target_hash, "status": "deleted", "cleanup_errors": []})

    result = api.review._review_action_sync("replacement.jpg", "replace")
    conn = sqlite_operator.init_database()
    new_storage_id = storage_id_for(conn, new_hash)
    conn.close()
    new_data = frontmatter_from_markdown(utils.note_path_for(new_hash, new_storage_id).read_text(encoding="utf-8"))

    assert result["status"] == "success"
    assert new_data["artist"] == "Old DB Artist"
    assert new_data["date_added"] == "2026-01-01 00:00:00"
    assert new_data["topics"] == ["[preserved](../../../../../topics/preserved.md)"]
    assert new_data["wd_tags"] == []


def test_review_multi_match_and_safe_specific_replace(monkeypatch, tmp_path):
    utils, sqlite_operator, api, processor, api_review = fresh_backend(
        monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api", "processor", "api.review"
    )

    # 1. Test processor.find_visual_duplicate with return_all=True
    class FakeSearchManager:
        def query_image(self, phash, threshold, ctx=None):
            return [("hash-a", 2, "Global"), ("hash-b", 4, "Fragment-to-Whole")]

        def query_global_only(self, phash, threshold, ctx=None):
            return []

    monkeypatch.setattr(processor, "search_manager", FakeSearchManager())

    best, match_type, total, distance, all_hashes = processor.find_visual_duplicate("0" * 16, return_all=True)
    assert best == "hash-a"
    assert all_hashes == ["hash-a", "hash-b"]

    # 2. Test api.review._get_review_items_sync resolving multiple matches
    conn = insert_mock_item(sqlite_operator, "hash-a", artist="Artist A")
    insert_mock_item(sqlite_operator, "hash-b", artist="Artist B").close()
    conn.close()

    review_file = utils.REVIEW_DIR / "staged.jpg"
    review_file.write_bytes(b"staged")
    sidecar_data = {
        "best_match": "hash-a",
        "matches": ["hash-a", "hash-b"],
        "metadata": {"artist": "Staged Artist"}
    }
    review_file.with_suffix(".jpg.json").write_text(json.dumps(sidecar_data), encoding="utf-8")

    items = api_review._get_review_items_sync()
    staged_item = next(item for item in items if item["filename"] == "staged.jpg")
    matches = staged_item["matches"]
    assert len(matches) == 2
    assert matches[0]["hash"] == "hash-a"
    assert matches[0]["artist"] == "Artist A"
    assert matches[1]["hash"] == "hash-b"
    assert matches[1]["artist"] == "Artist B"

    # 3. Test safe specific replacement action
    def fake_process_file(path, config, metadata=None, delete_source=False, skip_similarity=False, **kwargs):
        conn = insert_mock_item(sqlite_operator, "new-hash", artist="Staged Artist")
        new_storage_id = storage_id_for(conn, "new-hash")
        md = api.common.generate_markdown(conn, "new-hash")
        note_path = utils.note_path_for("new-hash", new_storage_id)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        utils.atomic_write_text(note_path, md)
        conn.close()
        if delete_source:
            path.unlink()
        return True, "ok", {"file_hash": "new-hash"}

    deleted_targets = []
    def fake_delete_item(target_hash, **kwargs):
        deleted_targets.append(target_hash)
        return {"hash": target_hash, "status": "deleted", "cleanup_errors": []}

    monkeypatch.setattr(api_review, "process_file", fake_process_file)
    monkeypatch.setattr(api_review, "_delete_item_after_replacement", fake_delete_item)

    result = api_review._review_action_sync("staged.jpg", "replace", target_hash="hash-b")
    assert result["status"] == "success"
    assert deleted_targets == ["hash-b"]


def test_wd_tagger_reuses_cached_session(monkeypatch, tmp_path):
    service, utils = fresh_backend(monkeypatch, tmp_path, "tagging.service", "utils")
    source = tmp_path / "tagged.jpg"
    source.write_bytes(b"image")
    calls = {"sessions": 0}

    class FakeSession:
        def __init__(self, path, providers=None):
            calls["sessions"] += 1

        def get_inputs(self):
            return [types.SimpleNamespace(name="input", shape=[1, 448, 448, 3])]

        def get_outputs(self):
            return [types.SimpleNamespace(name="output")]

        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
        InferenceSession=FakeSession,
    )
    fake_hf = types.SimpleNamespace(hf_hub_download=lambda **kwargs: "")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setattr(service, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(service, "_ensure_model_files", lambda repo, hf: (tmp_path / "model.onnx", tmp_path / "tags.csv"))
    monkeypatch.setattr(service, "_load_labels", lambda path: [{"name": "safe", "category": "9"}])
    monkeypatch.setattr(service.Image, "open", lambda path: types.SimpleNamespace(seek=lambda frame: None))
    monkeypatch.setattr(service, "_predict_image_tags", lambda *args, **kwargs: ({"label": "safe"}, [], [{"name": "tag"}]))

    first = service.tag_media(source, item_hash="6" * 64, config={"tagging": {"device": "cpu"}}, storage_id="000000000001")
    second = service.tag_media(source, item_hash="7" * 64, config={"tagging": {"device": "cpu"}}, storage_id="000000000002")

    assert first.status == "ok"
    assert second.status == "ok"
    assert calls["sessions"] == 1
    assert utils.wd_tag_cache_path_for("6" * 64, "000000000001").exists()


def test_wd_tagger_concurrent_calls_initialize_session_once(monkeypatch, tmp_path):
    service, = fresh_backend(monkeypatch, tmp_path, "tagging.service")
    source = tmp_path / "tagged.jpg"
    source.write_bytes(b"image")
    calls = {"sessions": 0}

    class FakeSession:
        def __init__(self, path, providers=None):
            time.sleep(0.05)
            calls["sessions"] += 1

        def get_inputs(self):
            return [types.SimpleNamespace(name="input", shape=[1, 448, 448, 3])]

        def get_outputs(self):
            return [types.SimpleNamespace(name="output")]

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"], InferenceSession=FakeSession))
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=lambda **kwargs: ""))
    monkeypatch.setattr(service, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(service, "_ensure_model_files", lambda repo, hf: (tmp_path / "model.onnx", tmp_path / "tags.csv"))
    monkeypatch.setattr(service, "_load_labels", lambda path: [{"name": "safe", "category": "9"}])
    monkeypatch.setattr(service.Image, "open", lambda path: types.SimpleNamespace(seek=lambda frame: None))
    monkeypatch.setattr(service, "_predict_image_tags", lambda *args, **kwargs: ({"label": "safe"}, [], []))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda index: service.tag_media(source, item_hash=f"{index:064x}", config={"tagging": {"device": "cpu"}}), range(4)))

    assert [result.status for result in results] == ["ok", "ok", "ok", "ok"]
    assert calls["sessions"] == 1


def test_wd_tagger_failed_session_is_not_cached(monkeypatch, tmp_path):
    service, = fresh_backend(monkeypatch, tmp_path, "tagging.service")
    source = tmp_path / "tagged.jpg"
    source.write_bytes(b"image")
    calls = {"sessions": 0}

    class FakeSession:
        def __init__(self, path, providers=None):
            calls["sessions"] += 1
            if calls["sessions"] == 1:
                raise RuntimeError("load failed")

        def get_inputs(self):
            return [types.SimpleNamespace(name="input", shape=[1, 448, 448, 3])]

        def get_outputs(self):
            return [types.SimpleNamespace(name="output")]

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"], InferenceSession=FakeSession))
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=lambda **kwargs: ""))
    monkeypatch.setattr(service, "get_mime_type", lambda path: "image/jpeg")
    monkeypatch.setattr(service, "_ensure_model_files", lambda repo, hf: (tmp_path / "model.onnx", tmp_path / "tags.csv"))
    monkeypatch.setattr(service, "_load_labels", lambda path: [{"name": "safe", "category": "9"}])
    monkeypatch.setattr(service.Image, "open", lambda path: types.SimpleNamespace(seek=lambda frame: None))
    monkeypatch.setattr(service, "_predict_image_tags", lambda *args, **kwargs: ({"label": "safe"}, [], []))

    failed = service.tag_media(source, item_hash="8" * 64, config={"tagging": {"device": "cpu"}})
    recovered = service.tag_media(source, item_hash="9" * 64, config={"tagging": {"device": "cpu"}})

    assert failed.status == "failed"
    assert recovered.status == "ok"
    assert calls["sessions"] == 2


def test_stale_metadata_scan_streams_and_respects_limit(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")
    seen = {"rows": 0}

    class Cursor:
        def __iter__(self):
            for index in range(10):
                seen["rows"] += 1
                yield (str(index), f"{index:012}", None, "", "", 0, 0, "", 0, 0, "")

        def fetchall(self):
            raise AssertionError("fetchall must not be used")

    class Conn:
        def execute(self, *args, **kwargs):
            return Cursor()

    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)

    assert metadata_index.stale_metadata_hashes(Conn(), limit=3) == ["0", "1", "2"]
    assert seen["rows"] == 3


def test_metadata_dirty_queue_created_and_drained_before_scan(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    item_hash = "ab" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - queued-topic\n---\n")
    metadata_index.enqueue_metadata_dirty(conn, item_hash, "test")
    conn.commit()

    monkeypatch.setattr(
        metadata_index,
        "stale_metadata_hashes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale scan should not run while dirty queue has work")),
    )

    result = metadata_index.reindex_stale_metadata_batch(conn, limit=10)
    topics = [row[0] for row in conn.execute("SELECT topic FROM item_topics WHERE item_hash = ?", (item_hash,))]
    queued = conn.execute("SELECT COUNT(*) FROM metadata_dirty_queue").fetchone()[0]
    conn.close()

    assert result["source"] == "dirty_queue"
    assert result["indexed"] == 1
    assert result["dirty_remaining"] is False
    assert topics == ["queued-topic"]
    assert queued == 0


def test_metadata_repair_idles_without_dirty_queue_by_default(monkeypatch, tmp_path):
    sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "metadata_index")
    conn = sqlite_operator.init_database()

    monkeypatch.setattr(
        metadata_index,
        "stale_metadata_hashes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("default repair should not stale scan")),
    )

    result = metadata_index.reindex_stale_metadata_batch(conn, limit=10)
    conn.close()

    assert result["source"] == "idle"
    assert result["queued"] == 0


def test_metadata_repair_allows_explicit_stale_scan(monkeypatch, tmp_path):
    sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "metadata_index")
    conn = sqlite_operator.init_database()
    calls = []

    monkeypatch.setattr(metadata_index, "stale_metadata_hashes", lambda *args, **kwargs: calls.append("scan") or [])

    result = metadata_index.reindex_stale_metadata_batch(conn, limit=10, allow_scan=True)
    conn.close()

    assert calls == ["scan"]
    assert result["source"] == "stale_scan"
    assert result["queued"] == 0


def test_metadata_watchdog_keeps_pending_hashes_after_failure(monkeypatch, tmp_path):
    sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "metadata_index")
    sqlite_operator.init_database().close()

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(metadata_index.threading, "Timer", FakeTimer)
    metadata_index._watchdog_pending = {"watch-hash"}
    metadata_index._watchdog_timer = None
    metadata_index._watchdog_flushing = False

    def fail_reindex(conn, item_hash, reason):
        raise RuntimeError("locked")

    monkeypatch.setattr(metadata_index, "safe_reindex_item_metadata", fail_reindex)
    metadata_index._watchdog_flush()

    assert metadata_index._watchdog_pending == {"watch-hash"}
    assert isinstance(metadata_index._watchdog_timer, FakeTimer)
    assert metadata_index._watchdog_timer.delay == 2.0

    calls = []

    def ok_reindex(conn, item_hash, reason):
        calls.append((item_hash, reason))
        return {"status": "ok"}

    metadata_index._watchdog_timer = None
    monkeypatch.setattr(metadata_index, "safe_reindex_item_metadata", ok_reindex)
    metadata_index._watchdog_flush()

    assert calls == [("watch-hash", "watchdog")]
    assert metadata_index._watchdog_pending == set()


def test_metadata_cached_counters_update_after_reindex(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    item_hash = "ac" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(
        utils,
        conn,
        item_hash,
        "---\ntopics:\n  - one\n  - two\nwd_rating: safe\nwd_tags:\n  - shared\n---\n",
    )
    metadata_index.reindex_item_metadata(conn, item_hash)
    first = metadata_index.metadata_index_status(conn)

    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - one\nwd_rating: explicit\n---\n")
    metadata_index.reindex_item_metadata(conn, item_hash)
    second = metadata_index.metadata_index_status(conn)
    conn.close()

    assert first["indexed"] == 1
    assert first["topics"] == 2
    assert first["wd_tags"] == 2
    assert first["facet_counts"] == 6
    assert second["indexed"] == 1
    assert second["topics"] == 1
    assert second["wd_tags"] == 1
    assert second["dirty"] == 0


def test_metadata_status_fast_path_skips_stale_count(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")

    class ScalarCursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class StaleCursor:
        def __iter__(self):
            yield ("a", "000000000001", None, "", "", 0, 0, "", 0, 0, "")
            yield ("b", "000000000002", "b", "000000000002", "", 0, 0, "", 0, 0, "ok")

        def fetchall(self):
            raise AssertionError("fetchall must not be used")

    class Conn:
        def execute(self, sql, *args):
            if "FROM items" in sql and "LEFT JOIN item_metadata_files" in sql:
                raise AssertionError("fast status must not scan stale rows")
            return ScalarCursor(0)

    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)
    monkeypatch.setattr(metadata_index, "metadata_index_ready", lambda conn: False)

    status = metadata_index.metadata_index_status(Conn())

    assert status["stale"] is None
    assert status["stale_deep"] is False


def test_metadata_status_deep_uses_streaming_stale_count(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")

    class ScalarCursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class StaleCursor:
        def __iter__(self):
            yield ("a", "000000000001", None, "", "", 0, 0, "", 0, 0, "")
            yield ("b", "000000000002", "b", "000000000002", "", 0, 0, "", 0, 0, "ok")

        def fetchall(self):
            raise AssertionError("fetchall must not be used")

    class Conn:
        def execute(self, sql, *args):
            if "FROM items" in sql and "LEFT JOIN item_metadata_files" in sql:
                return StaleCursor()
            return ScalarCursor(0)

    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)
    monkeypatch.setattr(metadata_index, "metadata_index_ready", lambda conn: False)
    monkeypatch.setattr(metadata_index, "_row_stale", lambda row: row[0] == "a")

    status = metadata_index.metadata_index_status(Conn(), deep=True)

    assert status["stale"] == 1
    assert status["stale_deep"] is True


def test_rebuild_metadata_index_status_does_not_clear_rows(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "aa" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - keep-topic\n---\n")
    metadata_index.reindex_item_metadata(conn, item_hash)
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0]
    conn.close()

    report = tool.run("status")

    conn = sqlite_operator.init_database()
    after = conn.execute("SELECT COUNT(*) FROM item_topics").fetchone()[0]
    conn.close()
    assert report["action"] == "status"
    assert before == 1
    assert after == before


def test_simple_frontmatter_parser_matches_yaml_for_common_fields(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")
    yaml_text = """hash: abc
title: lmz000001.jpg
storage_id: lmz000001
source_url: https://example.test/item
platform: pixiv
source_artist: artist-001
artist: artist-001
date_added: '2026-01-01 00:00:00'
topics:
- topic-a
- topic-b
wd_rating: safe
wd_character_tags:
- character-a
wd_tags:
- tag-a
- tag-b
"""

    parsed = metadata_index._parse_simple_frontmatter(yaml_text)
    expected = yaml.safe_load(yaml_text)

    assert parsed is not None
    assert str(parsed.pop("date_added")) == str(expected.pop("date_added"))
    assert parsed == expected


def test_simple_frontmatter_parser_falls_back_for_complex_yaml(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")
    yaml_text = """topics: [topic-a, topic-b]
manual:
  nested: true
"""

    assert metadata_index._parse_simple_frontmatter(yaml_text) is None


def test_rebuild_metadata_index_fails_when_storage_id_missing(monkeypatch, tmp_path):
    sqlite_operator, = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "bb" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    conn.execute("UPDATE items SET storage_id = '' WHERE hash = ?", (item_hash,))
    conn.commit()
    conn.close()

    assert tool.main(["--stale"]) == 2


def test_rebuild_metadata_index_stale_reindexes_compact_files(monkeypatch, tmp_path):
    utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "cc" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - stale-topic\n---\n")
    conn.close()

    report = tool.run("stale")

    conn = sqlite_operator.init_database()
    topics = [row[0] for row in conn.execute("SELECT topic FROM item_topics WHERE item_hash = ?", (item_hash,))]
    conn.close()
    assert report["indexed"] == 1
    assert topics == ["stale-topic"]


def test_rebuild_metadata_index_full_clears_and_rebuilds(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "dd" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - rebuilt-topic\n---\n")
    metadata_index.ensure_metadata_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO item_topics(item_hash, topic, topic_norm) VALUES (?, 'old-topic', 'old-topic')",
        (item_hash,),
    )
    conn.commit()
    conn.close()

    report = tool.run("full")

    conn = sqlite_operator.init_database()
    topics = [row[0] for row in conn.execute("SELECT topic FROM item_topics WHERE item_hash = ? ORDER BY topic", (item_hash,))]
    index_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_item_%'"
        ).fetchall()
    }
    ready = metadata_index.metadata_index_ready(conn)
    status = metadata_index.metadata_index_status(conn)
    conn.close()
    assert report["indexed"] == 1
    assert topics == ["rebuilt-topic"]
    assert ready is True
    assert status["indexed"] == 1
    assert status["topics"] == 1
    assert status["facet_counts"] == 3
    assert set(metadata_index.METADATA_SECONDARY_INDEXES).issubset(index_names)
    assert set(report["stages_ms"]) >= {
        "item_fetch",
        "metadata_read_parse",
        "row_building",
        "db_flushes",
        "item_updates",
        "metadata_file_inserts",
        "topic_inserts",
        "wd_tag_inserts",
        "commits",
        "secondary_index_drop",
        "secondary_index_rebuild",
        "facet_rebuild",
        "counter_refresh",
    }


def test_rebuild_metadata_progress_callback_reports_full_rebuild(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    item_hash = "d8" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - progress-topic\n---\n")
    events = []

    result = metadata_index.rebuild_all_metadata(conn, progress_callback=lambda **event: events.append(event))
    conn.close()

    assert result["indexed"] == 1
    assert any(event.get("items_total") == 1 for event in events)
    assert any(event.get("items_done") == 1 for event in events)
    assert {event.get("stage") for event in events if event.get("stage")} >= {
        "reading metadata",
        "rebuilding facets",
        "rebuilding indexes",
        "refreshing counters",
    }


def test_metadata_status_includes_maintenance_rebuild_job(monkeypatch, tmp_path):
    sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "metadata_index")
    conn = sqlite_operator.init_database()

    metadata_index._reset_maintenance_rebuild_job("full")
    metadata_index._update_maintenance_rebuild_job(stage="reading metadata", items_total=10, items_done=3)
    status = metadata_index.metadata_index_status(conn)
    metadata_index._finish_maintenance_rebuild_job("completed", items_done=10)
    finished = metadata_index.maintenance_rebuild_status()
    conn.close()

    assert status["maintenance_rebuild"]["running"] is True
    assert status["maintenance_rebuild"]["mode"] == "full"
    assert status["maintenance_rebuild"]["items_total"] == 10
    assert status["maintenance_rebuild"]["items_done"] == 3
    assert finished["running"] is False
    assert finished["status"] == "completed"


def test_metadata_rebuild_api_starts_maintenance_job(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")
    calls = []

    def fake_start(full=False, maintenance=False):
        calls.append((full, maintenance))
        return {"status": "started", "full": full, "maintenance_rebuild": {"running": True}}

    monkeypatch.setattr(api.runtime, "start_metadata_repair_worker", fake_start)

    result = asyncio.run(api.runtime.rebuild_metadata_index())

    assert result["status"] == "started"
    assert calls == [(True, True)]


def test_rebuild_metadata_index_full_skips_deep_validation_by_default(monkeypatch, tmp_path):
    utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "de" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - fast-full\n---\n")
    conn.close()

    monkeypatch.setattr(
        tool,
        "stale_metadata_count",
        lambda conn: (_ for _ in ()).throw(AssertionError("deep stale scan should not run")),
    )

    report = tool.run("full")

    assert report["indexed"] == 1
    assert report["stale_before"] is None
    assert report["stale_after"] is None
    assert report["status"]["stale_deep"] is False


def test_rebuild_metadata_index_full_deep_validate_preserves_stale_scan(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    tool = load_maintenance_tool("rebuild_metadata_index")
    item_hash = "df" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - deep-full\n---\n")
    conn.close()

    calls = {"count": 0}
    original = metadata_index.stale_metadata_count

    def counted(conn):
        calls["count"] += 1
        return original(conn)

    monkeypatch.setattr(metadata_index, "stale_metadata_count", counted)
    monkeypatch.setattr(tool, "stale_metadata_count", counted)

    report = tool.run("full", deep_validate=True)

    assert report["indexed"] == 1
    assert report["status"]["stale_deep"] is True
    assert calls["count"] >= 2


def test_rebuild_metadata_index_failure_recreates_secondary_indexes(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index")
    item_hash = "d9" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - fail-full\n---\n")
    metadata_index.ensure_metadata_schema(conn)
    conn.commit()

    def fail_payload(*args, **kwargs):
        raise RuntimeError("forced rebuild failure")

    monkeypatch.setattr(metadata_index, "_metadata_payload", fail_payload)

    with pytest.raises(RuntimeError, match="forced rebuild failure"):
        metadata_index.rebuild_all_metadata(conn)

    index_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    ready_row = conn.execute(
        "SELECT value FROM metadata_index_state WHERE key = ?",
        (metadata_index.READY_KEY,),
    ).fetchone()
    conn.close()

    assert set(metadata_index.METADATA_SECONDARY_INDEXES).issubset(index_names)
    assert ready_row == ("0",)


def test_rebuild_metadata_index_stale_limit_caps_work(monkeypatch, tmp_path):
    utils, sqlite_operator = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator")
    tool = load_maintenance_tool("rebuild_metadata_index")
    hashes = ["ee" * 32, "ef" * 32]
    conn = insert_mock_item(sqlite_operator, hashes[0])
    write_compact_note(utils, conn, hashes[0], "---\ntopics:\n  - first-topic\n---\n")
    conn.close()
    conn = insert_mock_item(sqlite_operator, hashes[1])
    write_compact_note(utils, conn, hashes[1], "---\ntopics:\n  - second-topic\n---\n")
    conn.close()

    report = tool.run("stale", limit=1)

    conn = sqlite_operator.init_database()
    indexed = conn.execute("SELECT COUNT(*) FROM item_metadata_files").fetchone()[0]
    conn.close()
    assert report["indexed"] == 1
    assert indexed == 1
    assert report["stale_after"] is None


def test_rebuild_metadata_index_json_output(monkeypatch, tmp_path, capsys):
    sqlite_operator, = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator")
    conn = sqlite_operator.init_database()
    conn.close()
    tool = load_maintenance_tool("rebuild_metadata_index")

    assert tool.main(["--status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "status"
    assert "items" in payload


def test_maintenance_cli_default_and_alias_run_update_downloaders(monkeypatch, tmp_path):
    tool = load_maintenance_tool("maintenance_cli")
    calls = []
    monkeypatch.setattr(tool, "update_downloaders", lambda: calls.append("update") or 0)

    assert tool.main([]) == 0
    assert tool.main(["update-tools"]) == 0
    assert tool.main(["update"]) == 0
    assert calls == ["update", "update", "update"]


def test_maintenance_cli_check_invokes_readiness_script(monkeypatch, tmp_path):
    tool = load_maintenance_tool("maintenance_cli")
    commands = []

    def fake_run(cmd, text=True, **kwargs):
        commands.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.main(["check"]) == 0
    assert commands
    assert commands[0][0] == sys.executable
    assert commands[0][-1] == "--non-interactive"
    assert "lmz_readiness_check.py" in commands[0][1]


def test_maintenance_cli_install_playwright_browser(monkeypatch, tmp_path):
    tool = load_maintenance_tool("maintenance_cli")
    commands = []

    def fake_run(cmd, text=True, **kwargs):
        commands.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.main(["install-playwright-browser"]) == 0
    assert commands == [[sys.executable, "-m", "playwright", "install", "chromium"]]


def test_update_tools_wrapper_runs_update_downloaders(monkeypatch, tmp_path):
    captured = []
    fake_cli = types.SimpleNamespace(main=lambda argv: captured.append(argv) or 0)
    monkeypatch.setitem(sys.modules, "maintenance_cli", fake_cli)
    monkeypatch.setattr(sys, "argv", ["update_tools.py"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "tools" / "maintenance" / "update_tools.py"), run_name="__main__")

    assert exc.value.code == 0
    assert captured == [["update-downloaders"]]


def test_insert_to_database_default_timestamp_is_utc_format(monkeypatch, tmp_path):
    sqlite_operator, = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator")
    sample = tmp_path / "utc-check.jpg"
    sample.write_bytes(b"utc")
    file_hash = "ab" * 32
    conn = sqlite_operator.init_database()
    sqlite_operator.insert_to_database(
        conn,
        sample,
        file_hash,
        "image/jpeg",
        ".jpg",
        metadata={},
        file_size=sample.stat().st_size,
    )
    conn.commit()
    row = conn.execute("SELECT date_added FROM items WHERE hash = ?", (file_hash,)).fetchone()
    conn.close()
    assert row and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(row[0]))


def test_review_state_timestamp_uses_standard_format(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")
    updated = api.common._set_review_state({}, "resolved_delete")
    value = str(updated.get("resolved_at") or "")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value)


def test_stream_logs_tail_then_heartbeat_and_truncate_recovery(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")
    log_file = api.common.LOG_FILES["system.jsonl"]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text('{"message":"tail"}\n', encoding="utf-8")

    monotonic_counter = {"value": 0.0}

    def fake_monotonic():
        monotonic_counter["value"] += 20.0
        return monotonic_counter["value"]

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(api.logs.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(api.logs.asyncio, "sleep", fake_sleep)

    async def _run():
        response = await api.logs.stream_logs("system.jsonl")
        gen = response.body_iterator
        first = await gen.__anext__()
        second = await gen.__anext__()
        log_file.write_text("", encoding="utf-8")
        log_file.write_text('{"message":"after-clear"}\n', encoding="utf-8")
        third = await gen.__anext__()
        await gen.aclose()
        return first, second, third

    first, second, third = asyncio.run(_run())
    first_text = first.decode() if isinstance(first, bytes) else str(first)
    second_text = second.decode() if isinstance(second, bytes) else str(second)
    third_text = third.decode() if isinstance(third, bytes) else str(third)

    assert "tail" in first_text
    assert "keep-alive" in second_text
    assert "after-clear" in third_text

    console_file = api.common._log_file_for("system.jsonl", source="console")
    assert console_file.name == "console.log"
    console_file.parent.mkdir(parents=True, exist_ok=True)
    console_file.write_text("console-tail\n", encoding="utf-8")

    async def _run_console():
        response = await api.logs.stream_logs("system.jsonl", source="console")
        gen = response.body_iterator
        first_console = await gen.__anext__()
        await gen.aclose()
        return first_console

    console_text = asyncio.run(_run_console())
    console_text = console_text.decode() if isinstance(console_text, bytes) else str(console_text)
    assert "console-tail" in console_text

    log_file.write_text('{"message":"structured-kept"}\n', encoding="utf-8")
    console_file.write_text("console-to-clear\n", encoding="utf-8")
    assert api.logs._clear_all_logs_sync("console") == {"status": "success"}
    assert console_file.read_text(encoding="utf-8") == ""
    assert "structured-kept" in log_file.read_text(encoding="utf-8")


def test_topic_filter_not_ready_skips_disk_scan(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")
    repair_calls = []
    monkeypatch.setattr(api.library, "metadata_index_ready", lambda conn: False)
    monkeypatch.setattr(api.library, "start_metadata_repair_worker", lambda full=False: repair_calls.append(full) or {"status": "started"})
    monkeypatch.setattr(api.library, "load_note_topics", lambda *args: (_ for _ in ()).throw(AssertionError("note scan called")))

    result = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["topic"], [], [], None, 25)

    assert result == {"items": [], "has_more": False, "next_cursor": None}
    assert repair_calls == [False]

    facet = api.library._get_facets_sync("topic", "topic", 25)

    assert facet == {"kind": "topic", "items": []}
    assert repair_calls == [False, False]


def test_metadata_facets_keep_counts_correct(monkeypatch, tmp_path):
    sqlite_operator, metadata_index = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "metadata_index")
    item_a = "aa" * 32
    item_b = "bb" * 32
    conn = insert_mock_item(sqlite_operator, item_a)
    conn.close()
    conn = insert_mock_item(sqlite_operator, item_b)
    metadata_index.ensure_metadata_schema(conn)
    conn.execute("INSERT OR IGNORE INTO item_topics(item_hash, topic, topic_norm) VALUES (?, 'Alpha', 'alpha')", (item_a,))
    conn.execute("INSERT OR IGNORE INTO item_topics(item_hash, topic, topic_norm) VALUES (?, 'Alpha', 'alpha')", (item_b,))
    conn.execute("INSERT OR IGNORE INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Shared', 'shared', 'general')", (item_a,))
    conn.execute("INSERT OR IGNORE INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Shared', 'shared', 'character')", (item_a,))
    conn.commit()

    topics = metadata_index.metadata_facets(conn, "topic", "", 10)
    wd_tags = metadata_index.metadata_facets(conn, "wd_tag", "", 10)

    conn.close()
    assert topics == [{"value": "Alpha", "count": 2}]
    assert wd_tags == [{"value": "Shared", "count": 1, "tag_type": "character"}]


def test_metadata_facet_counts_refresh_and_fallback(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )
    item_hash = "41" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - Alpha\nwd_tags:\n  - Shared\n---\n")
    metadata_index.reindex_item_metadata(conn, item_hash)
    metadata_index._set_metadata_index_ready(conn, True)
    conn.commit()

    count_row = conn.execute(
        "SELECT value, count FROM metadata_facet_counts WHERE kind = 'wd_tag' AND value_norm = 'shared'"
    ).fetchone()
    assert count_row == ("Shared", 1)

    conn.execute("DELETE FROM metadata_facet_counts")
    conn.commit()
    fallback = metadata_index.metadata_facets(conn, "wd_tag", "share", 10)
    assert fallback == [{"value": "Shared", "count": 1, "tag_type": "general"}]

    write_compact_note(utils, conn, item_hash, "---\ntopics:\n  - Beta\nwd_tags:\n  - New Tag\n---\n")
    metadata_index.reindex_item_metadata(conn, item_hash)
    metadata_index._set_metadata_index_ready(conn, True)
    conn.commit()
    conn.close()

    facet = api.library._get_facets_sync("wd_tag", "", 10)
    assert facet == {"kind": "wd_tag", "items": [{"value": "New Tag", "count": 1, "tag_type": "general"}]}


def test_replace_delete_refreshes_wd_facet_counts(monkeypatch, tmp_path):
    sqlite_operator, metadata_index, api_library = fresh_backend(
        monkeypatch,
        tmp_path,
        "db.sqlite_operator",
        "metadata_index",
        "api.library",
    )
    old_hash = "42" * 32
    kept_hash = "43" * 32
    conn = insert_mock_item(sqlite_operator, old_hash)
    conn.close()
    conn = insert_mock_item(sqlite_operator, kept_hash)
    metadata_index.ensure_metadata_schema(conn)
    conn.execute("INSERT OR IGNORE INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Shared', 'shared', 'general')", (old_hash,))
    conn.execute("INSERT OR IGNORE INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Shared', 'shared', 'general')", (kept_hash,))
    metadata_index.refresh_metadata_facet_counts_for_values(conn, {("wd_tag", "shared")})
    conn.commit()
    conn.close()

    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_after_replacement(old_hash)

    conn = sqlite_operator.init_database()
    count_row = conn.execute(
        "SELECT value, count FROM metadata_facet_counts WHERE kind = 'wd_tag' AND value_norm = 'shared'"
    ).fetchone()
    old_tag_rows = conn.execute("SELECT COUNT(*) FROM item_wd_tags WHERE item_hash = ?", (old_hash,)).fetchone()[0]
    conn.close()

    assert result["status"] == "deleted"
    assert count_row == ("Shared", 1)
    assert old_tag_rows == 0


def test_delete_and_replace_cleanup_remove_thumbnails(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    replace_hash = "52" * 32
    delete_hash = "53" * 32
    conn = insert_mock_item(sqlite_operator, replace_hash)
    replace_storage = storage_id_for(conn, replace_hash)
    conn.close()
    conn = insert_mock_item(sqlite_operator, delete_hash)
    delete_storage = storage_id_for(conn, delete_hash)
    conn.close()

    def write_item_files(item_hash: str, storage_id: str):
        asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
        note = utils.note_path_for(item_hash, storage_id)
        wd = utils.wd_tag_cache_path_for(item_hash, storage_id)
        thumb = thumbnails.thumbnail_path_for(item_hash, storage_id)
        for path, data in (
            (asset, b"asset"),
            (note, b"---\n---\n"),
            (wd, b"{}"),
            (thumb, b"thumb"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return asset, note, wd, thumb

    replace_paths = write_item_files(replace_hash, replace_storage)
    delete_paths = write_item_files(delete_hash, delete_storage)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    assert api_library._delete_item_after_replacement(replace_hash)["status"] == "deleted"
    assert api_library._delete_item_sync(delete_hash)["status"] == "success"

    for path in replace_paths + delete_paths:
        assert not path.exists()


def test_delete_stage_failure_keeps_db_row_and_files(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    item_hash = "54" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    note = utils.note_path_for(item_hash, storage_id)
    wd = utils.wd_tag_cache_path_for(item_hash, storage_id)
    thumb = thumbnails.thumbnail_path_for(item_hash, storage_id)
    for path, data in ((asset, b"asset"), (note, b"---\n---\n"), (wd, b"{}"), (thumb, b"thumb")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    real_replace = Path.replace

    def failing_replace(self, target):
        if self == asset:
            raise OSError("locked asset")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_sync(item_hash)
    conn = sqlite_operator.init_database()
    db_row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()

    assert result["status"] == "warning"
    assert result["deleted"] is False
    assert db_row is not None
    assert all(path.exists() for path in (asset, note, wd, thumb))


def test_delete_db_failure_restores_staged_files(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    item_hash = "55" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    note = utils.note_path_for(item_hash, storage_id)
    wd = utils.wd_tag_cache_path_for(item_hash, storage_id)
    thumb = thumbnails.thumbnail_path_for(item_hash, storage_id)
    for path, data in ((asset, b"asset"), (note, b"---\n---\n"), (wd, b"{}"), (thumb, b"thumb")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    monkeypatch.setattr(api_library, "refresh_metadata_index_counters", lambda conn: (_ for _ in ()).throw(RuntimeError("db failure")))
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_sync(item_hash)
    conn = sqlite_operator.init_database()
    db_row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()

    assert result["status"] == "warning"
    assert result["deleted"] is False
    assert db_row is not None
    assert all(path.exists() for path in (asset, note, wd, thumb))


def test_delete_final_trash_cleanup_failure_reports_error(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    item_hash = "56" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    note = utils.note_path_for(item_hash, storage_id)
    wd = utils.wd_tag_cache_path_for(item_hash, storage_id)
    thumb = thumbnails.thumbnail_path_for(item_hash, storage_id)
    for path, data in ((asset, b"asset"), (note, b"---\n---\n"), (wd, b"{}"), (thumb, b"thumb")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if ".delete-trash" in str(self):
            raise OSError("trash locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_sync(item_hash)
    conn = sqlite_operator.init_database()
    db_row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()

    assert result["status"] == "success"
    assert result["deleted"] is True
    assert db_row is None
    assert result["cleanup_errors"]
    assert all(".delete-trash" in error["path"] for error in result["cleanup_errors"])
    assert not any(path.exists() for path in (asset, note, wd, thumb))


def test_replace_final_trash_cleanup_failure_reports_error(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
    )
    item_hash = "57" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if ".replace-trash" in str(self):
            raise OSError("replace trash locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_after_replacement(item_hash)

    assert result["status"] == "deleted"
    assert result["cleanup_errors"]
    assert all(".replace-trash" in error["path"] for error in result["cleanup_errors"])


def test_review_replace_warns_when_old_target_cleanup_incomplete(monkeypatch, tmp_path):
    utils, sqlite_operator, api, api_review = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api",
        "api.review",
    )
    old_hash = "58" * 32
    new_hash = "59" * 32
    conn = insert_mock_item(sqlite_operator, old_hash)
    conn.close()
    review_file = utils.REVIEW_DIR / "replace-warning.jpg"
    review_file.write_bytes(b"replacement")
    review_file.with_suffix(".jpg.json").write_text(json.dumps({"best_match": old_hash}), encoding="utf-8")

    def fake_process_file(path, config, metadata=None, delete_source=False, skip_similarity=False, **kwargs):
        conn = insert_mock_item(sqlite_operator, new_hash)
        new_storage_id = storage_id_for(conn, new_hash)
        note_path = utils.note_path_for(new_hash, new_storage_id)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("---\n---\n", encoding="utf-8")
        conn.close()
        if delete_source and path.exists():
            path.unlink()
        return True, "ok", {"file_hash": new_hash}

    monkeypatch.setattr(api_review, "process_file", fake_process_file)
    monkeypatch.setattr(
        api_review,
        "_delete_item_after_replacement",
        lambda target_hash, **kwargs: {
            "hash": target_hash,
            "status": "deleted",
            "cleanup_errors": [{"hash": target_hash, "path": "review/.replace-trash/x", "error": "locked"}],
        },
    )

    result = api_review._review_action_sync("replace-warning.jpg", "replace")

    assert result["status"] == "warning"
    assert "cleanup" in result["message"].lower()


def test_successful_delete_does_not_leave_vault_health_orphans(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails, vaults = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
        "vaults",
    )
    item_hash = "5a" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    note = utils.note_path_for(item_hash, storage_id)
    wd = utils.wd_tag_cache_path_for(item_hash, storage_id)
    thumb = thumbnails.thumbnail_path_for(item_hash, storage_id)
    for path, data in ((asset, b"asset"), (note, b"---\n---\n"), (wd, b"{}"), (thumb, b"thumb")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_sync(item_hash)
    report = vaults.audit_vault_health("default")

    assert result["status"] == "success"
    assert str(asset) not in report["orphans"]["assets"]
    assert str(note) not in report["orphans"]["notes"]


@pytest.mark.parametrize("delete_mode", ["normal", "replacement"])
def test_delete_stages_all_storage_owned_files_across_shards(monkeypatch, tmp_path, delete_mode):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    item_hash = "5b" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    vault = api_library.get_runtime_context().active_vault
    paths = [
        utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id),
        utils.note_path_for(item_hash, storage_id),
        utils.wd_tag_cache_path_for(item_hash, storage_id),
        thumbnails.thumbnail_path_for(item_hash, storage_id),
        vault.assets_dir / "aa" / f"{storage_id}.png",
        vault.notes_dir / "bb" / f"{storage_id}.md",
        vault.wd_tags_dir / "cc" / f"{storage_id}.json",
        vault.thumbnails_dir / "dd" / f"{storage_id}.jpg",
        vault.thumbnails_dir / "ee" / f"{storage_id}_video.jpg",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned")
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    if delete_mode == "normal":
        result = api_library._delete_item_sync(item_hash)
        assert result["status"] == "success"
    else:
        result = api_library._delete_item_after_replacement(item_hash)
        assert result["status"] == "deleted"

    conn = sqlite_operator.init_database()
    assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is None
    conn.close()
    assert not any(path.exists() for path in paths)


def test_locked_wrong_shard_file_aborts_delete_and_restores_staged_files(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
    )
    item_hash = "5c" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    canonical = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    stale = api_library.get_runtime_context().active_vault.assets_dir / "ff" / f"{storage_id}.png"
    for path in (canonical, stale):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
    real_replace = Path.replace

    def fail_stale_move(self, target):
        if self == stale:
            raise OSError("stale asset locked")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_stale_move)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    result = api_library._delete_item_sync(item_hash)

    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()
    assert result["status"] == "warning"
    assert result["deleted"] is False
    assert row is not None
    assert canonical.exists()
    assert stale.exists()


def test_delete_waits_for_inflight_thumbnail_and_removes_published_file(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, thumbnails = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "thumbnails",
    )
    item_hash = "5d" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")
    started = threading.Event()
    release = threading.Event()

    def slow_generate(asset_path, generated_hash, generated_storage_id, ctx=None):
        started.set()
        assert release.wait(timeout=3)
        target = thumbnails.thumbnail_path_for(generated_hash, generated_storage_id, ctx)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"thumb")
        return target

    monkeypatch.setattr(thumbnails, "generate_image_thumbnail", slow_generate)
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        generate_future = executor.submit(
            thumbnails.ensure_thumbnail,
            item_hash,
            ".jpg",
            "image/jpeg",
            True,
            storage_id,
        )
        assert started.wait(timeout=2)
        delete_future = executor.submit(api_library._delete_item_sync, item_hash)
        time.sleep(0.1)
        assert not delete_future.done()
        release.set()
        generate_future.result(timeout=3)
        result = delete_future.result(timeout=3)

    assert result["status"] == "success"
    assert not asset.exists()
    assert not thumbnails.thumbnail_path_for(item_hash, storage_id).exists()


def test_wd_cache_publication_refuses_deleted_owner(monkeypatch, tmp_path):
    utils, sqlite_operator, api_library, service = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "api.library",
        "tagging.service",
    )
    item_hash = "5e" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")
    monkeypatch.setattr(api_library.search_manager, "remove_indexes_batch", lambda *args, **kwargs: None)
    assert api_library._delete_item_sync(item_hash)["status"] == "success"
    result = service.TagResult(
        hash=item_hash,
        status="ok",
        model="fake",
        threshold=0.35,
        created_at="2026-01-01 00:00:00",
        rating=None,
        character_tags=[],
        tags=[],
    )

    published = service._write_result(result, storage_id, media_path=asset)

    assert published is False
    assert not utils.wd_tag_cache_path_for(item_hash, storage_id).exists()


def test_item_details_include_topic_and_wd_counts(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )
    item_a = "91" * 32
    item_b = "92" * 32
    conn = insert_mock_item(sqlite_operator, item_a)
    write_compact_note(conn=conn, utils=utils, item_hash=item_a, text="---\ntopics:\n  - Shared Topic\nwd_rating: safe\nwd_tags:\n  - Shared Tag\n---\n")
    metadata_index.reindex_item_metadata(conn, item_a)
    conn.commit()
    conn.close()

    conn = insert_mock_item(sqlite_operator, item_b)
    write_compact_note(conn=conn, utils=utils, item_hash=item_b, text="---\ntopics:\n  - Shared Topic\nwd_rating: safe\nwd_tags:\n  - Shared Tag\n---\n")
    metadata_index.reindex_item_metadata(conn, item_b)
    conn.commit()
    conn.close()

    detail = api.library._get_item_sync(item_a)

    assert detail["topic_counts"]["Shared Topic"] == 2
    assert detail["wd_tag_counts"]["safe"] == 2
    assert detail["wd_tag_counts"]["Shared Tag"] == 2


def test_patch_item_updates_topics_and_wd_frontmatter_for_one_item(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )
    item_a = "93" * 32
    item_b = "94" * 32
    conn = insert_mock_item(sqlite_operator, item_a)
    write_compact_note(
        utils,
        conn,
        item_a,
        "---\ntopics:\n  - keep\nwd_rating: safe\nwd_character_tags:\n  - wrong character\nwd_tags:\n  - promote me\n  - remove me\n---\n",
    )
    metadata_index.reindex_item_metadata(conn, item_a)
    conn.commit()
    conn.close()

    conn = insert_mock_item(sqlite_operator, item_b)
    write_compact_note(
        utils,
        conn,
        item_b,
        "---\ntopics:\n  - keep\nwd_rating: safe\nwd_character_tags:\n  - wrong character\nwd_tags:\n  - promote me\n  - remove me\n---\n",
    )
    metadata_index.reindex_item_metadata(conn, item_b)
    conn.commit()
    conn.close()

    result = api.library._update_item_sync(
        item_a,
        api.library.ItemUpdate(
            topics=["keep", "promote me"],
            wd_rating="",
            wd_character_tags=[],
            wd_tags=["promote me"],
        ),
    )

    conn = sqlite_operator.init_database()
    storage_a = storage_id_for(conn, item_a)
    storage_b = storage_id_for(conn, item_b)
    data_a = frontmatter_from_markdown(utils.note_path_for(item_a, storage_a).read_text(encoding="utf-8"))
    data_b = frontmatter_from_markdown(utils.note_path_for(item_b, storage_b).read_text(encoding="utf-8"))
    rows_a = conn.execute("SELECT tag_type, tag FROM item_wd_tags WHERE item_hash = ? ORDER BY tag_type, tag", (item_a,)).fetchall()
    rows_b = conn.execute("SELECT tag_type, tag FROM item_wd_tags WHERE item_hash = ? ORDER BY tag_type, tag", (item_b,)).fetchall()
    conn.close()

    assert result["topics"] == ["keep", "promote_me"]
    assert result["wd_tags"]["rating"] == "None"
    assert result["wd_tags"]["characters"] == []
    assert result["wd_tags"]["general"] == ["promote me"]
    assert data_a["topics"][0].startswith("[keep](")
    assert data_a["topics"][1].startswith("[promote_me](")
    assert data_a["wd_rating"] == ""
    assert data_a["wd_character_tags"] == []
    assert data_a["wd_tags"] == ["promote me"]
    assert data_b["topics"] == ["keep"]
    assert data_b["wd_rating"] == "safe"
    assert data_b["wd_character_tags"] == ["wrong character"]
    assert data_b["wd_tags"] == ["promote me", "remove me"]
    assert rows_a == [("general", "promote me")]
    assert rows_b == [("character", "wrong character"), ("general", "promote me"), ("general", "remove me"), ("rating", "safe")]


def test_topic_file_creation_preserves_body_and_item_markdown_uses_links(monkeypatch, tmp_path):
    utils, sqlite_operator, api, topics = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api", "topics")
    item_hash = "95" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)

    existing_topic = utils.TOPICS_DIR / "syntax.md"
    existing_topic.parent.mkdir(parents=True, exist_ok=True)
    existing_topic.write_text("---\ncreated_at: old\n---\n\npersonal notes stay\n", encoding="utf-8")

    result = api.library._update_item_sync(
        item_hash,
        api.library.ItemUpdate(topics=["syntax", "color theory"]),
    )
    data = frontmatter_from_markdown(note_path.read_text(encoding="utf-8"))

    assert result["topics"] == ["color_theory", "syntax"]
    assert data["topics"][0].startswith("[syntax](")
    assert data["topics"][1].startswith("[color_theory](")
    assert "personal notes stay" in existing_topic.read_text(encoding="utf-8")
    assert (utils.TOPICS_DIR / "color_theory.md").exists()

    parsed = topics.parse_topic_values(data["topics"], note_path)
    assert [entry["label"] for entry in parsed] == ["syntax", "color_theory"]
    assert [entry["topic_rel"] for entry in parsed] == ["syntax.md", "color_theory.md"]
    conn.close()


def test_repeated_topic_promotions_create_each_topic_file(monkeypatch, tmp_path):
    utils, sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api")
    item_hash = "99" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    conn.close()

    first = api.library._update_item_sync(
        item_hash,
        api.library.ItemUpdate(topics=["karin (blue archive)"]),
    )
    assert first["topics"] == ["karin_blue_archive"]
    assert (utils.TOPICS_DIR / "karin_blue_archive.md").exists()

    second = api.library._update_item_sync(
        item_hash,
        api.library.ItemUpdate(topics=["karin_blue_archive", "black hair"]),
    )
    note_path = utils.note_path_for(item_hash, storage_id)
    data = frontmatter_from_markdown(note_path.read_text(encoding="utf-8"))

    assert second["topics"] == ["black_hair", "karin_blue_archive"]
    assert (utils.TOPICS_DIR / "karin_blue_archive.md").exists()
    assert (utils.TOPICS_DIR / "black_hair.md").exists()
    assert data["topics"] == [
        "[karin_blue_archive](../../../../../topics/karin_blue_archive.md)",
        "[black_hair](../../../../../topics/black_hair.md)",
    ]


def test_metadata_index_parses_linked_and_legacy_topics(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, topics = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "metadata_index", "topics")
    item_hash = "96" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)
    linked = topics.topic_markdown_link("syntax", note_path)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(f"---\ntopics:\n  - '{linked}'\n  - legacy topic\n---\n", encoding="utf-8")

    metadata_index.reindex_item_metadata(conn, item_hash)

    rows = conn.execute(
        "SELECT topic, topic_norm, topic_rel, topic_key FROM item_topics WHERE item_hash = ? ORDER BY topic",
        (item_hash,),
    ).fetchall()
    assert rows == [
        ("legacy topic", "legacy topic", "", "plain:legacy topic"),
        ("syntax", "syntax", "syntax.md", "rel:syntax.md"),
    ]
    assert metadata_index.metadata_facets(conn, "topic", "synt", 10) == [{"value": "syntax", "count": 1}]
    conn.close()


def test_workspace_topic_rename_updates_linked_legacy_and_cross_vault_refs(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api, topics, vaults = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
        "topics",
        "vaults",
    )
    vaults.create_vault("Second")
    vault_items = {item["id"]: item for item in vaults.vault_list()}
    second_root = Path(vault_items["second"]["root"])
    second_db = Path(vault_items["second"]["db_path"])

    linked_hash = "a7" * 32
    legacy_hash = "a8" * 32
    second_hash = "a9" * 32
    linked_conn = insert_mock_item(sqlite_operator, linked_hash, date_added="2026-01-01 00:00:01")
    linked_conn.close()
    legacy_conn = insert_mock_item(sqlite_operator, legacy_hash, date_added="2026-01-01 00:00:02")
    legacy_storage = storage_id_for(legacy_conn, legacy_hash)
    write_compact_note(utils, legacy_conn, legacy_hash, "---\ntopics:\n  - Old Topic\n---\n")
    metadata_index.reindex_item_metadata(legacy_conn, legacy_hash)
    legacy_conn.commit()
    legacy_conn.close()

    api.library._update_item_sync(linked_hash, api.library.ItemUpdate(topics=["Old Topic"]))
    old_topic = utils.TOPICS_DIR / "old_topic.md"
    old_topic.write_text("---\ncreated_at: old\n---\n\npersonal notes stay\n", encoding="utf-8")

    second_conn = sqlite_operator.init_database(second_db)
    second_storage = sqlite_operator.allocate_storage_id(second_conn)
    second_conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:03', '', '', 'local', 'DB Artist', '')
        """,
        (second_hash, second_storage, f"{second_hash}.jpg"),
    )
    second_note = second_root / "vault" / "notes" / second_hash[:2] / f"{second_storage}.md"
    second_note.parent.mkdir(parents=True, exist_ok=True)
    second_link = topics.topic_markdown_link("Old Topic", second_note)
    second_note.write_text(f"---\ntopics:\n  - '{second_link}'\n---\n", encoding="utf-8")
    metadata_index.ensure_metadata_schema(second_conn)
    for entry in topics.parse_topic_values([second_link], second_note):
        second_conn.execute(
            "INSERT INTO item_topics(item_hash, topic, topic_norm, topic_rel, topic_key) VALUES (?, ?, ?, ?, ?)",
            (second_hash, entry["label"], entry["label"].casefold(), entry["topic_rel"], entry["topic_key"]),
        )
    second_conn.commit()
    second_conn.close()

    ready_conn = sqlite_operator.init_database()
    metadata_index._set_metadata_index_ready(ready_conn, True)
    ready_conn.commit()
    ready_conn.close()

    result = api.library._rename_topic_sync("Old Topic", "New Topic")

    assert result["status"] == "success"
    assert set(result["vaults_touched"]) == {"default", "second"}
    assert result["notes_rewritten"] == 3
    assert result["legacy_plain_refs_rewritten"] == 1
    assert not old_topic.exists()
    new_topic = utils.TOPICS_DIR / "new_topic.md"
    assert new_topic.exists()
    assert "personal notes stay" in new_topic.read_text(encoding="utf-8")

    conn = sqlite_operator.init_database()
    linked_storage = storage_id_for(conn, linked_hash)
    linked_data = frontmatter_from_markdown(utils.note_path_for(linked_hash, linked_storage).read_text(encoding="utf-8"))
    legacy_data = frontmatter_from_markdown(utils.note_path_for(legacy_hash, legacy_storage).read_text(encoding="utf-8"))
    rows = conn.execute("SELECT item_hash, topic, topic_norm, topic_rel, topic_key FROM item_topics ORDER BY item_hash").fetchall()
    new_items = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["new_topic"], [], [], None, 25)
    old_items = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["old_topic"], [], [], None, 25)
    all_topics = api.library._get_facets_sync("topic", "", 50, "all")["items"]
    conn.close()

    assert linked_data["topics"] == ["[new_topic](../../../../../topics/new_topic.md)"]
    assert legacy_data["topics"] == ["[new_topic](../../../../../topics/new_topic.md)"]
    assert rows == [
        (linked_hash, "new_topic", "new_topic", "new_topic.md", "rel:new_topic.md"),
        (legacy_hash, "new_topic", "new_topic", "new_topic.md", "rel:new_topic.md"),
    ]
    assert [item["hash"] for item in new_items["items"]] == [legacy_hash, linked_hash]
    assert old_items["items"] == []
    assert "new_topic" in {item["value"] for item in all_topics}
    assert "old_topic" not in {item["value"] for item in all_topics}

    second_conn = sqlite3.connect(second_db)
    try:
        second_rows = second_conn.execute("SELECT topic, topic_norm, topic_rel, topic_key FROM item_topics WHERE item_hash = ?", (second_hash,)).fetchall()
    finally:
        second_conn.close()
    assert second_rows == [("new_topic", "new_topic", "new_topic.md", "rel:new_topic.md")]
    assert "[new_topic](" in second_note.read_text(encoding="utf-8")


def test_workspace_topic_rename_rejects_missing_and_existing_target(monkeypatch, tmp_path):
    utils, api = fresh_backend(monkeypatch, tmp_path, "utils", "api")
    (utils.TOPICS_DIR / "old_topic.md").parent.mkdir(parents=True, exist_ok=True)
    (utils.TOPICS_DIR / "old_topic.md").write_text("---\n---\n", encoding="utf-8")
    (utils.TOPICS_DIR / "new_topic.md").write_text("---\n---\n", encoding="utf-8")

    with pytest.raises(HTTPException) as conflict:
        api.library._rename_topic_sync("Old Topic", "New Topic")
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as missing:
        api.library._rename_topic_sync("Missing Topic", "Other Topic")
    assert missing.value.status_code == 404


def test_metadata_maintenance_wd_tag_rename_and_delete_rewrites_notes(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )
    item_a = "e1" * 32
    item_b = "e2" * 32
    conn = insert_mock_item(sqlite_operator, item_a)
    storage_a = storage_id_for(conn, item_a)
    note_a = utils.note_path_for(item_a, storage_a)
    note_a.parent.mkdir(parents=True, exist_ok=True)
    note_a.write_text(
        "---\nwd_rating: safe\nwd_character_tags:\n  - Shared\n  - Keep Character\nwd_tags:\n  - Shared\n  - Keep General\n---\nbody\n",
        encoding="utf-8",
    )
    metadata_index.reindex_item_metadata(conn, item_a)
    conn.commit()
    conn.close()

    conn = insert_mock_item(sqlite_operator, item_b)
    storage_b = storage_id_for(conn, item_b)
    note_b = utils.note_path_for(item_b, storage_b)
    note_b.parent.mkdir(parents=True, exist_ok=True)
    note_b.write_text("---\nwd_tags:\n  - Shared\n  - Other\n---\nbody\n", encoding="utf-8")
    metadata_index.reindex_item_metadata(conn, item_b)
    conn.commit()
    conn.close()

    renamed = api.library._rename_wd_tag_sync("Shared", "Renamed", tag_type="general")

    assert renamed["status"] == "success"
    assert renamed["notes_rewritten"] == 2
    data_a = frontmatter_from_markdown(note_a.read_text(encoding="utf-8"))
    data_b = frontmatter_from_markdown(note_b.read_text(encoding="utf-8"))
    assert data_a["wd_character_tags"] == ["Shared", "Keep Character"]
    assert data_a["wd_tags"] == ["Renamed", "Keep General"]
    assert data_b["wd_tags"] == ["Renamed", "Other"]

    deleted = api.library._delete_wd_tag_sync("Renamed")

    assert deleted["status"] == "success"
    assert deleted["notes_rewritten"] == 2
    data_a = frontmatter_from_markdown(note_a.read_text(encoding="utf-8"))
    data_b = frontmatter_from_markdown(note_b.read_text(encoding="utf-8"))
    assert data_a["wd_character_tags"] == ["Shared", "Keep Character"]
    assert data_a["wd_tags"] == ["Keep General"]
    assert data_b["wd_tags"] == ["Other"]

    conn = sqlite_operator.init_database()
    try:
        rows = conn.execute("SELECT tag_type, tag FROM item_wd_tags ORDER BY item_hash, tag_type, tag").fetchall()
        facets = metadata_index.metadata_facets(conn, "wd_tag", "", 20)
    finally:
        conn.close()
    assert ("general", "Renamed") not in rows
    assert ("character", "Shared") in rows
    assert "Renamed" not in {item["value"] for item in facets}


def test_metadata_maintenance_topic_delete_and_merge_routes_rewrite_notes(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )
    merge_hash = "e3" * 32
    delete_hash = "e4" * 32
    conn = insert_mock_item(sqlite_operator, merge_hash)
    conn.close()
    conn = insert_mock_item(sqlite_operator, delete_hash)
    conn.close()

    api.library._update_item_sync(merge_hash, api.library.ItemUpdate(topics=["Source Topic"]))
    api.library._update_item_sync(delete_hash, api.library.ItemUpdate(topics=["Delete Topic"]))
    (utils.TOPICS_DIR / "target_topic.md").write_text("---\ncreated_at: target\n---\n", encoding="utf-8")

    merged = api.library._merge_topic_sync("Source Topic", "Target Topic")

    assert merged["status"] == "success"
    assert merged["notes_rewritten"] == 1
    assert not (utils.TOPICS_DIR / "source_topic.md").exists()
    assert (utils.TOPICS_DIR / "target_topic.md").exists()

    conn = sqlite_operator.init_database()
    try:
        merge_storage = storage_id_for(conn, merge_hash)
        merge_data = frontmatter_from_markdown(utils.note_path_for(merge_hash, merge_storage).read_text(encoding="utf-8"))
        merge_rows = conn.execute("SELECT topic FROM item_topics WHERE item_hash = ?", (merge_hash,)).fetchall()
    finally:
        conn.close()
    assert merge_data["topics"] == ["[target_topic](../../../../../topics/target_topic.md)"]
    assert merge_rows == [("target_topic",)]

    deleted = api.library._delete_topic_sync("Delete Topic")

    assert deleted["status"] == "success"
    assert deleted["notes_rewritten"] == 1
    assert not (utils.TOPICS_DIR / "delete_topic.md").exists()
    conn = sqlite_operator.init_database()
    try:
        delete_storage = storage_id_for(conn, delete_hash)
        delete_data = frontmatter_from_markdown(utils.note_path_for(delete_hash, delete_storage).read_text(encoding="utf-8"))
        delete_rows = conn.execute("SELECT topic FROM item_topics WHERE item_hash = ?", (delete_hash,)).fetchall()
    finally:
        conn.close()
    assert delete_data["topics"] == []
    assert delete_rows == []


def test_metadata_maintenance_restores_notes_when_db_commit_fails(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, metadata_maintenance = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "metadata_maintenance",
    )
    item_hash = "e5" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    note_path = utils.note_path_for(item_hash, storage_id)
    original_text = "---\nwd_tags:\n  - Shared\n---\nbody\n"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(original_text, encoding="utf-8")
    metadata_index.reindex_item_metadata(conn, item_hash)
    conn.commit()
    conn.close()

    real_init_database = metadata_maintenance.init_database

    class CommitFailConnection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            raise sqlite3.OperationalError("forced commit failure")

    def failing_init_database(*args, **kwargs):
        return CommitFailConnection(real_init_database(*args, **kwargs))

    monkeypatch.setattr(metadata_maintenance, "init_database", failing_init_database)

    result = metadata_maintenance.rename_wd_tag_across_workspace("Shared", "Renamed")

    assert result["status"] == "partial"
    assert "forced commit failure" in str(result["errors"])
    assert note_path.read_text(encoding="utf-8") == original_text


def test_metadata_facets_no_match_uses_built_count_table_without_scan(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, params=()):
            if "SELECT 1 FROM metadata_facet_counts" in sql:
                return Cursor([(1,)])
            if "FROM metadata_facet_counts" in sql:
                return Cursor([])
            if "FROM item_wd_tags" in sql:
                raise AssertionError("built count table should not fall back to scan")
            return Cursor([])

    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)

    assert metadata_index.metadata_facets(Conn(), "wd_tag", "missing", 10) == []


def test_metadata_facets_empty_ready_kind_does_not_scan(monkeypatch, tmp_path):
    metadata_index, = fresh_backend(monkeypatch, tmp_path, "metadata_index")

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, params=()):
            if "SELECT 1 FROM metadata_facet_counts" in sql:
                return Cursor([])
            if "FROM metadata_facet_kind_state" in sql and params == ("artist",):
                return Cursor([("1",)])
            if "SELECT COUNT(*) FROM metadata_facet_counts" in sql:
                return Cursor([(0,)])
            if "FROM metadata_facet_counts" in sql:
                return Cursor([])
            if "FROM items" in sql or "FROM item_topics" in sql or "FROM item_wd_tags" in sql:
                raise AssertionError("ready empty facet cache should not fall back to scan")
            return Cursor([])

    monkeypatch.setattr(metadata_index, "ensure_metadata_schema", lambda conn: None)

    assert metadata_index.metadata_facets(Conn(), "artist", "missing", 10) == []


def test_metadata_filters_use_exact_when_available_and_partial_when_needed(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )

    def add_item(item_hash: str, frontmatter: str, date_added: str):
        conn = insert_mock_item(sqlite_operator, item_hash, date_added=date_added)
        write_compact_note(utils, conn, item_hash, frontmatter)
        metadata_index.reindex_item_metadata(conn, item_hash)
        conn.commit()
        conn.close()

    alpha_hash = "10" * 32
    alpha_extra_hash = "20" * 32
    beta_hash = "30" * 32
    wd_hash = "40" * 32
    add_item(alpha_hash, "---\ntopics:\n  - alpha\n---\n", "2026-01-01 00:00:01")
    add_item(alpha_extra_hash, "---\ntopics:\n  - alpha-extra\n---\n", "2026-01-01 00:00:02")
    add_item(beta_hash, "---\ntopics:\n  - beta\n---\n", "2026-01-01 00:00:03")
    add_item(wd_hash, "---\ntopics: []\nwd_tags:\n  - wd-one\n---\n", "2026-01-01 00:00:04")

    conn = sqlite_operator.init_database()
    metadata_index._set_metadata_index_ready(conn, True)
    assert api.library._metadata_filter_has_exact(conn, "item_topics", "topic_norm", "alpha") is True
    assert api.library._metadata_filter_has_exact(conn, "item_topics", "topic_norm", "alph") is False
    assert api.library._metadata_filter_has_exact(conn, "item_wd_tags", "tag_norm", "wd-one") is True
    conn.commit()
    conn.close()

    exact_topic = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["alpha"], [], [], None, 25)
    partial = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["alph"], [], [], None, 25)
    exact = api.library._get_items_sync(None, None, "newest", "all", [], [], [], ["beta"], [], [], None, 25)
    wd_exact = api.library._get_items_sync(None, None, "newest", "all", [], [], [], [], ["wd-one"], [], None, 25)

    assert [item["hash"] for item in exact_topic["items"]] == [alpha_hash]
    assert [item["hash"] for item in partial["items"]] == [alpha_extra_hash, alpha_hash]
    assert [item["hash"] for item in exact["items"]] == [beta_hash]
    assert [item["hash"] for item in wd_exact["items"]] == [wd_hash]


def test_artist_platform_facets_and_filters_use_exact_first(monkeypatch, tmp_path):
    utils, sqlite_operator, metadata_index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "metadata_index",
        "api",
    )

    def add_item(item_hash: str, artist: str, platform: str, date_added: str):
        conn = insert_mock_item(sqlite_operator, item_hash, artist=artist, date_added=date_added)
        conn.execute("UPDATE items SET platform = ? WHERE hash = ?", (platform, item_hash))
        write_compact_note(utils, conn, item_hash, "---\ntopics: []\n---\n")
        metadata_index.reindex_item_metadata(conn, item_hash)
        metadata_index._set_metadata_index_ready(conn, True)
        conn.commit()
        conn.close()

    artist_one_hash = "51" * 32
    artist_extra_hash = "52" * 32
    platform_hash = "53" * 32
    add_item(artist_one_hash, "artist1", "site1", "2026-01-01 00:00:01")
    add_item(artist_extra_hash, "artist11", "site11", "2026-01-01 00:00:02")
    add_item(platform_hash, "other", "site1", "2026-01-01 00:00:03")

    artist_facets = api.library._get_facets_sync("artist", "artist", 10)
    platform_facets = api.library._get_facets_sync("platform", "site", 10)
    exact_artist = api.library._get_items_sync(None, None, "newest", "all", ["artist1"], [], [], [], [], [], None, 25)
    partial_artist = api.library._get_items_sync(None, None, "newest", "all", ["artist"], [], [], [], [], [], None, 25)
    exact_platform = api.library._get_items_sync(None, None, "newest", "all", [], ["site1"], [], [], [], [], None, 25)

    assert artist_facets["items"][0] == {"value": "artist1", "count": 1}
    assert {item["value"] for item in platform_facets["items"]} >= {"site1", "site11"}
    assert [item["hash"] for item in exact_artist["items"]] == [artist_one_hash]
    assert [item["hash"] for item in partial_artist["items"]] == [artist_extra_hash, artist_one_hash]
    assert [item["hash"] for item in exact_platform["items"]] == [platform_hash, artist_one_hash]


def test_artist_schema_backfills_from_item_artists(monkeypatch, tmp_path):
    sqlite_operator, artists = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "artists")
    first_hash = "61" * 32
    second_hash = "62" * 32
    third_hash = "66" * 32
    conn = insert_mock_item(sqlite_operator, first_hash, artist="Artist One")
    conn.close()
    conn = insert_mock_item(sqlite_operator, second_hash, artist="artist one")
    conn.close()
    conn = insert_mock_item(sqlite_operator, third_hash, artist="Unknown")

    artists.ensure_artist_schema(conn)
    artists.ensure_artist_schema(conn)
    rows = conn.execute("SELECT name, name_norm, kind FROM artists").fetchall()
    conn.close()

    assert rows == [("Artist One", "artist one", "artist")]


def test_artist_resolver_uses_aliases_and_skips_placeholders(monkeypatch, tmp_path):
    sqlite_operator, artists = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "artists")
    conn = sqlite_operator.init_database()
    canonical = artists.resolve_artist_name(conn, "Canonical Artist")
    artist_id = conn.execute("SELECT id FROM artists WHERE name = ?", (canonical,)).fetchone()[0]
    artists.add_artist_alias(conn, artist_id, "Old Artist")

    assert artists.resolve_artist_name(conn, "old artist") == "Canonical Artist"
    assert artists.resolve_artist_name(conn, "Unknown") == "Unknown"
    assert conn.execute("SELECT 1 FROM artists WHERE name_norm = 'unknown'").fetchone() is None
    conn.close()


def test_platform_schema_backfills_and_api_lists(monkeypatch, tmp_path):
    sqlite_operator, platforms_module, workspace_db, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "platforms", "workspace_db", "api")
    conn = insert_mock_item(sqlite_operator, "67" * 32, artist="Platform Artist")
    conn.execute("UPDATE items SET platform = ? WHERE hash = ?", ("twitter", "67" * 32))
    conn.commit()
    conn.close()
    workspace_conn = workspace_db.connect_workspace_database()
    platforms_module.resolve_platform_label(workspace_conn, "Fanbox", create=True)
    workspace_conn.commit()
    workspace_conn.close()

    platforms = api.library._get_platforms_sync("", 20)["items"]
    used_platforms = api.library._get_platforms_sync("", 20, "used")["items"]
    x_platform = next(item for item in platforms if item["display_name"] == "X")

    assert x_platform["key_norm"] == "x"
    assert x_platform["item_count"] == 1
    assert any(item["display_name"] == "Fanbox" and item["item_count"] == 0 for item in platforms)
    assert all(item["item_count"] > 0 for item in used_platforms)


def test_stats_scope_used_and_all_for_artists_and_topics(monkeypatch, tmp_path):
    utils, sqlite_operator, artists_module, topics_module, metadata_index, workspace_db, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "artists",
        "topics",
        "metadata_index",
        "workspace_db",
        "api",
    )
    item_hash = "68" * 32
    conn = insert_mock_item(sqlite_operator, item_hash, artist="Used Artist")
    conn.commit()
    conn.close()
    workspace_conn = workspace_db.connect_workspace_database()
    artists_module.resolve_artist_name(workspace_conn, "Unused Artist", create=True)
    workspace_conn.commit()
    workspace_conn.close()

    api.library._update_item_sync(item_hash, api.library.ItemUpdate(topics=["used topic"]))
    api.library._update_item_sync(item_hash, api.library.ItemUpdate(wd_tags=["used wd tag"]))
    topics_module.ensure_topic_file("unused topic")
    workspace_conn = workspace_db.connect_workspace_database()
    workspace_db.upsert_wd_dictionary_tags(workspace_conn, [("general", "unused wd tag")])
    workspace_conn.commit()
    workspace_conn.close()
    conn = sqlite_operator.connect_database()
    metadata_index.rebuild_all_metadata(conn)
    conn.commit()
    conn.close()

    all_artists = api.library._get_artists_sync("", 20, "all")["items"]
    used_artists = api.library._get_artists_sync("", 20, "used")["items"]
    all_topics = api.library._get_facets_sync("topic", "", 20, "all")["items"]
    used_topics = api.library._get_facets_sync("topic", "", 20, "used")["items"]
    all_wd = api.library._get_facets_sync("wd_tag", "", 20, "all")["items"]
    used_wd = api.library._get_facets_sync("wd_tag", "", 20, "used")["items"]

    assert any(item["name"] == "Unused Artist" and item["item_count"] == 0 for item in all_artists)
    assert all(item["item_count"] > 0 for item in used_artists)
    assert any(item == {"value": "unused_topic", "count": 0} for item in all_topics)
    assert all(item["count"] > 0 for item in used_topics)
    assert any(item == {"value": "unused wd tag", "count": 0, "tag_type": "general"} for item in all_wd)
    assert all(item["count"] > 0 for item in used_wd)


def test_artist_api_lists_details_and_edits(monkeypatch, tmp_path):
    utils, sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "api")
    item_hash = "63" * 32
    conn = insert_mock_item(sqlite_operator, item_hash, artist="Artist API")
    conn.close()

    listing = api.library._get_artists_sync("", 10)
    artist = next(item for item in listing["items"] if item["name"] == "Artist API")
    detail = api.library._get_artist_sync(artist["id"])
    alias = api.library._post_artist_alias_sync(artist["id"], api.library.ArtistAliasCreate(alias="API Alias"))
    link = api.library._post_artist_link_sync(
        artist["id"],
        api.library.ArtistLinkCreate(platform="twitter2", url="https://x.com/api_artist"),
    )
    updated = api.library._patch_artist_sync(
        artist["id"],
        api.library.ArtistUpdate(name="Artist Canonical", kind="brand", notes="note"),
    )

    assert artist["item_count"] == 1
    assert detail["name"] == "Artist API"
    assert alias["alias_norm"] == "api alias"
    assert link["platform"] == "X"
    assert link["handle"] == "api_artist"
    assert updated["name"] == "Artist Canonical"
    assert updated["kind"] == "brand"
    assert updated["notes"] == "note"
    conn = sqlite_operator.init_database()
    row = conn.execute("SELECT source_artist, storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
    assert row[0] == "Artist Canonical"
    note_data = frontmatter_from_markdown(utils.note_path_for(item_hash, row[1]).read_text(encoding="utf-8"))
    assert note_data["artist"] == "Artist Canonical"
    conn.close()

    deleted_alias = api.library._delete_alias_sync(artist["id"], alias["id"])
    deleted_link = api.library._delete_link_sync(artist["id"], link["id"])
    assert deleted_alias == {"status": "success"}
    assert deleted_link == {"status": "success"}


def test_artist_api_rejects_duplicate_names_and_aliases(monkeypatch, tmp_path):
    sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "api")
    conn = insert_mock_item(sqlite_operator, "64" * 32, artist="Artist A")
    conn.close()
    conn = insert_mock_item(sqlite_operator, "65" * 32, artist="Artist B")
    conn.close()

    artists = api.library._get_artists_sync("", 10)["items"]
    artist_a = next(item for item in artists if item["name"] == "Artist A")
    artist_b = next(item for item in artists if item["name"] == "Artist B")

    with pytest.raises(HTTPException) as duplicate_name:
        api.library._patch_artist_sync(artist_b["id"], api.library.ArtistUpdate(name="Artist A"))
    assert duplicate_name.value.status_code == 409

    with pytest.raises(HTTPException) as duplicate_alias:
        api.library._post_artist_alias_sync(artist_a["id"], api.library.ArtistAliasCreate(alias="Artist B"))
    assert duplicate_alias.value.status_code == 409

    with pytest.raises(HTTPException) as empty_link:
        api.library._post_artist_link_sync(artist_a["id"], api.library.ArtistLinkCreate(platform="", url=""))
    assert empty_link.value.status_code == 400

    with pytest.raises(HTTPException) as placeholder_name:
        api.library._patch_artist_sync(artist_a["id"], api.library.ArtistUpdate(name="Unknown"))
    assert placeholder_name.value.status_code == 400


def test_artist_merge_absorbs_sources_and_rewrites_items(monkeypatch, tmp_path):
    utils, sqlite_operator, artists, workspace_db, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "artists",
        "workspace_db",
        "api",
    )
    target_hash = "68" * 32
    source_hash = "69" * 32
    second_source_hash = "70" * 32
    alias_hash = "71" * 32
    for item_hash, artist_name in [
        (target_hash, "iomayashi"),
        (source_hash, "nixeu"),
        (second_source_hash, "iomaya"),
        (alias_hash, "old nix"),
    ]:
        conn = insert_mock_item(sqlite_operator, item_hash, artist=artist_name)
        md_content = api.common.generate_markdown(conn, item_hash)
        row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
        utils.atomic_write_text(utils.note_path_for(item_hash, row[0]), md_content)
        conn.close()

    listing = api.library._get_artists_sync("", 20)["items"]
    ids = {artist["name"]: artist["id"] for artist in listing}
    workspace_conn = workspace_db.connect_workspace_database()
    workspace_conn.execute("DELETE FROM artists WHERE id = ?", (ids["old nix"],))
    artists.add_artist_alias(workspace_conn, ids["nixeu"], "old nix")
    artists.add_artist_link(workspace_conn, ids["iomayashi"], "X", "https://x.com/shared")
    artists.add_artist_link(workspace_conn, ids["nixeu"], "X", "https://x.com/shared")
    artists.add_artist_link(workspace_conn, ids["iomaya"], "Pixiv", "https://www.pixiv.net/users/iomaya")
    workspace_conn.execute("UPDATE artists SET notes = ? WHERE id = ?", ("old nixeu notes", ids["nixeu"]))
    workspace_conn.execute("UPDATE artists SET notes = ? WHERE id = ?", ("old iomaya notes", ids["iomaya"]))
    workspace_conn.commit()
    workspace_conn.close()

    preview = api.library._preview_artist_merge_sync(
        ids["iomayashi"],
        api.library.ArtistMergeRequest(source_artist_ids=[ids["nixeu"], ids["iomaya"]]),
    )
    assert preview["affected_items"] == 3
    assert {alias["value"] for alias in preview["aliases"]["add"]} == {"nixeu", "iomaya"}
    assert {alias["value"] for alias in preview["aliases"]["move"]} == {"old nix"}
    assert len(preview["links"]["move"]) == 1
    assert len(preview["links"]["duplicates"]) == 1
    assert preview["notes_appended"] == 2
    conn = sqlite_operator.init_database()
    workspace_conn = workspace_db.connect_workspace_database()
    assert workspace_conn.execute("SELECT name FROM artists WHERE id = ?", (ids["nixeu"],)).fetchone()[0] == "nixeu"
    assert conn.execute("SELECT source_artist FROM items WHERE hash = ?", (source_hash,)).fetchone()[0] == "nixeu"
    assert workspace_conn.execute("SELECT artist_id FROM artist_links WHERE url_norm = ?", ("https://www.pixiv.net/users/iomaya",)).fetchone()[0] == ids["iomaya"]
    conn.close()
    workspace_conn.close()

    merged = api.library._merge_artist_sync(
        ids["iomayashi"],
        api.library.ArtistMergeRequest(source_artist_ids=[ids["nixeu"], ids["iomaya"]]),
    )
    assert merged["merged"] is True
    assert merged["target_detail"]["name"] == "iomayashi"
    assert "merged from nixeu" in merged["target_detail"]["notes"]
    assert "old iomaya notes" in merged["target_detail"]["notes"]

    conn = sqlite_operator.init_database()
    workspace_conn = workspace_db.connect_workspace_database()
    names = {row[0] for row in workspace_conn.execute("SELECT name FROM artists").fetchall()}
    assert "iomayashi" in names
    assert "nixeu" not in names
    assert "iomaya" not in names
    aliases = {row[0] for row in workspace_conn.execute("SELECT alias FROM artist_aliases WHERE artist_id = ?", (ids["iomayashi"],)).fetchall()}
    assert {"nixeu", "iomaya", "old nix"} <= aliases
    item_artists = {
        row[0]
        for row in conn.execute(
            "SELECT source_artist FROM items WHERE hash IN (?, ?, ?)",
            (source_hash, second_source_hash, alias_hash),
        ).fetchall()
    }
    assert item_artists == {"iomayashi"}
    links = workspace_conn.execute("SELECT platform, url FROM artist_links WHERE artist_id = ?", (ids["iomayashi"],)).fetchall()
    assert len(links) == 2
    assert artists.resolve_artist_name(workspace_conn, "nixeu") == "iomayashi"
    row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (source_hash,)).fetchone()
    note_data = frontmatter_from_markdown(utils.note_path_for(source_hash, row[0]).read_text(encoding="utf-8"))
    assert note_data["artist"] == "iomayashi"
    conn.close()
    workspace_conn.close()


def test_artist_merge_preview_reports_alias_conflicts_without_mutating(monkeypatch, tmp_path):
    sqlite_operator, artists, workspace_db, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "artists", "workspace_db", "api")
    conn = insert_mock_item(sqlite_operator, "73" * 32, artist="Target Merge")
    conn.close()
    conn = insert_mock_item(sqlite_operator, "74" * 32, artist="Source Merge")
    conn.close()
    conn = insert_mock_item(sqlite_operator, "75" * 32, artist="Unrelated Merge")
    conn.close()

    listing = api.library._get_artists_sync("", 20)["items"]
    ids = {artist["name"]: artist["id"] for artist in listing}
    workspace_conn = workspace_db.connect_workspace_database()
    artists.add_artist_alias(workspace_conn, ids["Source Merge"], "conflict alias")
    workspace_conn.execute(
        "INSERT INTO artist_aliases(artist_id, alias, alias_norm, created_at) VALUES (?, ?, ?, ?)",
        (ids["Unrelated Merge"], "Source Merge", "source merge", "2026-01-01 00:00:00"),
    )
    workspace_conn.commit()
    workspace_conn.close()

    preview = api.library._preview_artist_merge_sync(
        ids["Target Merge"],
        api.library.ArtistMergeRequest(source_artist_ids=[ids["Source Merge"]]),
    )

    assert {alias["value"] for alias in preview["aliases"]["move"]} == {"conflict alias"}
    assert {alias["value"] for alias in preview["aliases"]["conflicts"]} == {"Source Merge"}

    workspace_conn = workspace_db.connect_workspace_database()
    before_alias_owner = workspace_conn.execute("SELECT artist_id FROM artist_aliases WHERE alias_norm = 'conflict alias'").fetchone()[0]
    assert before_alias_owner == ids["Source Merge"]
    assert workspace_conn.execute("SELECT name FROM artists WHERE id = ?", (ids["Source Merge"],)).fetchone()[0] == "Source Merge"
    assert workspace_conn.execute("SELECT artist_id FROM artist_aliases WHERE alias_norm = 'source merge'").fetchone()[0] == ids["Unrelated Merge"]
    workspace_conn.close()


def test_artist_merge_rejects_invalid_sources(monkeypatch, tmp_path):
    sqlite_operator, workspace_db, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "workspace_db", "api")
    conn = insert_mock_item(sqlite_operator, "72" * 32, artist="Merge Target")
    workspace_conn = workspace_db.connect_workspace_database()
    workspace_conn.execute(
        """
        INSERT INTO artists(name, name_norm, kind, notes, created_at, updated_at)
        VALUES ('Unknown', 'unknown', 'artist', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
        """
    )
    workspace_conn.commit()
    placeholder_id = workspace_conn.execute("SELECT id FROM artists WHERE name_norm = 'unknown'").fetchone()[0]
    workspace_conn.close()
    conn.close()
    artist = next(item for item in api.library._get_artists_sync("", 10)["items"] if item["name"] == "Merge Target")

    with pytest.raises(HTTPException) as empty_sources:
        api.library._preview_artist_merge_sync(artist["id"], api.library.ArtistMergeRequest(source_artist_ids=[]))
    assert empty_sources.value.status_code == 400

    with pytest.raises(HTTPException) as self_merge:
        api.library._merge_artist_sync(artist["id"], api.library.ArtistMergeRequest(source_artist_ids=[artist["id"]]))
    assert self_merge.value.status_code == 400

    with pytest.raises(HTTPException) as placeholder_source:
        api.library._merge_artist_sync(artist["id"], api.library.ArtistMergeRequest(source_artist_ids=[placeholder_id]))
    assert placeholder_source.value.status_code == 400

    with pytest.raises(HTTPException) as placeholder_target:
        api.library._merge_artist_sync(placeholder_id, api.library.ArtistMergeRequest(source_artist_ids=[artist["id"]]))
    assert placeholder_target.value.status_code == 400


def test_search_manager_query_sees_pending_during_vp_rebuild(monkeypatch, tmp_path):
    search_manager_module, = fresh_backend(monkeypatch, tmp_path, "db.search_manager")
    manager = search_manager_module.SearchManager()
    manager.video_tree = search_manager_module.VPTreeSearcher(search_manager_module._cosine_dist)
    manager.audio_tree = search_manager_module.VPTreeSearcher(search_manager_module._hamming_dist_audio)
    manager.global_tree = search_manager_module.BKTreeSearcher()
    manager.tile_tree = search_manager_module.BKTreeSearcher()
    manager.url_registry = search_manager_module.URLRegistry()

    import numpy as np

    sig = np.array([1.0, 0.0], dtype=np.float32).tobytes()
    manager.video_tree.add("video-hash", sig)
    started = threading.Event()
    release = threading.Event()
    original_make_tree = search_manager_module.VPTreeSearcher._make_tree

    def slow_make_tree(self, items):
        started.set()
        release.wait(timeout=2)
        return original_make_tree(self, items)

    monkeypatch.setattr(search_manager_module.VPTreeSearcher, "_make_tree", slow_make_tree)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.rebuild_deferred_indexes)
        assert started.wait(timeout=2)
        matches = manager.query_video(None, sig, ai_threshold=0.01)
        release.set()
        future.result(timeout=2)

    assert matches == [("video-hash", 1.0, "Semantic")]


def test_search_manager_removes_deleted_hashes_and_urls(monkeypatch, tmp_path):
    search_manager_module, = fresh_backend(monkeypatch, tmp_path, "db.search_manager")
    manager = search_manager_module.SearchManager()
    manager.video_tree = search_manager_module.VPTreeSearcher(search_manager_module._cosine_dist)
    manager.audio_tree = search_manager_module.VPTreeSearcher(search_manager_module._hamming_dist_audio)
    manager.global_tree = search_manager_module.BKTreeSearcher()
    manager.tile_tree = search_manager_module.BKTreeSearcher()
    manager.url_registry = search_manager_module.URLRegistry()

    import numpy as np

    phash = "0" * 16
    tile_phash = "f" * 16
    sig = np.array([1.0, 0.0], dtype=np.float32).tobytes()
    manager.update_indexes("deleted-hash", phash, "https://example.test/item", [(0, tile_phash)], visual_embedding=sig)
    manager.rebuild_deferred_indexes()

    assert manager.query_image(phash)
    assert manager.query_image(tile_phash)
    assert manager.query_video(None, sig, ai_threshold=0.01)
    assert manager.url_exists("https://example.test/item")

    manager.remove_indexes_batch([{"hash": "deleted-hash", "source_url": "https://example.test/item"}])

    assert manager.query_image(phash) == []
    assert manager.query_image(tile_phash) == []
    assert manager.query_video(None, sig, ai_threshold=0.01) == []
    assert not manager.url_exists("https://example.test/item")


def test_search_manager_vp_delete_defers_rebuild_outside_remove(monkeypatch, tmp_path):
    search_manager_module, = fresh_backend(monkeypatch, tmp_path, "db.search_manager")
    manager = search_manager_module.SearchManager()
    manager.video_tree = search_manager_module.VPTreeSearcher(search_manager_module._cosine_dist)
    manager.audio_tree = search_manager_module.VPTreeSearcher(search_manager_module._hamming_dist_audio)
    manager.global_tree = search_manager_module.BKTreeSearcher()
    manager.tile_tree = search_manager_module.BKTreeSearcher()
    manager.url_registry = search_manager_module.URLRegistry()

    import numpy as np

    sig = np.array([1.0, 0.0], dtype=np.float32).tobytes()
    manager.update_indexes("deleted-video", None, None, visual_embedding=sig)
    manager.rebuild_deferred_indexes()
    assert manager.query_video(None, sig, ai_threshold=0.01)

    def fail_sync_rebuild(self, items):
        raise AssertionError("remove should not rebuild VP tree synchronously")

    scheduled = []
    monkeypatch.setattr(search_manager_module.VPTreeSearcher, "_make_tree", fail_sync_rebuild)
    monkeypatch.setattr(manager, "_rebuild_deferred_indexes_async", lambda reason: scheduled.append(reason))

    result = manager.remove_indexes_batch([{"hash": "deleted-video"}])

    assert result["video"]["deferred"] is True
    assert scheduled == ["batch_remove"]
    assert manager.query_video(None, sig, ai_threshold=0.01) == []


def test_vp_rebuild_apply_does_not_resurrect_concurrent_delete(monkeypatch, tmp_path):
    search_manager_module, = fresh_backend(monkeypatch, tmp_path, "db.search_manager")
    tree = search_manager_module.VPTreeSearcher(search_manager_module._cosine_dist)

    import numpy as np

    sig_x = np.array([1.0, 0.0], dtype=np.float32).tobytes()
    sig_y = np.array([0.0, 1.0], dtype=np.float32).tobytes()
    tree.add("x", sig_x)
    tree.add("y", sig_y)
    tree.build_index()
    tree.dirty = True

    plan = tree.rebuild_plan()
    replacement = tree.build_replacement(plan)

    removed = tree.remove_hashes({"y"})
    assert removed["removed"] == 1

    applied = tree.apply_replacement(plan, replacement)
    assert applied["stale"] is True
    assert [item_hash for item_hash, _ in tree.indexed_items] == ["x"]
    assert tree.query(sig_y, 0.01) == []

    tree.build_index()
    assert tree.query(sig_x, 0.01) == [("x", 0.0)]
    assert tree.query(sig_y, 0.01) == []


def test_find_visual_duplicate_stops_tile_queries_after_first_match(monkeypatch, tmp_path):
    processor, = fresh_backend(monkeypatch, tmp_path, "processor")
    calls = []

    class FakeSearchManager:
        def query_image(self, phash, threshold):
            return []

        def query_global_only(self, phash, threshold):
            calls.append(phash)
            return [("match-hash", 1)]

    monkeypatch.setattr(processor, "search_manager", FakeSearchManager())

    match, match_type, total, distance = processor.find_visual_duplicate(
        "0" * 16,
        new_tiles=[(0, "tile-a"), (1, "tile-b"), (2, "tile-c")],
    )

    assert match == "match-hash"
    assert match_type == "Whole-to-Fragment (Tile #0)"
    assert total == 1
    assert distance == 1
    assert calls == ["tile-a"]


def test_hot_ingestion_paths_use_lightweight_db_helper(monkeypatch, tmp_path):
    processor, external_ingestion = fresh_backend(monkeypatch, tmp_path, "processor", "external_ingestion")

    assert "connect_database()" in inspect.getsource(processor.process_file)
    assert "connect_database()" in inspect.getsource(external_ingestion.ExternalIngestor._url_complete)
    assert "connect_database()" in inspect.getsource(external_ingestion.ExternalIngestor._instagram_complete)
    assert "connect_database()" in inspect.getsource(external_ingestion.ExternalIngestor._rollback_batch)


def test_online_process_metadata_keeps_only_online_identity(monkeypatch, tmp_path):
    external_ingestion, = fresh_backend(monkeypatch, tmp_path, "external_ingestion")

    metadata = external_ingestion._online_process_metadata(
        {
            "source_url": "https://example.test/item",
            "platform": "Pixiv",
            "artist": "Scraped Artist",
            "title": "Scraped Title",
            "unexpected": "raw scraper field",
        },
        "normal_pending_links",
        {"artist": "User Artist", "platform": "User Platform"},
    )

    assert metadata == {
        "source_url": "https://example.test/item",
        "platform": "User Platform",
        "artist": "User Artist",
        "ingest_type": "online",
        "run_id": "normal_pending_links",
    }


def test_online_worker_passes_explicit_queue_metadata_to_processor(monkeypatch, tmp_path):
    queue_service, external_ingestion = fresh_backend(monkeypatch, tmp_path, "queue_service", "external_ingestion")
    media_path = tmp_path / "download.jpg"
    media_path.write_bytes(b"image")
    captured = {}

    monkeypatch.setattr(external_ingestion.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(external_ingestion.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(external_ingestion.ExternalIngestor, "_url_complete", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        external_ingestion,
        "download_video",
        lambda *_args, **_kwargs: (
            True,
            {
                "file_paths": [str(media_path)],
                "metadata": {
                    "source_url": "https://youtube.com/watch?v=abc",
                    "platform": "YouTube",
                    "artist": "Scraped Artist",
                    "title": "Scraped Title",
                },
            },
        ),
    )

    def fake_process_file(_path, _config, **kwargs):
        captured.update(kwargs.get("metadata") or {})
        return True, "ok", {"hash": "11" * 32}

    monkeypatch.setattr(external_ingestion, "process_file", fake_process_file)
    ingestor = external_ingestion.ExternalIngestor(str(tmp_path / "links.md"))
    entry = queue_service.QueueEntry(
        "https://youtube.com/watch?v=abc",
        "User Artist",
        "User Platform",
        0,
        1,
    )

    success, _url, stats, index_data = ingestor._worker_item("youtube", entry, [0, 0])

    assert success is True
    assert stats["processed"] == 1
    assert index_data == [{"hash": "11" * 32}]
    assert captured == {
        "source_url": "https://youtube.com/watch?v=abc",
        "platform": "User Platform",
        "artist": "User Artist",
        "ingest_type": "online",
        "run_id": "links",
    }


def test_downloader_wrappers_use_bounded_default_timeouts(monkeypatch, tmp_path):
    gallery, yt_dlp = fresh_backend(monkeypatch, tmp_path, "downloaders.gallery_dl_wrapper", "downloaders.yt_dlp_wrapper")
    gallery_timeouts = []
    yt_timeouts = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "failed"

    def gallery_run(*args, **kwargs):
        gallery_timeouts.append(kwargs.get("timeout"))
        return Result()

    def yt_run(*args, **kwargs):
        yt_timeouts.append(kwargs.get("timeout"))
        return Result()

    monkeypatch.setattr(gallery, "_base_args", lambda url: [])
    monkeypatch.setattr(gallery.subprocess, "run", gallery_run)
    gallery.inspect_gallery("https://example.test/item")
    gallery.download_gallery("https://example.test/item", metadata_info={"platform": "X", "download_url": "https://example.test/item"})

    monkeypatch.setattr(yt_dlp, "get_platform_cookie_path", lambda platform: {"status": "missing", "source": "missing", "path": ""})
    monkeypatch.setattr(yt_dlp.subprocess, "run", yt_run)
    yt_dlp.download_video("https://www.youtube.com/watch?v=mock")

    assert gallery_timeouts[:2] == [120, 300]
    assert yt_timeouts[:2] == [120, 600]


def test_gallery_dl_uses_platform_cookie(monkeypatch, tmp_path):
    gallery, utils = fresh_backend(monkeypatch, tmp_path, "downloaders.gallery_dl_wrapper", "utils")
    platform_path = utils.platform_cookie_path("x")
    write_cookie_file(platform_path, ".x.com", "ct0")

    args = gallery._base_args("https://x.com/mock/status/1")

    assert args[args.index("--cookies") + 1] == str(platform_path)


def test_gallery_dl_pixiv_oauth_wins_over_cookie(monkeypatch, tmp_path):
    gallery, utils = fresh_backend(monkeypatch, tmp_path, "downloaders.gallery_dl_wrapper", "utils")
    write_cookie_file(utils.platform_cookie_path("pixiv"), ".pixiv.net", "PHPSESSID")
    monkeypatch.setattr(gallery, "get_pixiv_refresh_token", lambda: {
        "token": "secret-token",
        "status": "available",
        "source": "file",
        "path": str(utils.pixiv_refresh_token_path()),
    })

    args = gallery._base_args("https://www.pixiv.net/artworks/1")

    assert "--cookies" not in args
    assert "extractor.pixiv.refresh-token=secret-token" in args


def test_yt_dlp_uses_youtube_platform_cookie(monkeypatch, tmp_path):
    yt_dlp, utils = fresh_backend(monkeypatch, tmp_path, "downloaders.yt_dlp_wrapper", "utils")
    youtube_cookie = utils.platform_cookie_path("youtube")
    write_cookie_file(youtube_cookie, ".youtube.com", "SID")
    commands = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "failed"

    def run(command, **kwargs):
        commands.append(command)
        return Result()

    monkeypatch.setattr(yt_dlp.subprocess, "run", run)
    yt_dlp.download_video("https://www.youtube.com/watch?v=mock")

    assert len(commands) >= 2
    assert commands[0][commands[0].index("--cookies") + 1] == str(youtube_cookie)
    assert commands[1][commands[1].index("--cookies") + 1] == str(youtube_cookie)


def test_downloader_wrapper_timeout_defaults(monkeypatch, tmp_path):
    gallery, yt_dlp = fresh_backend(monkeypatch, tmp_path, "downloaders.gallery_dl_wrapper", "downloaders.yt_dlp_wrapper")

    assert gallery._timeout("gallery_metadata", 120) == 120
    assert gallery._timeout("gallery_download", 300) == 300
    assert yt_dlp._timeout("yt_dlp_metadata", 120) == 120
    assert yt_dlp._timeout("yt_dlp_download", 600) == 600


def test_thumbnail_repair_uses_shared_helper_and_skips_fresh(monkeypatch, tmp_path):
    utils, sqlite_operator, thumbnails = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "thumbnails")
    fresh_hash = "a" * 64
    stale_hash = "b" * 64
    conn = insert_mock_item(sqlite_operator, fresh_hash)
    fresh_storage_id = storage_id_for(conn, fresh_hash)
    stale_storage_id = sqlite_operator.allocate_storage_id(conn)
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, ?, '', '', 'local', ?, '')
        """,
        (stale_hash, stale_storage_id, f"{stale_hash}.jpg", "2026-01-03 00:00:00", "DB Artist"),
    )
    conn.commit()
    for item_hash in [fresh_hash, stale_hash]:
        storage_id = fresh_storage_id if item_hash == fresh_hash else stale_storage_id
        asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"asset")
    fresh_thumb = thumbnails.thumbnail_path_for(fresh_hash, fresh_storage_id)
    fresh_thumb.parent.mkdir(parents=True, exist_ok=True)
    fresh_thumb.write_bytes(b"thumb")
    calls = []

    def fake_ensure(item_hash, extension, mime_type, wait=True, storage_id=None):
        calls.append((item_hash, extension, mime_type, wait, storage_id))
        return thumbnails.thumbnail_path_for(item_hash, storage_id)

    monkeypatch.setattr(thumbnails, "ensure_thumbnail", fake_ensure)

    result = thumbnails.repair_missing_thumbnails(conn, limit=10)

    assert calls == [(stale_hash, ".jpg", "image/jpeg", True, stale_storage_id)]
    assert result["generated"] == 1
    assert result["skipped"] == 1
    conn.close()


def test_thumbnail_repair_removes_stale_image_and_video_copies_even_when_fresh(monkeypatch, tmp_path):
    utils, sqlite_operator, thumbnails = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "thumbnails")
    image_hash = "c1" * 32
    video_hash = "c2" * 32
    conn = insert_mock_item(sqlite_operator, image_hash)
    image_storage = storage_id_for(conn, image_hash)
    video_storage = sqlite_operator.allocate_storage_id(conn)
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.mp4', 'video/mp4', 10, '2026-01-04 00:00:00', '', '', 'local', 'DB Artist', '')
        """,
        (video_hash, video_storage, f"{video_hash}.mp4"),
    )
    conn.commit()
    for item_hash, storage_id, extension, mime_type in (
        (image_hash, image_storage, ".jpg", "image/jpeg"),
        (video_hash, video_storage, ".mp4", "video/mp4"),
    ):
        asset = utils.asset_path_for(item_hash, extension, mime_type, storage_id=storage_id)
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"asset")
    image_thumb = thumbnails.thumbnail_path_for(image_hash, image_storage)
    video_thumb = thumbnails.video_thumbnail_path_for(video_hash, video_storage)
    stale_image = thumbnails._thumbnail_dir() / "aa" / f"{image_storage}.jpg"
    stale_video = thumbnails._thumbnail_dir() / "bb" / f"{video_storage}_video.jpg"
    stale_video_image_variant = thumbnails._thumbnail_dir() / "cc" / f"{video_storage}.jpg"
    for path in (image_thumb, video_thumb, stale_image, stale_video, stale_video_image_variant):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"thumb")
    now = time.time() + 10
    os.utime(image_thumb, (now, now))
    os.utime(video_thumb, (now, now))

    result = thumbnails.repair_missing_thumbnails(conn, limit=10)

    assert result["generated"] == 0
    assert result["skipped"] == 2
    assert result["stale_removed"] == 3
    assert result["cleanup_errors"] == []
    assert image_thumb.exists()
    assert video_thumb.exists()
    assert not stale_image.exists()
    assert not stale_video.exists()
    assert not stale_video_image_variant.exists()
    conn.close()


def test_thumbnail_repair_reports_stale_cleanup_failure(monkeypatch, tmp_path):
    utils, sqlite_operator, thumbnails = fresh_backend(monkeypatch, tmp_path, "utils", "db.sqlite_operator", "thumbnails")
    item_hash = "c3" * 32
    conn = insert_mock_item(sqlite_operator, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    canonical = thumbnails.thumbnail_path_for(item_hash, storage_id)
    stale = thumbnails._thumbnail_dir() / "dd" / f"{storage_id}.jpg"
    for path in (asset, canonical, stale):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    now = time.time() + 10
    os.utime(canonical, (now, now))
    real_unlink = Path.unlink

    def fail_stale_unlink(self, *args, **kwargs):
        if self == stale:
            raise OSError("stale thumbnail locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)

    result = thumbnails.repair_missing_thumbnails(conn, limit=10)

    assert result["stale_removed"] == 0
    assert result["cleanup_errors"]
    assert result["cleanup_errors"][0]["path"] == str(stale)
    assert stale.exists()
    conn.close()


def test_thumbnail_and_wd_repairs_ignore_non_media_rows(monkeypatch, tmp_path):
    utils, sqlite_operator, thumbnails, vaults = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "thumbnails",
        "vaults",
    )
    conn = sqlite_operator.init_database()
    storage_id = sqlite_operator.allocate_storage_id(conn)
    item_hash = "c4" * 32
    conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.txt', 'text/plain', 10, '2026-01-04 00:00:00', '', '', 'local', 'DB Artist', '')
        """,
        (item_hash, storage_id, f"{item_hash}.txt"),
    )
    conn.commit()
    asset = utils.asset_path_for(item_hash, ".txt", "text/plain", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("text", encoding="utf-8")
    monkeypatch.setattr(thumbnails, "ensure_thumbnail", lambda *args, **kwargs: pytest.fail("non-media thumbnail generated"))

    thumb_result = thumbnails.repair_missing_thumbnails(conn, limit=10)
    wd_result = vaults._repair_missing_wd_cache(conn, vaults._ctx_for_vault("default"))

    assert thumb_result["checked"] == 0
    assert wd_result["checked"] == 0
    conn.close()


def test_thumbnail_api_returns_503_when_generation_is_busy(monkeypatch, tmp_path):
    sqlite_operator, api = fresh_backend(monkeypatch, tmp_path, "db.sqlite_operator", "api")
    item_hash = "c" * 64
    conn = insert_mock_item(sqlite_operator, item_hash)
    conn.close()

    def busy(*args, **kwargs):
        raise api.common.ThumbnailBusyError("busy")

    monkeypatch.setattr(api.library, "get_or_generate_thumbnail", busy)

    with pytest.raises(HTTPException) as exc:
        api.library._get_thumbnail_sync(item_hash)

    assert exc.value.status_code == 503


def test_no_duplicate_thumbnail_generation_paths_outside_thumbnail_module(monkeypatch, tmp_path):
    processor, api = fresh_backend(monkeypatch, tmp_path, "processor", "api")

    processor_source = inspect.getsource(processor)
    api_source = "\n".join(
        inspect.getsource(module)
        for module in (
            api.app,
            api.app_settings,
            api.capture,
            api.common,
            api.guards,
            api.ingestion,
            api.library,
            api.logs,
            api.review,
            api.runtime,
        )
    )

    assert "ensure_thumbnail(" in inspect.getsource(processor.process_file)
    assert "get_or_generate_thumbnail(" in inspect.getsource(api.library._get_thumbnail_sync)
    assert "generate_image_thumbnail(" not in processor_source
    assert "generate_video_thumbnail(" not in processor_source
    assert "generate_image_thumbnail(" not in api_source
    assert "generate_video_thumbnail(" not in api_source


def test_sampled_video_extraction_uses_one_ffmpeg_subprocess(monkeypatch, tmp_path):
    fingerprint, = fresh_backend(monkeypatch, tmp_path, "fingerprint")
    calls = []
    kwargs_seen = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        kwargs_seen.append(kwargs)
        for value in cmd:
            if isinstance(value, str) and value.endswith(".png"):
                path = Path(value)
                path.parent.mkdir(parents=True, exist_ok=True)
                from PIL import Image
                image = Image.new("RGB", (2, 2), "red")
                image.save(path)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fingerprint, "get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(fingerprint.subprocess, "run", fake_run)

    frames = fingerprint.extract_sampled_video_frames(tmp_path / "video.mp4", frame_count=5)

    assert len(frames) == 5
    assert len(calls) == 1
    assert calls[0].count("-i") == 5
    assert kwargs_seen[0]["stderr"] == fingerprint.subprocess.DEVNULL


def test_calculate_phash_logs_failures_with_traceback(monkeypatch, tmp_path):
    utils, = fresh_backend(monkeypatch, tmp_path, "utils")
    logger = importlib.import_module("logger")
    calls = []

    def fake_log_system(level, message, **kwargs):
        calls.append((level, message, kwargs))

    def fail_open(path):
        raise OSError("bad image")

    monkeypatch.setattr(logger, "log_system", fake_log_system)
    monkeypatch.setitem(sys.modules, "imagehash", types.SimpleNamespace(phash=lambda img: "unused"))
    from PIL import Image as PILImage
    monkeypatch.setattr(PILImage, "open", fail_open)

    assert utils.calculate_phash(tmp_path / "bad.jpg") is None
    assert calls == [
        (
            "WARNING",
            "Image perceptual hash failed",
            {"file": str(tmp_path / "bad.jpg"), "exc_info": True},
        )
    ]


def test_logger_helper_writes_traceback_to_json(monkeypatch, tmp_path):
    logger_impl, = fresh_backend(monkeypatch, tmp_path, "logger.logger")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logger_impl.JSONFormatter())
    test_logger = logging.getLogger("lmz_test_exc_info")
    old_handlers = test_logger.handlers[:]
    old_propagate = test_logger.propagate
    test_logger.handlers = [handler]
    test_logger.propagate = False

    try:
        try:
            raise RuntimeError("logged failure")
        except RuntimeError:
            logger_impl._log(test_logger, "WARNING", "Traceback check", file="bad.jpg", exc_info=True)

        record = json.loads(stream.getvalue())
        assert record["message"] == "Traceback check"
        assert record["file"] == "bad.jpg"
        assert "RuntimeError: logged failure" in record["exception"]
    finally:
        test_logger.handlers = old_handlers
        test_logger.propagate = old_propagate


def test_ingest_log_helpers_route_to_separate_streams(monkeypatch, tmp_path):
    logger_impl, = fresh_backend(monkeypatch, tmp_path, "logger.logger")
    streams = {}
    targets = {
        "local": logger_impl.ingest_local_logger,
        "online": logger_impl.ingest_online_logger,
        "audit": logger_impl.activity_logger,
    }
    old_state = {}

    for name, target in targets.items():
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logger_impl.JSONFormatter())
        streams[name] = stream
        old_state[name] = (target.handlers[:], target.propagate)
        target.handlers = [handler]
        target.propagate = False

    try:
        logger_impl.log_ingest_local("INFO", "local item", run_id="run-local")
        logger_impl.log_ingest_online("INFO", "online item", queue="normal")
        logger_impl.log_ingest_audit("INFO", "audit summary", ingest_type="local")

        assert "local item" in streams["local"].getvalue()
        assert "online item" not in streams["local"].getvalue()
        assert "online item" in streams["online"].getvalue()
        assert "local item" not in streams["online"].getvalue()
        assert "audit summary" in streams["audit"].getvalue()
    finally:
        for name, target in targets.items():
            handlers, propagate = old_state[name]
            target.handlers = handlers
            target.propagate = propagate


def test_log_activity_records_ingest_type_and_run_id(monkeypatch, tmp_path):
    logger_impl, = fresh_backend(monkeypatch, tmp_path, "logger.logger")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logger_impl.JSONFormatter())
    old_handlers = logger_impl.activity_logger.handlers[:]
    old_propagate = logger_impl.activity_logger.propagate
    logger_impl.activity_logger.handlers = [handler]
    logger_impl.activity_logger.propagate = False

    try:
        logger_impl.log_activity(
            original_name="source.jpg",
            vault_id="abc",
            platform="Local",
            artist="Tester",
            ingest_type="local",
            run_id="run-1",
        )

        record = json.loads(stream.getvalue())
        assert record["message"] == "Ingestion successful"
        assert record["ingest_type"] == "local"
        assert record["run_id"] == "run-1"
        assert record["status"] == "success"

        stream.seek(0)
        stream.truncate(0)
        logger_impl.log_activity(
            original_name="source.jpg",
            vault_id="abc",
            platform="Local",
            artist="Tester",
            ingest_type="local",
            run_id="run-1",
            status="failed",
        )
        record = json.loads(stream.getvalue())
        assert record["level"] == "ERROR"
        assert record["message"] == "Ingestion failed"
        assert record["status"] == "failed"
    finally:
        logger_impl.activity_logger.handlers = old_handlers
        logger_impl.activity_logger.propagate = old_propagate


def test_fingerprint_fallback_failures_are_logged(monkeypatch, tmp_path):
    fingerprint, = fresh_backend(monkeypatch, tmp_path, "fingerprint")
    logger = importlib.import_module("logger")
    calls = []
    video = tmp_path / "bad.mp4"

    def fake_log_system(level, message, **kwargs):
        calls.append((level, message, kwargs))

    monkeypatch.setattr(logger, "log_system", fake_log_system)

    monkeypatch.setattr(fingerprint.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ffmpeg missing")))
    assert fingerprint.is_silent(video) is True

    monkeypatch.setattr(fingerprint, "is_silent", lambda path: False)
    assert fingerprint.get_audio_fingerprint(video) == b""

    assert fingerprint.get_video_duration(video) == 0.0

    monkeypatch.setattr(fingerprint, "get_model", lambda: (_ for _ in ()).throw(RuntimeError("model load failed")))
    assert fingerprint.get_visual_embedding(video) == b""

    assert [call[1] for call in calls] == [
        "Video silence detection failed",
        "Audio fingerprint failed",
        "Video duration probe failed",
        "Video visual embedding failed",
    ]
    assert all(call[0] == "WARNING" for call in calls)
    assert all(call[2]["file"] == str(video) for call in calls)
    assert all(call[2]["exc_info"] is True for call in calls)


def test_video_frame_extraction_discards_stderr(monkeypatch, tmp_path):
    fingerprint, = fresh_backend(monkeypatch, tmp_path, "fingerprint")
    kwargs_seen = []

    def fake_run(cmd, *args, **kwargs):
        kwargs_seen.append(kwargs)
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
        return types.SimpleNamespace(stdout=buffer.getvalue())

    monkeypatch.setattr(fingerprint.subprocess, "run", fake_run)

    frame = fingerprint.extract_video_frame(tmp_path / "video.mp4", 1.0)

    assert frame.size == (2, 2)
    assert kwargs_seen[0]["stdout"] == fingerprint.subprocess.PIPE
    assert kwargs_seen[0]["stderr"] == fingerprint.subprocess.DEVNULL


def test_search_manager_runtime_logs_route_to_system(monkeypatch, tmp_path):
    search_manager_module, = fresh_backend(monkeypatch, tmp_path, "db.search_manager")
    calls = []

    monkeypatch.setattr(search_manager_module, "log_system", lambda level, message, **kwargs: calls.append((level, message, kwargs)))
    monkeypatch.setattr(search_manager_module, "get_all_urls", lambda conn: [])
    monkeypatch.setattr(search_manager_module, "get_all_phashes", lambda conn: [])
    monkeypatch.setattr(search_manager_module, "get_all_tiles", lambda conn: [])
    monkeypatch.setattr(search_manager_module, "get_all_video_signatures", lambda conn: [])

    manager = search_manager_module.SearchManager()
    manager.is_hydrated = False
    manager.hydrate(object())
    manager.update_indexes_batch([{"file_hash": "hash", "phash": None, "url": "", "tiles": []}])

    messages = [call[1] for call in calls]
    assert "Hydrating RAM indexes from SQLite..." in messages
    assert any(message.startswith("Hydration complete:") for message in messages)
    assert "RAM index batch update queued" in messages


def test_local_ingest_worker_emits_local_and_audit_logs(monkeypatch, tmp_path):
    api, = fresh_backend(monkeypatch, tmp_path, "api")
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")
    local_calls = []
    audit_calls = []

    def fake_process_file(path, config, metadata=None, delete_source=False, skip_similarity=False, **kwargs):
        assert metadata["ingest_type"] == "local"
        assert metadata["run_id"] == "run-local"
        if delete_source:
            path.unlink()
        return True, "Success: source.jpg", {"file_hash": "abc", "phash": None, "url": "", "tiles": []}

    monkeypatch.setattr(api.ingestion, "process_file", fake_process_file)
    monkeypatch.setattr(api.ingestion, "log_ingest_local", lambda level, message, **kwargs: local_calls.append((level, message, kwargs)))
    monkeypatch.setattr(api.ingestion, "log_ingest_audit", lambda level, message, **kwargs: audit_calls.append((level, message, kwargs)))

    api.ingestion._prepare_local_ingest_run("run-local", {"artist": "A", "platform": "Local"}, False, 1)
    api.ingestion._run_local_ingest_worker([str(source)], {"artist": "A", "platform": "Local"}, False, "run-local")

    local_messages = [call[1] for call in local_calls]
    assert "Local ingest run started" in local_messages
    assert "Local ingest item processed" in local_messages
    assert "Local ingest run finished" in local_messages
    assert any(call[2].get("status") == "ingested" for call in local_calls)
    assert audit_calls[-1][1] == "Local ingestion run summary"
    assert audit_calls[-1][2]["ingest_type"] == "local"
    assert audit_calls[-1][2]["summary_ingested"] == 1


def test_multi_vault_shared_workspace_metadata(monkeypatch, tmp_path):
    """Workspace DB aggregates artists/platforms/wd_tags from all vault DBs.

    Creates a second vault alongside the default one, inserts items with
    different artists into each vault's DB, and verifies:
    - Workspace DB sees artists from both vaults after rebuild
    - Active vault counts are scoped to the active vault only
    - Prune removes metadata not referenced by any vault
    - Scope=all facets include entries from all vaults
    """
    vaults, utils, sqlite_operator, workspace_db, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "vaults",
        "utils",
        "db.sqlite_operator",
        "workspace_db",
        "api",
    )

    # ── Create a second vault ──
    vaults.create_vault("Second")
    vault_items = {item["id"]: item for item in vaults.vault_list()}
    assert "second" in vault_items
    second_db_path = Path(vault_items["second"]["db_path"])

    # ── Insert items into default vault (the active one) ──
    default_hash = "d1" * 32
    conn = insert_mock_item(sqlite_operator, default_hash, artist="Default Artist")
    conn.execute("UPDATE items SET platform = ? WHERE hash = ?", ("pixiv", default_hash))
    conn.commit()
    conn.close()

    # ── Insert items into second vault's DB directly ──
    second_conn = sqlite_operator.init_database(second_db_path)
    second_storage_id = sqlite_operator.allocate_storage_id(second_conn)
    second_hash = "s1" * 32
    second_conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:00', '', '', 'instagram', 'Second Vault Artist', '')
        """,
        (second_hash, second_storage_id, f"{second_hash}.jpg"),
    )
    second_conn.commit()
    second_conn.close()

    # ── Rebuild workspace metadata — should pull from both vaults ──
    result = workspace_db.rebuild_workspace_metadata()
    assert result["status"] == "success"

    ws_conn = workspace_db.connect_workspace_database()
    try:
        artist_norms = {row[0] for row in ws_conn.execute("SELECT name_norm FROM artists").fetchall()}
        platform_norms = {row[0] for row in ws_conn.execute("SELECT key_norm FROM platforms").fetchall()}
    finally:
        ws_conn.close()

    # Both vaults' artists should be in workspace DB
    assert "default artist" in artist_norms
    assert "second vault artist" in artist_norms

    # Both vaults' platforms should be in workspace DB
    assert "pixiv" in platform_norms
    assert "instagram" in platform_norms

    # ── Verify counts are scoped to active vault (default) ──
    all_artists = api.library._get_artists_sync("", 50, "all")["items"]
    artist_map = {a["name"]: a for a in all_artists}

    # Default Artist has 1 item in the active vault
    assert artist_map["Default Artist"]["item_count"] == 1
    # Second Vault Artist has 0 items in the active vault (items are in second vault)
    assert artist_map["Second Vault Artist"]["item_count"] == 0

    # ── Verify used_only filters correctly ──
    used_artists = api.library._get_artists_sync("", 50, "used")["items"]
    used_names = {a["name"] for a in used_artists}
    assert "Default Artist" in used_names
    assert "Second Vault Artist" not in used_names

    # ── Verify facets scope=all includes workspace data ──
    all_artist_facets = api.library._get_facets_sync("artist", "", 50, "all")["items"]
    facet_values = {item["value"] for item in all_artist_facets}
    assert "Default Artist" in facet_values
    assert "Second Vault Artist" in facet_values

    all_platform_facets = api.library._get_facets_sync("platform", "", 50, "all")["items"]
    platform_values = {item["value"] for item in all_platform_facets}
    assert "Pixiv" in platform_values
    assert "Instagram" in platform_values

    # ── Prune should NOT remove Second Vault Artist (used in second vault) ──
    prune_result = workspace_db.prune_unused_workspace_metadata()
    assert prune_result["status"] == "success"
    assert prune_result["pruned"]["artists"] == 0

    # ── Insert an orphan artist, then prune should remove it ──
    ws_conn = workspace_db.connect_workspace_database()
    ws_conn.execute(
        "INSERT INTO artists(name, name_norm, kind, notes, created_at, updated_at) VALUES (?, ?, 'artist', '', '2026-01-01', '2026-01-01')",
        ("Orphan Artist", "orphan artist"),
    )
    ws_conn.commit()
    ws_conn.close()

    prune_result = workspace_db.prune_unused_workspace_metadata()
    assert prune_result["pruned"]["artists"] == 1

    ws_conn = workspace_db.connect_workspace_database()
    try:
        assert ws_conn.execute("SELECT 1 FROM artists WHERE name_norm = 'orphan artist'").fetchone() is None
        assert ws_conn.execute("SELECT 1 FROM artists WHERE name_norm = 'default artist'").fetchone() is not None
        assert ws_conn.execute("SELECT 1 FROM artists WHERE name_norm = 'second vault artist'").fetchone() is not None
    finally:
        ws_conn.close()


def test_vault_health_dictionary_drift_uses_all_workspace_vaults(monkeypatch, tmp_path):
    vaults, sqlite_operator, workspace_db = fresh_backend(
        monkeypatch,
        tmp_path,
        "vaults",
        "db.sqlite_operator",
        "workspace_db",
    )
    vaults.create_vault("Second")
    vault_items = {item["id"]: item for item in vaults.vault_list()}
    second_db_path = Path(vault_items["second"]["db_path"])
    workspace_db.connect_workspace_database().close()

    default_hash = "d2" * 32
    default_conn = insert_mock_item(sqlite_operator, default_hash)
    default_conn.execute(
        "INSERT INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Active Missing', 'active missing', 'general')",
        (default_hash,),
    )
    default_conn.commit()
    default_conn.close()

    second_conn = sqlite_operator.init_database(second_db_path)
    second_storage = sqlite_operator.allocate_storage_id(second_conn)
    second_hash = "d3" * 32
    second_conn.execute(
        """
        INSERT INTO items(hash, storage_id, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash)
        VALUES (?, ?, ?, '.jpg', 'image/jpeg', 10, '2026-01-01 00:00:00', '', '', 'local', 'Second', '')
        """,
        (second_hash, second_storage, f"{second_hash}.jpg"),
    )
    second_conn.execute(
        "INSERT INTO item_wd_tags(item_hash, tag, tag_norm, tag_type) VALUES (?, 'Second Only', 'second only', 'general')",
        (second_hash,),
    )
    second_conn.commit()
    second_conn.close()

    ws_conn = workspace_db.connect_workspace_database()
    ws_conn.executemany(
        "INSERT OR IGNORE INTO wd_tag_dictionary(tag, tag_norm, tag_type, first_seen_at, updated_at) VALUES (?, ?, 'general', '2026-01-01', '2026-01-01')",
        [("Second Only", "second only"), ("Unused", "unused")],
    )
    ws_conn.commit()
    ws_conn.close()

    initial = vaults.audit_vault_health("default")
    assert initial["workspace_dictionary_drift"] == {"missing_in_dictionary": 1, "unused_in_vault": 1}

    ws_conn = workspace_db.connect_workspace_database()
    ws_conn.execute("DELETE FROM wd_tag_dictionary WHERE tag_norm = 'unused'")
    ws_conn.commit()
    ws_conn.close()
    without_unused = vaults.audit_vault_health("default")
    assert without_unused["workspace_dictionary_drift"] == {"missing_in_dictionary": 1, "unused_in_vault": 0}
    assert without_unused["issue_count"] == initial["issue_count"]

    ws_conn = workspace_db.connect_workspace_database()
    ws_conn.execute(
        "INSERT INTO wd_tag_dictionary(tag, tag_norm, tag_type, first_seen_at, updated_at) VALUES ('Active Missing', 'active missing', 'general', '2026-01-01', '2026-01-01')"
    )
    ws_conn.commit()
    ws_conn.close()
    complete = vaults.audit_vault_health("default")
    assert complete["workspace_dictionary_drift"] == {"missing_in_dictionary": 0, "unused_in_vault": 0}
    assert complete["issue_count"] == without_unused["issue_count"] - 1


def test_artist_used_scope_filters_before_limit(monkeypatch, tmp_path):
    sqlite_operator, workspace_db, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "db.sqlite_operator",
        "workspace_db",
        "api",
    )

    conn = insert_mock_item(sqlite_operator, "u1" * 32, artist="Zzz Used Artist")
    conn.close()

    ws_conn = workspace_db.connect_workspace_database()
    try:
        for index in range(80):
            ws_conn.execute(
                """
                INSERT OR IGNORE INTO artists(name, name_norm, kind, notes, created_at, updated_at)
                VALUES (?, ?, 'artist', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (f"Aaa Unused {index:03d}", f"aaa unused {index:03d}"),
            )
        ws_conn.commit()
    finally:
        ws_conn.close()

    used_artists = api.library._get_artists_sync("", 20, "used")["items"]
    assert [artist["name"] for artist in used_artists] == ["Zzz Used Artist"]


def test_vault_transition_preflight_rejects_before_mutation(monkeypatch, tmp_path):
    vaults, api = fresh_backend(monkeypatch, tmp_path, "vaults", "api")
    ctx = vaults._ctx()
    before_config = ctx.config_path.read_bytes()
    ingestion = importlib.import_module("api.ingestion")
    monkeypatch.setattr(
        ingestion,
        "runtime_switch_preflight",
        lambda *args, **kwargs: {"allowed": False, "blockers": ["test_transition_blocked"]},
    )

    with pytest.raises(HTTPException) as exc:
        vaults.create_vault("Blocked Vault")

    assert exc.value.status_code == 409
    assert ctx.config_path.read_bytes() == before_config
    assert not (ctx.root / "data" / "vaults" / "blocked-vault").exists()


def test_active_vault_hydration_failure_restores_config_and_runtime(monkeypatch, tmp_path):
    vaults, runtime_context = fresh_backend(monkeypatch, tmp_path, "vaults", "runtime_context")
    vaults.create_vault("Hydration Target")
    ctx = vaults._ctx()
    before_config = ctx.config_path.read_bytes()
    search_manager_module = importlib.import_module("db.search_manager")
    original_hydrate = search_manager_module.search_manager.hydrate

    def fail_target_hydration(conn):
        current = runtime_context.try_get_runtime_context()
        if current is not None and current.active_vault.id == "hydration-target":
            raise RuntimeError("candidate vault hydration failed")
        return original_hydrate(conn)

    monkeypatch.setattr(search_manager_module.search_manager, "hydrate", fail_target_hydration)
    with pytest.raises(RuntimeError, match="candidate vault hydration failed"):
        vaults.set_active_vault("hydration-target")

    assert ctx.config_path.read_bytes() == before_config
    assert runtime_context.get_runtime_context().active_vault.id == "default"
    assert vaults.active_vault_id() == "default"


def test_active_vault_config_failure_restores_exact_config(monkeypatch, tmp_path):
    vaults, runtime_context = fresh_backend(monkeypatch, tmp_path, "vaults", "runtime_context")
    vaults.create_vault("Config Failure Target")
    ctx = vaults._ctx()
    before_config = ctx.config_path.read_bytes()

    def fail_config_write(config, ctx=None):
        raise RuntimeError("active-vault config write failed")

    monkeypatch.setattr(vaults, "_write_config", fail_config_write)
    with pytest.raises(RuntimeError, match="active-vault config write failed"):
        vaults.set_active_vault("config-failure-target")

    assert ctx.config_path.read_bytes() == before_config
    assert runtime_context.get_runtime_context().active_vault.id == "default"
    assert vaults.active_vault_id() == "default"


def test_create_vault_config_failure_removes_staged_tree(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    ctx = vaults._ctx()
    before_config = ctx.config_path.read_bytes()
    target_root = ctx.root / "data" / "vaults" / "create-failure"

    def fail_config_write(config, ctx=None):
        raise RuntimeError("create config failed")

    monkeypatch.setattr(vaults, "_write_config", fail_config_write)
    with pytest.raises(RuntimeError, match="create config failed"):
        vaults.create_vault("Create Failure")

    assert ctx.config_path.read_bytes() == before_config
    assert not target_root.exists()
    assert "create-failure" not in vaults._read_config()["vaults"]


def test_delete_vault_config_failure_restores_tree_and_config(monkeypatch, tmp_path):
    vaults = fresh_backend(monkeypatch, tmp_path, "vaults")[0]
    created = vaults.create_vault("Delete Failure")
    target_root = Path(next(item["root"] for item in created["items"] if item["id"] == "delete-failure"))
    marker = target_root / "vault" / "notes" / "marker.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("must survive", encoding="utf-8")
    ctx = vaults._ctx()
    before_config = ctx.config_path.read_bytes()

    def fail_config_write(config, ctx=None):
        raise RuntimeError("delete config failed")

    monkeypatch.setattr(vaults, "_write_config", fail_config_write)
    with pytest.raises(RuntimeError, match="delete config failed"):
        vaults.delete_vault("delete-failure", confirm=True)

    assert ctx.config_path.read_bytes() == before_config
    assert marker.read_text(encoding="utf-8") == "must survive"
    assert "delete-failure" in vaults._read_config()["vaults"]


def test_relocation_activation_failure_restores_old_path_and_removes_new_files(monkeypatch, tmp_path):
    vaults, api, runtime_context = fresh_backend(monkeypatch, tmp_path, "vaults", "api", "runtime_context")
    ctx = vaults._ctx()
    old_root = ctx.active_vault.root
    target_root = ctx.root / "data" / "vaults" / "relocation-failure"
    target_root.mkdir(parents=True)
    before_config = ctx.config_path.read_bytes()
    search_manager_module = importlib.import_module("db.search_manager")
    original_hydrate = search_manager_module.search_manager.hydrate

    def fail_target_hydration(conn):
        current = runtime_context.try_get_runtime_context()
        if current is not None and current.active_vault.root == target_root:
            raise RuntimeError("relocation hydration failed")
        return original_hydrate(conn)

    monkeypatch.setattr(search_manager_module.search_manager, "hydrate", fail_target_hydration)
    with pytest.raises(HTTPException) as exc:
        api.runtime._relocate_vault_sync("default", str(target_root))

    assert exc.value.status_code == 500
    assert ctx.config_path.read_bytes() == before_config
    assert runtime_context.get_runtime_context().active_vault.root == old_root
    assert not any(target_root.rglob("*"))


def test_relocation_config_failure_restores_old_path_and_config(monkeypatch, tmp_path):
    vaults, api, runtime_context = fresh_backend(monkeypatch, tmp_path, "vaults", "api", "runtime_context")
    ctx = vaults._ctx()
    old_root = ctx.active_vault.root
    target_root = ctx.root / "data" / "vaults" / "relocation-config-failure"
    target_root.mkdir(parents=True)
    before_config = ctx.config_path.read_bytes()

    def fail_config_write(config, ctx=None):
        raise RuntimeError("relocation config failed")

    monkeypatch.setattr(vaults, "_write_config", fail_config_write)
    with pytest.raises(HTTPException) as exc:
        api.runtime._relocate_vault_sync("default", str(target_root))

    assert exc.value.status_code == 500
    assert ctx.config_path.read_bytes() == before_config
    assert runtime_context.get_runtime_context().active_vault.root == old_root
    assert not any(target_root.rglob("*"))


def test_vault_transition_uses_a1_lock(monkeypatch, tmp_path):
    vaults, runtime_api = fresh_backend(monkeypatch, tmp_path, "vaults", "api.runtime")
    result = {}
    started = threading.Event()

    def run_create():
        started.set()
        result["value"] = vaults.create_vault("Serialized Vault")

    with runtime_api.runtime_transition_lock():
        worker = threading.Thread(target=run_create)
        worker.start()
        assert started.wait(2)
        time.sleep(0.1)
        assert "value" not in result
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result["value"]["status"] == "success"
