import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import yaml
from config_repository import WorkspaceConfigRepository

from db.sqlite_operator import init_database
from media_lifecycle import storage_lifecycle_lock
from md_generator import generate_markdown, normalize_topic_list
from metadata_index import (
    ensure_metadata_schema,
    refresh_metadata_facet_counts_for_values,
    refresh_metadata_index_counters,
    safe_reindex_item_metadata,
)
from runtime_context import WorkspaceContext, get_runtime_context
from tagging.metadata import extra_groups
from topics import (
    format_topics_for_note,
    parse_topic_value,
    parse_topic_values,
    rename_topic as rename_topic_file,
    slugify_topic_label,
    topic_file_path_for_label,
)
from utils import atomic_write_text, note_path_for, utc_now_str


@dataclass
class MetadataMaintenanceResult:
    status: str = "success"
    vaults_touched: list[str] = field(default_factory=list)
    items_matched: int = 0
    notes_rewritten: int = 0
    items_reindexed: int = 0
    legacy_plain_refs_rewritten: int = 0
    errors: list[dict] = field(default_factory=list)

    def mark_touched(self, vault_id: str):
        if vault_id not in self.vaults_touched:
            self.vaults_touched.append(vault_id)

    def as_dict(self) -> dict:
        payload = {
            "status": "partial" if self.errors else self.status,
            "vaults_touched": self.vaults_touched,
            "items_matched": self.items_matched,
            "notes_rewritten": self.notes_rewritten,
            "items_reindexed": self.items_reindexed,
            "errors": self.errors,
        }
        if self.legacy_plain_refs_rewritten:
            payload["legacy_plain_refs_rewritten"] = self.legacy_plain_refs_rewritten
        return payload


def _ctx(ctx: WorkspaceContext | None = None) -> WorkspaceContext:
    return ctx or get_runtime_context()


def _topic_norm(value: str) -> str:
    return str(value or "").strip().casefold()


def _read_config(ctx: WorkspaceContext | None = None) -> dict:
    return WorkspaceConfigRepository(_ctx(ctx).config_path).read().value.model_dump(mode="json")


