import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace

import pytest

from .test_mock_vault_isolation import fresh_backend


@pytest.mark.parametrize("kind", ["import", "restore"])
@pytest.mark.parametrize("has_database", [False, True])
def test_install_package_initializes_database_once(monkeypatch, tmp_path, kind, has_database):
    vaults, packages = fresh_backend(monkeypatch, tmp_path, "vaults", "vault_packages")
    package_type = packages.EXPORT_PACKAGE_TYPE if kind == "import" else packages.BACKUP_PACKAGE_TYPE
    manifest = packages.build_manifest(
        package_type=package_type,
        source_vault_id="example",
        source_vault_name="Example",
        contents={"assets": False, "notes": False, "db": has_database},
        item_count=0,
        file_count=int(has_database),
    )
    archive_path = tmp_path / "example.lmzvault.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(packages.MANIFEST_NAME, packages.manifest_bytes(manifest))
        if has_database:
            db_path = tmp_path / "package.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE package_marker (value TEXT)")
                conn.execute("INSERT INTO package_marker VALUES ('retained')")
                conn.commit()
            archive.write(db_path, "db/lmz_main.db")

    calls = []
    init_database = vaults.init_database

    def count_init(path):
        calls.append(path)
        return init_database(path)

    monkeypatch.setattr(vaults, "init_database", count_init)
    fingerprint = packages.package_fingerprint(archive_path)
    install = vaults.import_vault_package if kind == "import" else vaults.restore_backup_package
    result = install(archive_path, package_fingerprint_value=fingerprint, confirm=True)
    assert len(calls) == 1

    ctx = vaults.get_vault_context(result["vault"])
    with closing(sqlite3.connect(ctx.active_vault.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == (0,)
        if has_database:
            assert conn.execute("SELECT value FROM package_marker").fetchone() == ("retained",)


def test_vault_resolution_reuses_paths_without_switching_context(monkeypatch, tmp_path):
    vaults, runtime = fresh_backend(monkeypatch, tmp_path, "vaults", "runtime_context")
    current = runtime.get_runtime_context()
    vaults.create_vault("Another")
    customized = replace(current, models_dir=tmp_path / "custom-models")
    selected = vaults.get_vault_context("another", ctx=customized)
    rebuilt = runtime.build_runtime_context(current.config_path, "another")
    assert selected.active_vault == rebuilt.active_vault
    assert selected == replace(customized, active_vault=rebuilt.active_vault)
    assert runtime.get_runtime_context() is current


def test_workspace_schema_reuses_definitions_without_vault_backfill(monkeypatch, tmp_path):
    workspace, artists, platforms = fresh_backend(
        monkeypatch, tmp_path, "workspace_db", "artists", "platforms"
    )
    with closing(sqlite3.connect(":memory:")) as workspace_db, closing(sqlite3.connect(":memory:")) as definitions:
        workspace.ensure_workspace_schema(workspace_db)
        workspace.ensure_workspace_schema(workspace_db)
        artists.ensure_artist_schema(definitions, backfill=False)
        platforms.ensure_platform_schema(definitions, backfill=False)
        sql = "SELECT name, sql FROM sqlite_master WHERE tbl_name IN ('artists','artist_aliases','artist_links','platforms','platform_aliases') ORDER BY name"
        assert workspace_db.execute(sql).fetchall() == definitions.execute(sql).fetchall()
        assert workspace_db.execute("SELECT COUNT(*) FROM platforms").fetchone() == (0,)


def test_vault_health_reports_compact_orphans_without_modifying_files(monkeypatch, tmp_path):
    vaults, db, runtime = fresh_backend(
        monkeypatch, tmp_path, "vaults", "db.sqlite_operator", "runtime_context"
    )
    ctx = runtime.get_runtime_context()
    vault = ctx.active_vault
    asset = vault.assets_dir / "ab" / "000000000001.jpg"
    note = vault.notes_dir / "ab" / "000000000001.md"
    for path, content in [(asset, b"asset"), (note, b"note")]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with closing(db.init_database(ctx=ctx)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        report = vaults.audit_vault_health(vault.id, ctx=ctx)
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == before
    assert str(asset) in report["orphans"]["assets"]
    assert str(note) in report["orphans"]["notes"]
    assert asset.read_bytes() == b"asset"
    assert note.read_bytes() == b"note"
