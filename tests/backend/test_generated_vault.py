import importlib
import importlib.util
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml



ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
GENERATOR_PATH = ROOT / "tests" / "generators" / "generate_test_vault.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_test_vault_under_test", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reset_backend_modules():
    for name in list(sys.modules):
        if name in {"api", "utils", "runtime_context", "metadata_index", "md_generator", "thumbnails", "topics", "workspace_db", "vaults", "workspaces", "artists", "platforms"} or name.startswith(("api.", "db.", "logger", "tagging")):
            del sys.modules[name]


def assert_inside(path: Path, root: Path):
    assert path.resolve().is_relative_to(root.resolve())


def test_generated_vault_smoke_and_isolation(tmp_path, monkeypatch):
    generator = load_generator()
    generated_root = tmp_path / "generated"
    output = generated_root / "001-smoke"
    generator.main([
        "--output", str(output),
        "--generated-root", str(generated_root),
        "--items", "100",
        "--groups", "12",
        "--review", "4",
        "--video-ratio", "0.2",
        "--artists", "8",
        "--topics", "9",
        "--wd-tags", "30",
        "--wd-character-tags", "6",
        "--seed", "7",
    ])

    config_path = output / "config.yaml"
    manifest_path = output / "manifest.json"
    vault_root = output / "data" / "vaults" / "default"
    db_path = vault_root / "db" / "lmz_main.db"
    assert config_path.exists()
    assert manifest_path.exists()
    assert db_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["counts"]["items"] == 100
    assert manifest["counts"]["review"] == 4
    assert manifest["counts"]["videos"] > 0
    assert manifest["counts"]["wd_tag_pool"] == 30
    assert manifest["counts"]["wd_character_tag_pool"] == 6
    assert manifest["counts"]["wd_rows_estimated"] == 2200
    assert manifest["counts"]["topic_files"] == 9
    assert (output / "data" / "topics").exists()

    for relative in [
        "vault/assets",
        "vault/notes",
        "db",
        "review",
        "wd-tags",
        "ui_cache/thumbnails",
        "logs",
        "queues",
        "batches",
        "input",
        "local_ingest",
        "online_ingest",
    ]:
        assert (vault_root / relative).exists()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 100
        assert conn.execute("SELECT COUNT(*) FROM item_wd_tags").fetchone()[0] == manifest["counts"]["wd_rows_estimated"]
        assert conn.execute("SELECT COUNT(*) FROM metadata_facet_counts").fetchone()[0] == manifest["counts"]["metadata_index_facet_counts"]
    finally:
        conn.close()

    for relative in [
        "data/vaults/default/logs/structured/system.jsonl",
        "data/vaults/default/logs/structured/auth.jsonl",
        "data/vaults/default/logs/structured/review.jsonl",
        "data/vaults/default/logs/structured/ingest_local.jsonl",
        "data/vaults/default/logs/structured/ingest_online.jsonl",
        "data/vaults/default/logs/structured/ingestion_audit.jsonl",
        "data/vaults/default/logs/raw/console.log",
    ]:
        assert (output / relative).exists()
    assert not (output / "data" / "secrets" / "auth").exists()

    monkeypatch.setenv("LMZ_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LMZ_AUTH_ROOT", str(tmp_path / "app-auth"))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    reset_backend_modules()
    utils = importlib.import_module("utils")
    sqlite_operator = importlib.import_module("db.sqlite_operator")
    thumbnails = importlib.import_module("thumbnails")
    vaults = importlib.import_module("vaults")

    assert utils.DB_PATH == db_path
    assert utils.LOGS_DIR == vault_root / "logs"
    assert utils.THUMBNAILS_DIR == vault_root / "ui_cache" / "thumbnails"
    assert thumbnails.THUMBNAIL_DIR == utils.THUMBNAILS_DIR
    conn = sqlite_operator.connect_database()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 100
    finally:
        conn.close()

    health = vaults.audit_vault_health("default")
    assert health["hash_mismatches"] == []
    assert health["missing_files"]["wd"] == []
    assert health["workspace_dictionary_drift"] == {"missing_in_dictionary": 0, "unused_in_vault": 0}


