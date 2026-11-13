import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .test_mock_vault_isolation import app_settings_config, fresh_backend


@pytest.fixture
def ingest_runtime(monkeypatch, tmp_path):
    utils, db, processor, runtime = fresh_backend(
        monkeypatch, tmp_path, "utils", "db.sqlite_operator", "processor", "runtime_context"
    )
    ctx = runtime.get_runtime_context()
    source = tmp_path / "source.txt"
    source.write_text("durable ingest data", encoding="utf-8")
    item_hash = utils.calculate_file_hash(source)
    # Exercise the real database/filesystem transaction without loading a tagger.
    monkeypatch.setattr(processor, "get_mime_type", lambda path: "text/plain")
    monkeypatch.setattr(
        processor, "tag_media",
        lambda *args, **kwargs: SimpleNamespace(status="skipped", error="", tags=[]),
    )
    monkeypatch.setattr(processor, "generate_markdown", lambda *args, **kwargs: "---\ntopics: []\n---\n")
    monkeypatch.setattr(processor, "safe_reindex_item_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "log_activity", lambda **kwargs: None)
    events = []
    monkeypatch.setattr(processor, "log_system", lambda level, message, **kwargs: events.append(message))
    return utils, db, processor, ctx, source, item_hash, events


@pytest.mark.parametrize("failure_phase", ["index", "activity_log"])
def test_failure_after_commit_preserves_item_and_reports_success(ingest_runtime, monkeypatch, failure_phase):
    utils, db, processor, ctx, source, item_hash, events = ingest_runtime

    def fail(*args, **kwargs):
        raise RuntimeError(f"simulated {failure_phase} failure")

    if failure_phase == "index":
        monkeypatch.setattr(processor.search_manager, "update_indexes", fail)
    else:
        monkeypatch.setattr(processor, "log_activity", fail)

    ok, message, result = processor.process_file(
        source, app_settings_config(["text/plain"], ["txt"]), delete_source=True, ctx=ctx
    )
    conn = db.connect_database(ctx=ctx)
    row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()

    assert ok, message
    assert result["file_hash"] == item_hash
    assert row is not None
    assert message == f"Success: source.txt -> {row[0]}.txt"
    assert utils.storage_asset_path_for(item_hash, row[0], ".txt", "text/plain", ctx=ctx).read_text() == "durable ingest data"
    assert utils.note_path_for(item_hash, row[0], ctx=ctx).exists()
    assert source.exists() is (failure_phase == "index")
    assert "Ingest committed but follow-up failed" in events


@pytest.mark.parametrize("failure_phase", ["markdown", "commit"])
def test_failure_before_commit_rolls_back_database_and_copied_files(ingest_runtime, monkeypatch, failure_phase):
    _, db, processor, ctx, source, item_hash, events = ingest_runtime
    original_assets = set(ctx.active_vault.assets_dir.rglob("*"))
    original_notes = set(ctx.active_vault.notes_dir.rglob("*.md"))
    if failure_phase == "markdown":
        def fail_write(*args, **kwargs):
            raise OSError("simulated markdown write failure")
        monkeypatch.setattr(processor, "atomic_write_text", fail_write)
    else:
        connection = db.connect_database(ctx=ctx)

        class FailedCommit:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                raise sqlite3.OperationalError("simulated commit failure")

        monkeypatch.setattr(processor, "connect_database", lambda **kwargs: FailedCommit())

    ok, message, result = processor.process_file(
        source, app_settings_config(["text/plain"], ["txt"]), delete_source=True, ctx=ctx
    )
    conn = db.connect_database(ctx=ctx)
    row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone()
    conn.close()

    assert not ok
    assert "Rolled back" in message
    assert result is None
    assert row is None
    assert source.read_text() == "durable ingest data"
    assert {path for path in ctx.active_vault.assets_dir.rglob("*") if path.is_file()} == {
        path for path in original_assets if path.is_file()
    }
    assert set(ctx.active_vault.notes_dir.rglob("*.md")) == original_notes
    assert "Ingest committed but follow-up failed" not in events


@pytest.mark.parametrize("recovery_available", [True, False])
def test_ingest_uses_recovered_similarity_index(ingest_runtime, monkeypatch, recovery_available):
    _, db, processor, ctx, source, item_hash, _ = ingest_runtime
    from db import search_manager as indexes

    manager = processor.search_manager
    source = source.rename(source.with_suffix(".jpg"))
    phash = "0" * 16
    cfg = app_settings_config(["image/jpeg"], ["jpg"])
    monkeypatch.setattr(processor, "get_mime_type", lambda _path: "image/jpeg")
    monkeypatch.setattr(processor, "calculate_phash", lambda _path: phash)
    monkeypatch.setattr(processor, "calculate_tiles", lambda _path: [])
    monkeypatch.setattr(processor, "ensure_thumbnail", lambda *args, **kwargs: None)

    def fail_index(*_args):
        raise RuntimeError("simulated image index failure")

    def fail_read(*_args):
        raise sqlite3.OperationalError("simulated SQLite read failure")

    conn = db.connect_database(ctx=ctx)
    try:
        manager.hydrate(conn, ctx=ctx)
        monkeypatch.setattr(manager._state_for(ctx).global_tree, "add", fail_index)
        if not recovery_available:
            monkeypatch.setattr(indexes, "get_all_phashes", fail_read)

        ok, message, _ = processor.process_file(source, cfg, delete_source=True, ctx=ctx)
        assert ok, message
        assert not source.exists()
        assert conn.execute("SELECT phash FROM items WHERE hash = ?", (item_hash,)).fetchone() == (phash,)
        if recovery_available:
            assert manager.query_image(phash, 0, ctx=ctx) == [(item_hash, 0, "Global")]
        else:
            source.write_bytes(b"second image, different content")
            ok, message, result = processor.process_file(source, cfg, delete_source=True, ctx=ctx)
            assert not ok and result is None
            assert message == "Similarity index could not be restored from SQLite"
            assert source.exists()
            assert conn.execute("SELECT COUNT(*) FROM items WHERE phash = ?", (phash,)).fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["image", "tile", "video", "pending_review", "update"])
