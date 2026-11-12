import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from .test_mock_vault_isolation import fresh_backend, insert_mock_item


@pytest.fixture
def search_runtime(monkeypatch, tmp_path):
    module, runtime = fresh_backend(
        monkeypatch, tmp_path, "db.search_manager", "runtime_context"
    )
    return module, runtime.get_runtime_context()


def previous_cosine_distance(first: bytes, second: bytes) -> float:
    a, b = np.frombuffer(first, dtype=np.float32), np.frombuffer(second, dtype=np.float32)
    if len(a) != len(b) or not len(a):
        return 1.0
    a_norm, b_norm = np.linalg.norm(a), np.linalg.norm(b)
    if a_norm == 0 or b_norm == 0:
        return 1.0
    return 1.0 - float(np.dot(a, b) / (a_norm * b_norm))


def test_failed_hydration_does_not_publish_partial_indexes(monkeypatch, tmp_path):
    module, db = fresh_backend(monkeypatch, tmp_path, "db.search_manager", "db.sqlite_operator")
    manager = module.search_manager
    phash = "0" * 16
    conn = insert_mock_item(db, "removed-before-retry")
    try:
        conn.execute("UPDATE items SET phash = ? WHERE hash = ?", (phash, "removed-before-retry"))
        conn.commit()

        def fail_tiles(_conn):
            raise RuntimeError("simulated tile load failure")

        with monkeypatch.context() as failure:
            failure.setattr(module, "get_all_tiles", fail_tiles)
            with pytest.raises(RuntimeError, match="tile load failure"):
                manager.hydrate(conn)

        assert not manager.is_hydrated
        assert manager.query_image(phash, 0) == []
        conn.execute("DELETE FROM items WHERE hash = ?", ("removed-before-retry",))
        conn.commit()
        manager.hydrate(conn)
        assert manager.is_hydrated
        assert manager.query_image(phash, 0) == []
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["insert", "batch_insert", "delete"])
def test_committed_mutation_repairs_index_before_return(monkeypatch, tmp_path, operation):
    module, db, runtime = fresh_backend(
        monkeypatch, tmp_path, "db.search_manager", "db.sqlite_operator", "runtime_context"
    )
    manager = module.search_manager
    ctx = runtime.get_runtime_context()
    other = replace(ctx, active_vault=replace(ctx.active_vault, db_path=tmp_path / "other.db"))
    phash, url = "0" * 16, "https://example.test/item"
    manager.update_indexes("unrelated", phash, None, ctx=other)
    conn = insert_mock_item(db, "item")
    try:
        if operation != "delete":
            manager.hydrate(conn)
        conn.execute("UPDATE items SET phash = ?, source_url = ? WHERE hash = 'item'", (phash, url))
        db.insert_tiles(conn, "item", [(0, phash)])
        conn.commit()

        def fail(*_args):
            raise RuntimeError("simulated partial RAM mutation")

        state = manager._state_for(ctx)
        if operation == "delete":
            manager.hydrate(conn)
            conn.execute("DELETE FROM items WHERE hash = 'item'")
            conn.commit()
            monkeypatch.setattr(state.tile_tree, "remove_hashes", fail)
            manager.remove_indexes_batch([{"hash": "item", "source_url": url}], ctx=ctx)
        else:
            monkeypatch.setattr(state.tile_tree, "add", fail)
            payload = {"file_hash": "item", "phash": phash, "url": url, "tiles": [(0, phash)]}
            if operation == "batch_insert":
                manager.update_indexes_batch([{"file_hash": "not-in-db", "phash": phash}, payload], ctx=ctx)
            else:
                manager.update_indexes(**payload, ctx=ctx)

        assert not state.needs_recovery  # Repaired before a query could retry it.
        expected = [] if operation == "delete" else [
            ("item", 0, "Global"), ("item", 0, "Fragment-to-Whole")
        ]
        assert manager.query_image(phash, 0, ctx=ctx) == expected
        assert manager.url_exists(url, ctx=ctx) is (operation != "delete")
        assert manager.query_image(phash, 0, ctx=other) == [("unrelated", 0, "Global")]
        assert not other.active_vault.db_path.exists()
    finally:
        conn.close()