def test_generated_vault_rows_notes_and_media_are_consistent(tmp_path, monkeypatch):
    generator = load_generator()
    generated_root = tmp_path / "generated"
    output = generated_root / "001-consistency"
    generator.main([
        "--output", str(output),
        "--generated-root", str(generated_root),
        "--items", "40",
        "--groups", "5",
        "--review", "2",
        "--video-ratio", "0.35",
        "--wd-tags", "30",
        "--wd-character-tags", "5",
        "--seed", "11",
    ])

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    vault_root = output / "data" / "vaults" / "default"
    conn = sqlite3.connect(vault_root / "db" / "lmz_main.db")
    try:
        samples = [manifest["items"][0], manifest["items"][7], next(item for item in manifest["items"] if item["mime_type"] == "video/mp4")]
        for item in samples:
            row = conn.execute(
                "SELECT storage_id, file_extension, mime_type, source_url, platform, source_artist FROM items WHERE hash = ?",
                (item["hash"],),
            ).fetchone()
            assert row == (
                item["storage_id"],
                item["extension"],
                item["mime_type"],
                item["source_url"],
                item["platform"],
                item["artist"],
            )

            shard = item["hash"][:2]
            asset_path = vault_root / "vault" / "assets" / shard / f"{item['storage_id']}{item['extension']}"
            note_path = vault_root / "vault" / "notes" / shard / f"{item['storage_id']}.md"
            wd_path = vault_root / "wd-tags" / shard / f"{item['storage_id']}.json"
            suffix = "_video" if item["mime_type"].startswith("video/") else ""
            thumb_path = vault_root / "ui_cache" / "thumbnails" / shard / f"{item['storage_id']}{suffix}.jpg"
            assert asset_path.exists()
            assert note_path.exists()
            assert wd_path.exists()
            assert thumb_path.exists()
            assert hashlib.sha256(asset_path.read_bytes()).hexdigest() == item["hash"]

            frontmatter = yaml.safe_load(note_path.read_text(encoding="utf-8").split("---", 2)[1])
            assert frontmatter["hash"] == item["hash"]
            assert frontmatter["storage_id"] == item["storage_id"]
            assert frontmatter["source_url"] == item["source_url"]
            assert frontmatter["platform"] == item["platform"]
            assert frontmatter["artist"] == item["artist"]
            assert frontmatter["topics"] == item["topic_links"]
            assert all(value.startswith("[topic_") and "topics/topic_" in value for value in frontmatter["topics"])
            assert frontmatter["wd_rating"] == item["wd_tags"]["rating"]
            assert frontmatter["wd_character_tags"] == item["wd_tags"]["characters"]
            assert frontmatter["wd_tags"] == item["wd_tags"]["general"]
            cache = json.loads(wd_path.read_text(encoding="utf-8"))
            assert cache["hash"] == item["hash"]
            assert cache["status"] == "ok"

        assert conn.execute("SELECT COUNT(*) FROM item_wd_tags").fetchone()[0] == manifest["counts"]["wd_rows_estimated"]
        assert conn.execute("SELECT COUNT(*) FROM metadata_facet_counts").fetchone()[0] == manifest["counts"]["metadata_index_facet_counts"]
        assert conn.execute("SELECT COUNT(*) FROM item_topics WHERE topic_rel != ''").fetchone()[0] == manifest["counts"]["metadata_index_topics"]
    finally:
        conn.close()

    sample_wd_tag = manifest["items"][0]["wd_tags"]["general"][0]
    monkeypatch.setenv("LMZ_CONFIG_PATH", str(output / "config.yaml"))
    monkeypatch.setenv("LMZ_AUTH_ROOT", str(tmp_path / "app-auth"))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    reset_backend_modules()
    library = importlib.import_module("api.library")

    filtered = library._get_items_sync(None, None, "newest", "all", [], [], [], [], [sample_wd_tag], [], None, 25)
    facets = library._get_facets_sync("wd_tag", sample_wd_tag, 25)

    assert filtered["items"]
    assert all(item["hash"] for item in filtered["items"])
    assert any(item["value"] == sample_wd_tag for item in facets["items"])


def test_generated_vault_refuses_runtime_paths(tmp_path):
    generator = load_generator()
    generated_root = tmp_path / "generated"
    for dangerous in [ROOT, ROOT / "data", ROOT / "config", ROOT / "logs", ROOT / "secrets"]:
        try:
            generator.main(["--output", str(dangerous), "--generated-root", str(generated_root)])
        except SystemExit as exc:
            assert exc.code != 0
        else:
            raise AssertionError(f"dangerous path was accepted: {dangerous}")


def test_generated_vault_auto_numbers_outputs(tmp_path):
    generator = load_generator()
    generated_root = tmp_path / "generated"
    generator.main(["--generated-root", str(generated_root), "--name", "small", "--items", "3", "--review", "0"])
    generator.main(["--generated-root", str(generated_root), "--name", "video mix", "--items", "3", "--review", "0"])

    assert (generated_root / "001-small").exists()
    assert (generated_root / "002-video-mix").exists()


def test_generated_vault_force_retries_windows_directory_reuse(monkeypatch, tmp_path):
    generator = load_generator()
    generated_root = tmp_path / "generated"
    output = generated_root / "existing"
    output.mkdir(parents=True)
    (output / "old.txt").write_text("old", encoding="utf-8")
    original_mkdir = Path.mkdir
    delayed = {"raised": False}
    sleeps = []

    def delayed_mkdir(path, *args, **kwargs):
        if path == output and not delayed["raised"]:
            delayed["raised"] = True
            raise PermissionError("directory name is still pending deletion")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", delayed_mkdir)
    monkeypatch.setattr(generator.time, "sleep", lambda seconds: sleeps.append(seconds))

    generator.main([
        "--generated-root", str(generated_root),
        "--output", str(output),
        "--items", "1",
        "--review", "0",
        "--force",
    ])

    assert delayed["raised"] is True
    assert sleeps == [0.05]
    assert not (output / "old.txt").exists()
    assert (output / "manifest.json").exists()