def test_internal_type_error_does_not_retry_in_the_active_vault(ingest_runtime, monkeypatch, operation):
    _, _, processor, active, source, _, _ = ingest_runtime
    # Keep the same test SQLite/filesystem paths, but use a distinct ctx object
    # so a context-free retry is observable even with no second vault on disk.
    explicit = replace(active, active_vault=replace(active.active_vault, id="explicit-context"))
    calls = []

    def fail(*_args, ctx=None, **_kwargs):
        calls.append(ctx)
        raise TypeError("simulated internal TypeError")

    if operation == "image":
        monkeypatch.setattr(processor.search_manager, "query_image", fail)
        processor.find_visual_duplicate("0" * 16, ctx=explicit)
    elif operation == "tile":
        monkeypatch.setattr(processor.search_manager, "query_image", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(processor.search_manager, "query_global_only", fail)
        processor.find_visual_duplicate("0" * 16, new_tiles=[(0, "0" * 16)], ctx=explicit)
    elif operation == "video":
        monkeypatch.setattr(processor.search_manager, "query_video", fail)
        processor.find_video_duplicate(b"audio", b"", ctx=explicit)
    else:
        cfg = app_settings_config(["text/plain"], ["txt"])
        if operation == "pending_review":
            monkeypatch.setattr(processor, "_pending_review_match", fail)
            with pytest.raises(TypeError, match="simulated internal"):
                processor.process_file(source, cfg, ctx=explicit)
        else:
            monkeypatch.setattr(processor.search_manager, "update_indexes", fail)
            ok, message, _ = processor.process_file(source, cfg, ctx=explicit)
            assert ok, message
    assert len(calls) == 1 and calls[0] is explicit


def test_ingest_search_delete_and_vault_switch_over_api(ingest_runtime, monkeypatch):
    _, db, processor, ctx, source, item_hash, _ = ingest_runtime
    from api.app import app
    from api.common import _api_key
    from metadata_index import stop_metadata_watchdog
    from runtime_activation import activate_runtime_context
    from runtime_context import build_runtime_context, clear_runtime_context
    from vaults import create_vault

    source = source.rename(source.with_suffix(".jpg"))
    phash = "0" * 16
    cfg = app_settings_config(["image/jpeg"], ["jpg"])
    monkeypatch.setattr(processor, "get_mime_type", lambda _path: "image/jpeg")
    monkeypatch.setattr(processor, "calculate_phash", lambda _path: phash)
    monkeypatch.setattr(processor, "calculate_tiles", lambda _path: [])
    monkeypatch.setattr(processor, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr("metadata_index.start_metadata_repair_worker", lambda **_kwargs: None)
    create_vault("Second")
    activate_runtime_context(ctx)

    try:
        ok, message, _ = processor.process_file(source, cfg, delete_source=True, ctx=ctx)
        assert ok, message
        assert processor.search_manager.query_global_only(phash, 0) == [(item_hash, 0)]
        headers = {"X-LMZ-API-KEY": _api_key()}
        with TestClient(app) as client:
            assert client.get(f"/api/items/{item_hash}").status_code == 200
            assert client.delete(f"/api/items/{item_hash}").status_code in (401, 403)
            deleted = client.delete(f"/api/items/{item_hash}", headers=headers)
            assert deleted.status_code == 200
            assert deleted.json() == {"status": "success", "deleted": True, "cleanup_errors": []}
            assert client.get(f"/api/items/{item_hash}").status_code == 404
            assert processor.search_manager.query_global_only(phash, 0) == []

            switched = client.post("/api/vaults/active", headers=headers, json={"id": "second"})
            assert switched.status_code == 200
            assert switched.json()["active"] == "second"
            assert client.get(f"/api/items/{item_hash}").status_code == 404

        # Clear process-local state and let the startup lifecycle reload the
        # persisted workspace and build a fresh index, as on backend restart.
        clear_runtime_context()
        processor.search_manager.reset_all()
        with TestClient(app) as client:
            assert build_runtime_context().active_vault.id == "second"
            assert client.get(f"/api/items/{item_hash}").status_code == 404
            assert processor.search_manager.query_global_only(phash, 0) == []
        conn = db.connect_database(ctx=ctx)
        try:
            assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is None
        finally:
            conn.close()
    finally:
        stop_metadata_watchdog()


def test_jxl_ingest_keeps_original_and_serves_browser_preview(ingest_runtime, monkeypatch):
    from PIL import Image
    from api.app import app
    from jxl_preview import register_jxl_decoder
    from metadata_index import stop_metadata_watchdog
    from validators import get_mime_type

    utils, _, processor, ctx, source, _, _ = ingest_runtime
    source = source.with_suffix(".jxl")
    register_jxl_decoder()
    Image.new("RGBA", (16, 10), (10, 90, 230, 70)).save(source, format="JXL", lossless=True)
    original = source.read_bytes()
    monkeypatch.setattr(processor, "get_mime_type", get_mime_type)
    monkeypatch.setattr("metadata_index.start_metadata_repair_worker", lambda **_kwargs: None)

    try:
        with TestClient(app) as client:
            ok, message, result = processor.process_file(
                source, app_settings_config(["image/jxl"], [".jxl"]), skip_similarity=True, ctx=ctx
            )
            assert ok, message
            item_hash = result["file_hash"]
            item_response = client.get(f"/api/items/{item_hash}")
            assert item_response.status_code == 200
            item = item_response.json()
            assert item["mime_type"] == "image/jxl"
            assert item["url"].endswith(".jxl")
            stored = ctx.active_vault.assets_dir / item["url"].removeprefix("/vault/")
            assert stored.read_bytes() == original

            preview = client.get(item["url"])
            assert preview.status_code == 200
            assert preview.headers["content-type"] == "image/png"
            assert preview.content.startswith(b"\x89PNG")
            thumb = client.get(item["thumbnail_url"])
            assert thumb.status_code == 200
            assert thumb.headers["content-type"] == "image/jpeg"
    finally:
        stop_metadata_watchdog()