def test_failed_recovery_rejects_queries_until_sqlite_is_readable(monkeypatch, tmp_path):
    module, db = fresh_backend(monkeypatch, tmp_path, "db.search_manager", "db.sqlite_operator")
    manager = module.search_manager
    phash = "0" * 16
    conn = insert_mock_item(db, "item")
    try:
        conn.execute("UPDATE items SET phash = ? WHERE hash = 'item'", (phash,))
        conn.commit()
        manager.hydrate(conn)
        conn.execute("DELETE FROM items WHERE hash = 'item'")
        conn.commit()

        def fail(*_args):
            raise sqlite3.OperationalError("simulated failure")

        monkeypatch.setattr(manager.global_tree, "remove_hashes", fail)
        with monkeypatch.context() as failure:
            failure.setattr(module, "get_all_phashes", fail)
            manager.remove_indexes_batch([{"hash": "item"}])
            with pytest.raises(module.SearchIndexUnavailableError):
                manager.query_image(phash, 0)
        assert manager.query_image(phash, 0) == []
    finally:
        conn.close()


def test_recovery_and_delayed_index_update_do_not_duplicate_a_committed_signature(monkeypatch, tmp_path):
    module, db, runtime = fresh_backend(
        monkeypatch, tmp_path, "db.search_manager", "db.sqlite_operator", "runtime_context"
    )
    manager = module.search_manager
    ctx = runtime.get_runtime_context()
    audio = np.array([1, 2], dtype=np.int32).tobytes()
    conn = insert_mock_item(db, "first")
    try:
        conn.execute("UPDATE items SET phash = ? WHERE hash = 'first'", ("0" * 16,))
        conn.commit()
        manager.hydrate(conn)

        def fail(*_args):
            raise RuntimeError("simulated RAM failure")

        load_started = Event()
        next_item_committed = Event()
        original_get_urls = module.get_all_urls

        def pause_before_snapshot(conn):
            load_started.set()
            assert next_item_committed.wait(5)
            return original_get_urls(conn)

        monkeypatch.setattr(manager.global_tree, "add", fail)
        monkeypatch.setattr(module, "get_all_urls", pause_before_snapshot)

        def commit_and_update():
            assert load_started.wait(5)
            later = insert_mock_item(db, "later")
            try:
                later.execute("UPDATE items SET audio_hash = ? WHERE hash = 'later'", (audio,))
                later.commit()
                next_item_committed.set()
                manager.update_indexes("later", None, None, audio_hash=audio, ctx=ctx)
            finally:
                later.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(commit_and_update)
            manager.update_indexes("first", "0" * 16, None, ctx=ctx)
            future.result(timeout=5)

        assert not manager._state_for(ctx).needs_recovery
        assert manager.query_video(audio, None, ctx=ctx) == [("later", 1.0, "Sonic")]
    finally:
        conn.close()


@pytest.mark.parametrize("bulk", [False, True])
def test_delete_keeps_its_vault_if_active_context_switches(monkeypatch, tmp_path, bulk):
    library, db, runtime = fresh_backend(
        monkeypatch, tmp_path, "api.library", "db.sqlite_operator", "runtime_context"
    )
    manager = library.search_manager
    first = runtime.get_runtime_context()
    other = replace(first, active_vault=replace(first.active_vault, id="other", db_path=tmp_path / "other.db"))
    phash = "0" * 16
    item_hash = "aa" * 32
    conn = insert_mock_item(db, item_hash)
    try:
        conn.execute("UPDATE items SET phash = ? WHERE hash = ?", (phash, item_hash))
        conn.commit()
        manager.hydrate(conn, ctx=first)
        manager.update_indexes(item_hash, phash, None, ctx=other)
        original_refresh = library.refresh_metadata_index_counters

        def switch_context(conn):
            original_refresh(conn)
            runtime.set_runtime_context(other)

        monkeypatch.setattr(library, "refresh_metadata_index_counters", switch_context)
        if bulk:
            assert library._bulk_delete_items_sync([item_hash])["deleted"] == [item_hash]
        else:
            assert library._delete_item_sync(item_hash)["deleted"]
        assert runtime.get_runtime_context() is other
        assert conn.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,)).fetchone() is None
        assert manager.query_global_only(phash, 0, ctx=first) == []
        assert manager.query_global_only(phash, 0, ctx=other) == [(item_hash, 0)]
    finally:
        conn.close()


