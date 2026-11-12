from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from .test_mock_vault_isolation import fresh_backend, insert_mock_item


def test_new_ingestor_keeps_global_permits_already_in_use(monkeypatch, tmp_path):
    module, runtime = fresh_backend(
        monkeypatch, tmp_path, "external_ingestion", "runtime_context"
    )
    config = {"ingestion": {"concurrency": {"global_max_workers": 2}}}
    monkeypatch.setattr(module, "get_app_settings", lambda: config)
    ctx = runtime.get_runtime_context()
    first = module.ExternalIngestor(str(tmp_path / "first.txt"), ctx=ctx)
    original_limit = module.GLOBAL_WORKER_LIMIT

    with original_limit:
        second = module.ExternalIngestor(str(tmp_path / "second.txt"), ctx=ctx)
        assert module.GLOBAL_WORKER_LIMIT is original_limit
        assert first.worker_limit is second.worker_limit is original_limit
        assert original_limit.acquire(blocking=False)
        try:
            assert not original_limit.acquire(blocking=False)
        finally:
            original_limit.release()


def test_concurrent_ingestors_share_one_global_limit(monkeypatch, tmp_path):
    module, runtime = fresh_backend(
        monkeypatch, tmp_path, "external_ingestion", "runtime_context"
    )
    config = {"ingestion": {"concurrency": {"global_max_workers": 2}}}
    monkeypatch.setattr(module, "get_app_settings", lambda: config)
    ctx = runtime.get_runtime_context()

    def create(index):
        return module.ExternalIngestor(str(tmp_path / f"{index}.txt"), ctx=ctx)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ingestors = list(pool.map(create, range(24)))

    assert all(ingestor.worker_limit is module.GLOBAL_WORKER_LIMIT for ingestor in ingestors)


def test_configured_limit_change_does_not_rebind_existing_ingestor(monkeypatch, tmp_path):
    module, runtime = fresh_backend(
        monkeypatch, tmp_path, "external_ingestion", "runtime_context"
    )
    config = {"ingestion": {"concurrency": {"global_max_workers": 2}}}
    monkeypatch.setattr(module, "get_app_settings", lambda: config)
    ctx = runtime.get_runtime_context()
    first = module.ExternalIngestor(str(tmp_path / "first.txt"), ctx=ctx)
    original_limit = module.GLOBAL_WORKER_LIMIT
    config["ingestion"]["concurrency"]["global_max_workers"] = 3

    second = module.ExternalIngestor(str(tmp_path / "second.txt"), ctx=ctx)

    assert first.worker_limit is original_limit
    assert second.worker_limit is module.GLOBAL_WORKER_LIMIT
    assert second.worker_limit is not original_limit
    for _ in range(3):
        assert second.worker_limit.acquire(blocking=False)
    try:
        assert not second.worker_limit.acquire(blocking=False)
    finally:
        for _ in range(3):
            second.worker_limit.release()