def _resolve_from_root(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _vault_entries(ctx: WorkspaceContext | None = None) -> list[dict]:
    runtime = _ctx(ctx)
    config = _read_config(runtime)
    vaults = config.get("vaults") if isinstance(config.get("vaults"), dict) else {}
    if not vaults:
        vault = runtime.active_vault
        return [{"id": vault.id, "name": vault.name, "root": vault.root, "db_path": vault.db_path, "active": True}]
    active_id = str(config.get("active_vault") or runtime.active_vault.id)
    entries = []
    for vault_id, entry in sorted(vaults.items()):
        vault_root = _resolve_from_root(runtime.root, str((entry or {}).get("root") or f"data/vaults/{vault_id}"))
        entries.append({
            "id": str(vault_id),
            "name": str((entry or {}).get("name") or vault_id),
            "root": vault_root,
            "db_path": vault_root / "db" / "lmz_main.db",
            "active": str(vault_id) == active_id,
        })
    return entries


def markdown_frontmatter_bounds(text: str) -> tuple[int, int] | None:
    stripped = text.lstrip("\ufeff")
    offset = len(text) - len(stripped)
    if not stripped.startswith("---"):
        return None
    lines = stripped.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    cursor = offset + len(lines[0])
    for line in lines[1:]:
        next_cursor = cursor + len(line)
        if line.strip() == "---":
            return offset, next_cursor
        cursor = next_cursor
    return None


def load_note_frontmatter_preserving_body(note_path: Path) -> tuple[dict, str]:
    text = note_path.read_text(encoding="utf-8")
    bounds = markdown_frontmatter_bounds(text)
    if bounds is None:
        return {}, text
    start, end = bounds
    frontmatter_text = text[start:end].split("---", 2)[1]
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, text[end:]


def write_note_frontmatter_preserving_body(note_path: Path, frontmatter: dict, body: str):
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(note_path, f"---\n{fm_text}---\n{body}")


def _restore_note_snapshots(
    snapshots: dict[Path, str],
    result: MetadataMaintenanceResult,
    vault_id: str,
    conn: sqlite3.Connection,
    vault_root: Path,
):
    for note_path, original_text in snapshots.items():
        try:
            storage_id = note_path.stem
            with storage_lifecycle_lock(storage_id, vault_root=vault_root):
                if conn.execute("SELECT 1 FROM items WHERE storage_id = ?", (storage_id,)).fetchone():
                    atomic_write_text(note_path, original_text)
        except Exception as exc:
            result.errors.append({
                "vault": vault_id,
                "path": str(note_path),
                "error": f"restore failed after DB rollback: {exc}",
            })


def _vault_note_path(vault_root: Path, item_hash: str, storage_id: str) -> Path:
    return vault_root / "vault" / "notes" / (str(item_hash or "")[:2] or "00") / f"{storage_id}.md"


def rewrite_metadata_notes_for_hashes(conn: sqlite3.Connection, item_hashes: Iterable[str], reason: str = "artist_rename", ctx: WorkspaceContext | None = None) -> MetadataMaintenanceResult:
    result = MetadataMaintenanceResult()
    for item_hash in list(dict.fromkeys(str(value or "").strip() for value in item_hashes if str(value or "").strip())):
        result.items_matched += 1
        md_content = generate_markdown(conn, item_hash)
        if not md_content:
            continue
        row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
        if not row or not row[0]:
            continue
        with storage_lifecycle_lock(row[0], ctx=ctx):
            current = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
            if not current or current[0] != row[0]:
                continue
            note_path = note_path_for(item_hash, row[0], ctx=ctx)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(note_path, md_content)
            safe_reindex_item_metadata(conn, item_hash, reason)
            result.notes_rewritten += 1
            result.items_reindexed += 1
    return result


def _replace_note_topics(note_path: Path, old_slug: str, old_norms: set[str], new_label: str | None) -> tuple[bool, int, list[str]]:
    frontmatter, body = load_note_frontmatter_preserving_body(note_path)
    raw_topics = normalize_topic_list(frontmatter.get("topics"))
    updated_topics: list[str] = []
    changed = False
    legacy_plain_refs = 0
    old_rel = f"{old_slug}.md".casefold()
    old_key = f"rel:{old_rel}"
    for raw in raw_topics:
        entry = parse_topic_value(raw, note_path)
        topic_key = str(entry.get("topic_key") or "").casefold()
        topic_rel = str(entry.get("topic_rel") or "").casefold()
        label_norm = _topic_norm(entry.get("label") or "")
        raw_norm = _topic_norm(raw)
        linked_match = topic_key == old_key or topic_rel == old_rel
        plain_match = topic_key.startswith("plain:") and (label_norm in old_norms or raw_norm in old_norms)
        if linked_match or plain_match:
            changed = True
            if plain_match and not linked_match:
                legacy_plain_refs += 1
            if new_label is not None:
                updated_topics.append(new_label)
        else:
            updated_topics.append(raw)
    if not changed:
        return False, 0, raw_topics
    formatted_topics = format_topics_for_note(updated_topics, note_path)
    frontmatter["topics"] = formatted_topics
    write_note_frontmatter_preserving_body(note_path, frontmatter, body)
    return True, legacy_plain_refs, formatted_topics


def _refresh_item_topic_rows(conn: sqlite3.Connection, item_hash: str, storage_id: str, note_path: Path, formatted_topics: list[str]):
    ensure_metadata_schema(conn)
    entries = parse_topic_values(formatted_topics, note_path)
    conn.execute("DELETE FROM item_topics WHERE item_hash = ?", (item_hash,))
    conn.executemany(
        "INSERT OR IGNORE INTO item_topics(item_hash, topic, topic_norm, topic_rel, topic_key) VALUES (?, ?, ?, ?, ?)",
        [
            (
                item_hash,
                entry["label"],
                _topic_norm(entry["label"]),
                entry["topic_rel"],
                entry["topic_key"],
            )
            for entry in entries
            if entry.get("label")
        ],
    )
    _refresh_metadata_file_note_stat(conn, item_hash, storage_id, note_path)


def _refresh_metadata_file_note_stat(conn: sqlite3.Connection, item_hash: str, storage_id: str, note_path: Path):
    try:
        stat = note_path.stat()
    except OSError:
        return
    conn.execute(
        """
        INSERT INTO item_metadata_files(
            item_hash, storage_id, note_path, note_mtime_ns, note_size,
            wd_path, wd_mtime_ns, wd_size, indexed_at, status, error
        )
        VALUES (?, ?, ?, ?, ?, '', NULL, NULL, ?, 'ok', '')
        ON CONFLICT(item_hash) DO UPDATE SET
            storage_id = excluded.storage_id,
            note_path = excluded.note_path,
            note_mtime_ns = excluded.note_mtime_ns,
            note_size = excluded.note_size,
            indexed_at = excluded.indexed_at,
            status = 'ok',
            error = ''
        """,
        (item_hash, storage_id, str(note_path), int(stat.st_mtime_ns), int(stat.st_size), utc_now_str()),
    )


def rewrite_topic_refs_across_workspace(old_label: str, new_label: str | None, ctx: WorkspaceContext | None = None) -> MetadataMaintenanceResult:
    old_slug = slugify_topic_label(old_label)
    new_slug = slugify_topic_label(new_label) if new_label is not None else ""
    old_norms = {_topic_norm(old_label), _topic_norm(old_slug)}
    changed_norms = set(old_norms)
    if new_slug:
        changed_norms.add(_topic_norm(new_slug))
    old_rel = f"{old_slug}.md"
    old_key = f"rel:{old_rel}".casefold()
    old_topic_norms = sorted(norm for norm in old_norms if norm)
    result = MetadataMaintenanceResult()
    if not old_topic_norms:
        return result

    for vault in _vault_entries(ctx):
        db_path = Path(vault["db_path"])
        if not db_path.exists():
            continue
        conn = init_database(db_path)
        note_snapshots: dict[Path, str] = {}
        try:
            ensure_metadata_schema(conn)
            placeholders = ",".join("?" for _ in old_topic_norms)
            rows = conn.execute(
                f"""
                SELECT DISTINCT items.hash, items.storage_id
                FROM item_topics
                JOIN items ON items.hash = item_topics.item_hash
                WHERE item_topics.topic_key = ?
                   OR item_topics.topic_rel = ?
                   OR item_topics.topic_norm IN ({placeholders})
                """,
                (old_key, old_rel, *old_topic_norms),
            ).fetchall()
            result.items_matched += len(rows)
            touched_this_vault = False
            for item_hash, storage_id in rows:
                if not storage_id:
                    continue
                note_path = _vault_note_path(Path(vault["root"]), item_hash, storage_id)
                try:
                    with storage_lifecycle_lock(storage_id, vault_root=Path(vault["root"])):
                        current = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
                        if not current or current[0] != storage_id:
                            continue
                        if not note_path.exists():
                            result.errors.append({"vault": vault["id"], "hash": item_hash, "error": "note missing"})
                            continue
                        note_snapshots.setdefault(note_path, note_path.read_text(encoding="utf-8"))
                        changed, legacy_count, formatted_topics = _replace_note_topics(note_path, old_slug, old_norms, new_label)
                        if not changed:
                            continue
                        _refresh_item_topic_rows(conn, item_hash, storage_id, note_path, formatted_topics)
                        result.notes_rewritten += 1
                        result.items_reindexed += 1
                        result.legacy_plain_refs_rewritten += legacy_count
                        touched_this_vault = True
                except Exception as exc:
                    result.errors.append({"vault": vault["id"], "hash": item_hash, "error": str(exc)})
            if touched_this_vault:
                refresh_metadata_facet_counts_for_values(conn, {("topic", norm) for norm in changed_norms})
                refresh_metadata_index_counters(conn)
                result.mark_touched(str(vault["id"]))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception as rollback_err:
                result.errors.append({"vault": vault["id"], "error": f"Database rollback failed: {rollback_err}"})
            _restore_note_snapshots(note_snapshots, result, str(vault["id"]), conn, Path(vault["root"]))
            result.errors.append({"vault": vault["id"], "error": str(exc)})
        finally:
            conn.close()
    return result


def rename_topic_across_workspace(old_label: str, new_label: str, ctx: WorkspaceContext | None = None) -> dict:
    rename_result = rename_topic_file(old_label, new_label, ctx=ctx)
    maintenance = rewrite_topic_refs_across_workspace(old_label, new_label, ctx=ctx)
    return {
        "status": "partial" if maintenance.errors else "success",
        "old_label": str(old_label or "").strip(),
        "new_label": str(new_label or "").strip(),
        "old_path": str(rename_result["old_path"]),
        "new_path": str(rename_result["new_path"]),
        "vaults_touched": maintenance.vaults_touched,
        "notes_rewritten": maintenance.notes_rewritten,
        "legacy_plain_refs_rewritten": maintenance.legacy_plain_refs_rewritten,
        "errors": maintenance.errors,
    }


def delete_topic_across_workspace(label: str, ctx: WorkspaceContext | None = None) -> dict:
    clean = str(label or "").strip()
    if not clean:
        raise ValueError("topic label is required")
    topic_path = topic_file_path_for_label(clean, ctx)
    if not topic_path.exists():
        raise FileNotFoundError(f"topic not found: {topic_path.name}")

    saved_content = topic_path.read_text(encoding="utf-8")
    topic_path.unlink()

    try:
        maintenance = rewrite_topic_refs_across_workspace(clean, None, ctx=ctx)
        if maintenance.errors:
            topic_path.write_text(saved_content, encoding="utf-8")
    except Exception:
        topic_path.write_text(saved_content, encoding="utf-8")
        raise

    payload = maintenance.as_dict()
    payload.update({"label": clean, "path": str(topic_path)})
    return payload


def merge_topic_across_workspace(source_label: str, target_label: str, ctx: WorkspaceContext | None = None) -> dict:
    clean_source = str(source_label or "").strip()
    clean_target = str(target_label or "").strip()
    if not clean_source or not clean_target:
        raise ValueError("source and target topic labels are required")
    if _topic_norm(clean_source) == _topic_norm(clean_target):
        raise ValueError("source and target topics must differ")
    source_path = topic_file_path_for_label(clean_source, ctx)
    target_path = topic_file_path_for_label(clean_target, ctx)
    if not source_path.exists():
        raise FileNotFoundError(f"topic not found: {source_path.name}")
    if not target_path.exists():
        raise FileNotFoundError(f"target topic not found: {target_path.name}")

    saved_content = source_path.read_text(encoding="utf-8")
    source_path.unlink()

    try:
        maintenance = rewrite_topic_refs_across_workspace(clean_source, clean_target, ctx=ctx)
        if maintenance.errors:
            source_path.write_text(saved_content, encoding="utf-8")
    except Exception:
        source_path.write_text(saved_content, encoding="utf-8")
        raise

    payload = maintenance.as_dict()
    payload.update({"source_label": clean_source, "target_label": clean_target, "source_path": str(source_path)})
    return payload


def _wd_rows_from_frontmatter(frontmatter: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    rating = str(frontmatter.get("wd_rating") or "").strip()
    if rating:
        rows.append(("rating", rating))
    rows.extend(("character", value) for value in normalize_topic_list(frontmatter.get("wd_character_tags")))
    rows.extend(("general", value) for value in normalize_topic_list(frontmatter.get("wd_tags")))
    for category, tags in extra_groups(frontmatter.get("tag_groups")).items():
        rows.extend((category, tag) for tag in tags)
    return rows


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        norm = _topic_norm(clean)
        if clean and norm not in seen:
            seen.add(norm)
            result.append(clean)
    return result


def _transform_wd_frontmatter(frontmatter: dict, tag_type: str | None, transform: Callable[[str], str | None]) -> tuple[bool, set[str]]:
    changed = False
    changed_norms: set[str] = set()

    def apply_scalar(field: str):
        nonlocal changed
        current = str(frontmatter.get(field) or "").strip()
        if not current:
            return
        updated = transform(current)
        if updated != current:
            changed = True
            changed_norms.add(_topic_norm(current))
            if updated:
                changed_norms.add(_topic_norm(updated))
                frontmatter[field] = updated
            else:
                frontmatter[field] = ""

    def apply_list(field: str):
        nonlocal changed
        values = normalize_topic_list(frontmatter.get(field))
        updated_values: list[str] = []
        field_changed = False
        for value in values:
            updated = transform(value)
            if updated != value:
                field_changed = True
                changed = True
                changed_norms.add(_topic_norm(value))
                if updated:
                    changed_norms.add(_topic_norm(updated))
                    updated_values.append(updated)
            else:
                updated_values.append(value)
        if field_changed:
            frontmatter[field] = _dedupe(updated_values)

    if tag_type in {None, "rating"}:
        apply_scalar("wd_rating")
    if tag_type in {None, "character"}:
        apply_list("wd_character_tags")
    if tag_type in {None, "general"}:
        apply_list("wd_tags")
    groups = extra_groups(frontmatter.get("tag_groups"))
    for category, tags in groups.items():
        if tag_type is not None and tag_type != category:
            continue
        updated_tags = []
        for value in tags:
            updated = transform(value)
            if updated != value:
                changed = True
                changed_norms.add(_topic_norm(value))
                if updated:
                    changed_norms.add(_topic_norm(updated))
            if updated:
                updated_tags.append(updated)
        groups[category] = _dedupe(updated_tags)
    if "tag_groups" in frontmatter:
        frontmatter["tag_groups"] = groups
    return changed, changed_norms


def _refresh_item_wd_rows(conn: sqlite3.Connection, item_hash: str, storage_id: str, note_path: Path, frontmatter: dict):
    ensure_metadata_schema(conn)
    conn.execute("DELETE FROM item_wd_tags WHERE item_hash = ?", (item_hash,))
    rows = [
        (item_hash, tag, _topic_norm(tag), tag_type)
        for tag_type, tag in _wd_rows_from_frontmatter(frontmatter)
        if _topic_norm(tag)
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO item_wd_tags(item_hash, tag, tag_norm, tag_type)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    _refresh_metadata_file_note_stat(conn, item_hash, storage_id, note_path)


def rewrite_wd_tags_across_workspace(tag: str, transform: Callable[[str], str | None], tag_type: str | None = None, ctx: WorkspaceContext | None = None) -> MetadataMaintenanceResult:
    clean = str(tag or "").strip()
    tag_norm = _topic_norm(clean)
    if not tag_norm:
        raise ValueError("WD tag is required")
    clean_type = str(tag_type or "").strip().casefold() or None
    if clean_type is not None and not all(char.isalnum() or char in "_-" for char in clean_type):
        raise ValueError("Tag type must be a category name")

    result = MetadataMaintenanceResult()
    changed_norms: set[str] = {tag_norm}
    for vault in _vault_entries(ctx):
        db_path = Path(vault["db_path"])
        if not db_path.exists():
            continue
        conn = init_database(db_path)
        note_snapshots: dict[Path, str] = {}
        try:
            ensure_metadata_schema(conn)
            params: list = [tag_norm]
            type_sql = ""
            if clean_type:
                type_sql = " AND tag_type = ?"
                params.append(clean_type)
            rows = conn.execute(
                f"""
                SELECT DISTINCT items.hash, items.storage_id
                FROM item_wd_tags
                JOIN items ON items.hash = item_wd_tags.item_hash
                WHERE item_wd_tags.tag_norm = ?{type_sql}
                """,
                tuple(params),
            ).fetchall()
            result.items_matched += len(rows)
            touched_this_vault = False
            for item_hash, storage_id in rows:
                if not storage_id:
                    continue
                note_path = _vault_note_path(Path(vault["root"]), item_hash, storage_id)
                try:
                    with storage_lifecycle_lock(storage_id, vault_root=Path(vault["root"])):
                        current = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
                        if not current or current[0] != storage_id:
                            continue
                        if not note_path.exists():
                            result.errors.append({"vault": vault["id"], "hash": item_hash, "error": "note missing"})
                            continue
                        note_snapshots.setdefault(note_path, note_path.read_text(encoding="utf-8"))
                        frontmatter, body = load_note_frontmatter_preserving_body(note_path)
                        changed, item_changed_norms = _transform_wd_frontmatter(frontmatter, clean_type, lambda value: transform(value) if _topic_norm(value) == tag_norm else value)
                        if not changed:
                            continue
                        write_note_frontmatter_preserving_body(note_path, frontmatter, body)
                        _refresh_item_wd_rows(conn, item_hash, storage_id, note_path, frontmatter)
                        changed_norms.update(item_changed_norms)
                        result.notes_rewritten += 1
                        result.items_reindexed += 1
                        touched_this_vault = True
                except Exception as exc:
                    result.errors.append({"vault": vault["id"], "hash": item_hash, "error": str(exc)})
            if touched_this_vault:
                refresh_metadata_facet_counts_for_values(conn, {("wd_tag", norm) for norm in changed_norms})
                refresh_metadata_index_counters(conn)
                result.mark_touched(str(vault["id"]))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception as rollback_err:
                result.errors.append({"vault": vault["id"], "error": f"Database rollback failed: {rollback_err}"})
            _restore_note_snapshots(note_snapshots, result, str(vault["id"]), conn, Path(vault["root"]))
            result.errors.append({"vault": vault["id"], "error": str(exc)})
        finally:
            conn.close()
    return result


def rename_wd_tag_across_workspace(old_tag: str, new_tag: str, tag_type: str | None = None, ctx: WorkspaceContext | None = None) -> dict:
    clean_new = str(new_tag or "").strip()
    if not clean_new:
        raise ValueError("new WD tag is required")
    result = rewrite_wd_tags_across_workspace(old_tag, lambda _value: clean_new, tag_type=tag_type, ctx=ctx)
    payload = result.as_dict()
    payload.update({"old_tag": str(old_tag or "").strip(), "new_tag": clean_new, "tag_type": tag_type})
    return payload


def delete_wd_tag_across_workspace(tag: str, tag_type: str | None = None, ctx: WorkspaceContext | None = None) -> dict:
    result = rewrite_wd_tags_across_workspace(tag, lambda _value: None, tag_type=tag_type, ctx=ctx)
    payload = result.as_dict()
    payload.update({"tag": str(tag or "").strip(), "tag_type": tag_type})
    return payload