@pytest.mark.parametrize("batch", [False, True])
def test_delayed_index_update_does_not_restore_a_deleted_item(monkeypatch, tmp_path, batch):
    library, db = fresh_backend(monkeypatch, tmp_path, "api.library", "db.sqlite_operator")
    manager = library.search_manager
    item_hash, phash = "ab" * 32, "0" * 16
    conn = insert_mock_item(db, item_hash)
    try:
        conn.execute("UPDATE items SET phash = ? WHERE hash = ?", (phash, item_hash))
        conn.commit()
        manager.hydrate(conn)
        assert library._delete_item_sync(item_hash)["deleted"]
        if batch:
            manager.update_indexes_batch([{"file_hash": item_hash, "phash": phash}])
        else:
            manager.update_indexes(item_hash, phash, None, conn=conn)
        assert manager.query_global_only(phash, 0) == []
    finally:
        conn.close()


def test_cosine_search_keeps_matches_that_vp_pruning_would_skip(search_runtime):
    module, ctx = search_runtime
    manager = module.search_manager
    signatures = {}
    for angle in (0, 20, 40):
        radians = math.radians(angle)
        signature = np.array([math.cos(radians), math.sin(radians)], dtype=np.float32).tobytes()
        signatures[str(angle)] = signature
        manager.update_indexes(str(angle), None, None, visual_embedding=signature, ctx=ctx)

    results = manager.query_video(None, signatures["20"], ai_threshold=0.08, ctx=ctx)

    assert {item_hash for item_hash, _, _ in results} == {"0", "20", "40"}
    for item_hash, score, kind in results:
        assert kind == "Semantic"
        assert score == 1.0 - previous_cosine_distance(signatures["20"], signatures[item_hash])


def test_audio_prefix_matches_are_not_pruned(search_runtime):
    module, ctx = search_runtime
    manager = module.search_manager
    for item_hash, values in (("a", [0, 0]), ("b", [0, -1])):
        signature = np.array(values, dtype=np.int32).tobytes()
        manager.update_indexes(item_hash, None, None, audio_hash=signature, ctx=ctx)

    query = np.array([0], dtype=np.int32).tobytes()
    assert manager.query_video(query, None, ctx=ctx) == [
        ("a", 1.0, "Sonic"), ("b", 1.0, "Sonic")
    ]


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_similarity_queries_match_exact_scan_across_index_updates(search_runtime, kind):
    module, ctx = search_runtime
    manager = module.search_manager
    rng = np.random.default_rng(47)
    if kind == "video":
        signatures = [rng.normal(size=8).astype(np.float32).tobytes() for _ in range(24)]
        signatures.extend([np.zeros(8, dtype=np.float32).tobytes(), np.ones(7, dtype=np.float32).tobytes()])
        distance = previous_cosine_distance
        label, payload_key = "Semantic", "visual_embedding"
    else:
        from fingerprint import compare_audio_fingerprints
        signatures = [
            rng.integers(-2**31, 2**31 - 1, size=1 + i % 5, dtype=np.int32).tobytes()
            for i in range(24)
        ]
        def distance(a, b):
            return 1.0 - compare_audio_fingerprints(a, b)
        label, payload_key = "Sonic", "audio_hash"

    items = [(str(i), signature) for i, signature in enumerate(signatures)]
    items.append(("same-score", signatures[0]))
    for item_hash, signature in items:
        manager.update_indexes(item_hash, None, None, ctx=ctx, **{payload_key: signature})

    for stage in ("initial", "updated", "deleted"):
        if stage == "updated":
            manager.update_indexes("0", None, None, ctx=ctx, **{payload_key: signatures[1]})
            items[0] = ("0", signatures[1])
        if stage == "deleted":
            manager.remove_indexes_batch([{"hash": "0"}, {"hash": "3"}], ctx=ctx)
            items = [item for item in items if item[0] not in {"0", "3"}]
        for query in signatures[::5]:
            for threshold in (0.0, 0.15, 0.9, 1.0, distance(query, signatures[3])):
                expected = sorted(
                    ((item_hash, distance(query, signature)) for item_hash, signature in items),
                    key=lambda item: item[1],
                )
                if kind == "video":
                    actual = manager.query_video(None, query, ai_threshold=threshold, ctx=ctx)
                else:
                    audio_threshold = 1.0 - threshold
                    actual = manager.query_video(query, None, audio_threshold=audio_threshold, ctx=ctx)
                    threshold = 1.0 - audio_threshold
                assert actual == [(item_hash, 1.0 - dist, label) for item_hash, dist in expected if dist <= threshold]