@pytest.fixture
def rollback_runtime(monkeypatch, tmp_path):
    module, db, utils, runtime = fresh_backend(
        monkeypatch, tmp_path, "external_ingestion", "db.sqlite_operator", "utils", "runtime_context"
    )
    ctx = runtime.get_runtime_context()
    item_hash = "ab" * 32
    conn = insert_mock_item(db, item_hash)
    storage_id = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()[0]
    conn.close()
    paths = [
        utils.storage_asset_path_for(item_hash, storage_id, ".jpg", "image/jpeg", ctx=ctx),
        utils.note_path_for(item_hash, storage_id=storage_id, ctx=ctx),
        utils.wd_tag_cache_path_for(item_hash, storage_id=storage_id, ctx=ctx),
        module.thumbnail_path_for(item_hash, storage_id, ctx=ctx),
        module.video_thumbnail_path_for(item_hash, storage_id, ctx=ctx),
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve until commit")
    module.search_manager.update_indexes(item_hash, "0" * 16, None, ctx=ctx)
    ingestor = module.ExternalIngestor(str(tmp_path / "links.txt"), ctx=ctx)
    return module, db, ctx, ingestor, item_hash, paths


def test_batch_rollback_commit_failure_keeps_database_files_and_index(rollback_runtime, monkeypatch):
    module, db, ctx, ingestor, item_hash, paths = rollback_runtime
    connection = db.connect_database(ctx=ctx)

    class FailedCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            assert all(path.exists() for path in paths)
            raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(module, "connect_database", lambda **kwargs: FailedCommit())

    assert ingestor._rollback_batch([{"file_hash": item_hash}]) is None
    conn = db.connect_database(ctx=ctx)
    assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is not None
    conn.close()
    assert all(path.read_bytes() == b"preserve until commit" for path in paths)
    assert module.search_manager.query_global_only("0" * 16, 0, ctx=ctx) == [(item_hash, 0)]


def test_batch_rollback_removes_committed_rows_files_and_index_once(rollback_runtime):
    module, db, ctx, ingestor, item_hash, paths = rollback_runtime

    assert ingestor._rollback_batch([{"file_hash": item_hash}, {"file_hash": item_hash}, {}]) == 1
    conn = db.connect_database(ctx=ctx)
    assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is None
    conn.close()
    assert not any(path.exists() for path in paths)
    assert module.search_manager.query_global_only("0" * 16, 0, ctx=ctx) == []


def test_batch_rollback_updates_facets_for_deleted_items_only(rollback_runtime):
    _, db, ctx, ingestor, item_hash, _ = rollback_runtime
    from metadata_index import rebuild_metadata_facet_counts, refresh_metadata_index_counters

    conn = insert_mock_item(db, "remaining", artist="DB Artist")
    try:
        conn.execute("INSERT INTO item_topics (item_hash, topic, topic_norm) VALUES (?, 'Unique', 'unique')", (item_hash,))
        rebuild_metadata_facet_counts(conn)
        refresh_metadata_index_counters(conn)
        conn.commit()
        artist_count = "SELECT count FROM metadata_facet_counts WHERE kind='artist' AND value_norm='db artist'"
        topic_count = "SELECT count FROM metadata_facet_counts WHERE kind='topic' AND value_norm='unique'"
        assert conn.execute(artist_count).fetchone() == (2,)
        assert conn.execute(topic_count).fetchone() == (1,)

        assert ingestor._rollback_batch([{"file_hash": item_hash}]) == 1
        assert conn.execute(artist_count).fetchone() == (1,)
        assert conn.execute(topic_count).fetchone() is None
    finally:
        conn.close()


def test_batch_rollback_later_database_failure_restores_earlier_rows(rollback_runtime, monkeypatch):
    module, db, ctx, ingestor, item_hash, paths = rollback_runtime
    second_hash = "cd" * 32
    connection = insert_mock_item(db, second_hash)

    class FailedDelete:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, statement, parameters=()):
            if statement.startswith("DELETE FROM items") and parameters == (second_hash,):
                raise sqlite3.OperationalError("simulated second-row delete failure")
            return connection.execute(statement, parameters)

    monkeypatch.setattr(module, "connect_database", lambda **kwargs: FailedDelete())

    assert ingestor._rollback_batch([
        {"file_hash": item_hash}, {"file_hash": second_hash}
    ]) is None
    conn = db.connect_database(ctx=ctx)
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE hash IN (?, ?)", (item_hash, second_hash)
    ).fetchone()[0] == 2
    conn.close()
    assert all(path.read_bytes() == b"preserve until commit" for path in paths)
    assert module.search_manager.query_global_only("0" * 16, 0, ctx=ctx) == [(item_hash, 0)]


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_online_worker_returns_index_data_for_items_still_in_sqlite(
    rollback_runtime, monkeypatch, rollback_fails
):
    module, db, ctx, ingestor, item_hash, paths = rollback_runtime
    phash = "0" * 16
    conn = db.connect_database(ctx=ctx)
    conn.execute("UPDATE items SET phash = ? WHERE hash = ?", (phash, item_hash))
    conn.commit()
    module.search_manager.reset(ctx)  # Online batch publishes indexes after the worker.
    url = "https://x.com/example/status/123"
    entry = module.QueueEntry(url)
    payload = {"file_hash": item_hash, "phash": phash}
    monkeypatch.setattr(ingestor, "_get_downloader_type", lambda _url: "gallery-dl")
    monkeypatch.setattr(ingestor, "_url_complete", lambda _url: False)
    monkeypatch.setattr(ingestor, "_log_failure", lambda *_args: None)
    monkeypatch.setattr(module, "download_gallery", lambda *_args, **_kwargs: (
        True, {"file_paths": [str(paths[0]), "not-ingested.jpg"]},
    ))
    outcomes = iter([(True, "committed", payload), (False, "rejected", None)])
    monkeypatch.setattr(module, "process_file", lambda *_args, **_kwargs: next(outcomes))
    if rollback_fails:
        def fail_to_open(**_kwargs):
            raise sqlite3.OperationalError("simulated connection failure")
        monkeypatch.setattr(module, "connect_database", fail_to_open)

    try:
        ok, _, stats, index_data = ingestor._worker_item("x", entry, [0, 0])
        assert not ok
        assert stats["processed"] == int(rollback_fails)
        assert index_data == ([payload] if rollback_fails else [])
        row = conn.execute("SELECT phash FROM items WHERE hash = ?", (item_hash,)).fetchone()
        assert row == ((phash,) if rollback_fails else None)
        module.search_manager.update_indexes_batch(index_data, ctx=ctx)
        assert module.search_manager.query_global_only(phash, 0, ctx=ctx) == (
            [(item_hash, 0)] if rollback_fails else []
        )
    finally:
        conn.close()


@pytest.mark.parametrize("failure_phase", ["index", "unlink"])
def test_batch_rollback_followup_failure_does_not_undo_committed_delete(
    rollback_runtime, monkeypatch, failure_phase
):
    module, db, ctx, ingestor, item_hash, paths = rollback_runtime
    if failure_phase == "index":
        def fail_index(*args, **kwargs):
            raise RuntimeError("simulated index failure")
        monkeypatch.setattr(module.search_manager, "remove_indexes_batch", fail_index)
    else:
        original_unlink = type(paths[0]).unlink

        def fail_asset(path, *args, **kwargs):
            if path == paths[0]:
                raise PermissionError("simulated file lock")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(type(paths[0]), "unlink", fail_asset)

    assert ingestor._rollback_batch([{"file_hash": item_hash}]) == 1
    conn = db.connect_database(ctx=ctx)
    assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is None
    conn.close()
    assert not any(path.exists() for path in paths[1:])
    assert paths[0].exists() is (failure_phase == "unlink")