def test_video_query_keeps_a_snapshot_without_blocking_mutations(search_runtime, monkeypatch):
    module, ctx = search_runtime
    manager = module.search_manager
    sig = np.array([1, 0], dtype=np.float32).tobytes()
    manager.update_indexes("deleted", None, None, visual_embedding=sig, ctx=ctx)
    manager.update_indexes("survivor", None, None, visual_embedding=sig, ctx=ctx)
    started = Event()
    resume = Event()
    original_distances = module._visual_distances

    def pause_query(*args):
        assert not manager._state_for(ctx).sync_lock.locked()
        started.set()
        assert resume.wait(5)
        yield from original_distances(*args)

    with monkeypatch.context() as pause, ThreadPoolExecutor(max_workers=1) as executor:
        pause.setattr(module, "_visual_distances", pause_query)
        pending = executor.submit(manager.query_video, None, sig, ctx=ctx)
        assert started.wait(5)
        manager.remove_indexes_batch([{"hash": "deleted"}], ctx=ctx)
        manager.update_indexes("new", None, None, visual_embedding=sig, ctx=ctx)
        resume.set()
        assert pending.result(timeout=5) == [
            ("deleted", 1.0, "Semantic"), ("survivor", 1.0, "Semantic")
        ]

    assert manager.query_video(None, sig, ctx=ctx) == [
        ("survivor", 1.0, "Semantic"), ("new", 1.0, "Semantic")
    ]


def test_image_queries_search_under_lock_without_copying_the_index(search_runtime, monkeypatch):
    module, ctx = search_runtime
    manager = module.search_manager
    manager.update_indexes("image", "0" * 16, None, [(0, "0" * 16)], ctx=ctx)
    state = manager._state_for(ctx)
    original_query = module.BKTreeSearcher.query

    def locked_query(tree, signature, threshold):
        assert state.sync_lock.locked()
        return original_query(tree, signature, threshold)

    def no_snapshot():
        pytest.fail("An image lookup must not copy the complete index")

    monkeypatch.setattr(module.BKTreeSearcher, "query", locked_query)
    monkeypatch.setattr(state.global_tree, "snapshot", no_snapshot)
    monkeypatch.setattr(state.tile_tree, "snapshot", no_snapshot)

    assert manager.query_image("0" * 16, 0, ctx=ctx) == [
        ("image", 0, "Global"), ("image", 0, "Fragment-to-Whole")
    ]
    assert manager.query_global_only("0" * 16, 0, ctx=ctx) == [("image", 0)]


def test_bk_index_deduplicates_pairs_and_allows_readd_after_delete(search_runtime):
    module, _ = search_runtime
    tree = module.BKTreeSearcher()
    tree.add("item", "0" * 16)
    tree.add("item", "0" * 16)
    tree.add("item", "f" * 16)
    before_delete = tree.snapshot()
    assert len(tree.items) == 2
    assert tree.remove_hashes({"item"})["removed"] == 2
    tree.add("item", "0" * 16)
    assert tree.query("0" * 16, 0) == [("item", 0)]
    assert module.BKTreeSearcher.query_snapshot(before_delete, "f" * 16, 0) == [("item", 0)]


def test_image_search_matches_exact_hamming_scan_before_and_after_delete(search_runtime):
    module, _ = search_runtime
    tree = module.BKTreeSearcher()
    rng = np.random.default_rng(73)
    signatures = [rng.bytes(8).hex() for _ in range(40)]
    items = [(str(i), signature) for i, signature in enumerate(signatures)]
    items.extend((f"tile-{i}", signature) for i, signature in enumerate(signatures[:5]))
    for item in items:
        tree.add(*item)

    for deleted in (set(), {"0", "1", "tile-0"}):
        tree.remove_hashes(deleted)
        items = [item for item in items if item[0] not in deleted]
        for query in signatures[::7]:
            for threshold in (0, 5, 32, 64):
                expected = [
                    (item_hash, (int(query, 16) ^ int(signature, 16)).bit_count())
                    for item_hash, signature in items
                ]
                expected = sorted(item for item in expected if item[1] <= threshold)
                assert sorted(tree.query(query, threshold)) == expected
