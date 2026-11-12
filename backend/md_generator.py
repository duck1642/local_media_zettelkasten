
import sqlite3
import yaml
from datetime import datetime

from tagging import wd_frontmatter_fields
from tagging.metadata import extra_groups
from topics import format_topics_for_note
from utils import note_path_for, require_storage_id, storage_shard_for_hash, utc_now_str

MANUAL_FRONTMATTER_FIELDS = (
    "title",
    "topics",
    "wd_rating",
    "wd_character_tags",
    "wd_tags",
    "tag_groups",
)
WD_FRONTMATTER_FIELDS = ("wd_rating", "wd_character_tags", "wd_tags")


def _format_date_added(date_added) -> str:
    if not date_added:
        return utc_now_str()
    if isinstance(date_added, datetime):
        return date_added.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(date_added, str):
        return date_added.split(".")[0] if "." in date_added else date_added
    return str(date_added)


def generate_markdown(conn: sqlite3.Connection, file_hash: str, asset_rel_path: str | None = None, title: str = "", topics_override: list | None = None, manual_overrides: dict | None = None, force_wd_from_cache: bool = False) -> str:
    """SQLite kaydı, mevcut not ve etiket önbelleğinden Markdown metni üret.

    Notu diske yazmaz. Normalde mevcut elle düzenlenen alanlar korunur;
    force_wd_from_cache etiketleri yeni tahminlerle yeniler. Kayıt yoksa
    boş metin döner.
    """

    cursor = conn.cursor()
    cursor.execute('''
        SELECT original_filename, mime_type, file_extension, source_url, platform, source_artist, date_added, phash, storage_id
        FROM items
        WHERE hash = ?
    ''', (file_hash,))

    row = cursor.fetchone()

    if not row:
        return ""

    original_filename, mime_type, file_extension, source_url, platform, source_artist, date_added, phash, storage_id = row

    storage_id = require_storage_id(storage_id)
    note_path = note_path_for(file_hash, storage_id)
    existing_frontmatter = load_note_frontmatter(file_hash, storage_id)
    date_added_str = _format_date_added(date_added)

    shard_folder = storage_shard_for_hash(file_hash)
    asset_link = asset_rel_path if asset_rel_path else f"../../assets/{shard_folder}/{storage_id}{file_extension}"

    is_video = mime_type.startswith('video/')
    asset_type = "video" if is_video else "image"


    frontmatter = {
        "hash": file_hash,
        "title": title or "",
        "filename": original_filename or "",
        "date_added": date_added_str,
        "type": asset_type,
        "source_url": source_url or "",
        "platform": platform or "",
        "artist": source_artist or "",
        "phash": phash or "",
        "topics": format_topics_for_note(topics_override, note_path) if topics_override is not None else load_note_topics(file_hash, storage_id),
        "file_format": mime_type or ""
    }
    for field in ("title",):
        if field in existing_frontmatter:
            frontmatter[field] = existing_frontmatter[field]
    if topics_override is None and "topics" in existing_frontmatter:
        frontmatter["topics"] = normalize_topic_list(existing_frontmatter.get("topics"))

    cache_wd_fields = wd_frontmatter_fields(file_hash, storage_id)
    for field in WD_FRONTMATTER_FIELDS:
        if force_wd_from_cache:
            frontmatter[field] = cache_wd_fields.get(field, [] if field != "wd_rating" else "")
        else:
            frontmatter[field] = existing_frontmatter[field] if field in existing_frontmatter else cache_wd_fields.get(field, [] if field != "wd_rating" else "")
    if "tag_groups" in existing_frontmatter or "tag_groups" in cache_wd_fields:
        # Normal akışta boş not grubu da bilinçli düzenleme sayılır; cache onu doldurmaz.
        groups = cache_wd_fields if force_wd_from_cache else existing_frontmatter if "tag_groups" in existing_frontmatter else cache_wd_fields
        frontmatter["tag_groups"] = extra_groups(groups.get("tag_groups"))
    for field, value in (manual_overrides or {}).items():
        if field in MANUAL_FRONTMATTER_FIELDS:
            frontmatter[field] = format_topics_for_note(value, note_path) if field == "topics" else extra_groups(value) if field == "tag_groups" else value


    fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return f"""---
{fm_str}---

![]({asset_link})
"""


def normalize_topic_list(topics) -> list[str]:
    if not topics:
        return []
    if isinstance(topics, list):
        return [str(topic).strip() for topic in topics if str(topic).strip()]
    normalized = str(topics).replace("\r", "\n").replace(",", "\n")
    return [topic.strip() for topic in normalized.split("\n") if topic.strip()]


def load_note_frontmatter(file_hash: str, storage_id: str) -> dict:
    path = note_path_for(file_hash, storage_id)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}
    yaml_text = "\n".join(lines[1:end_index])
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def load_note_topics(file_hash: str, storage_id: str, fallback=None) -> list[str]:
    frontmatter = load_note_frontmatter(file_hash, storage_id)
    if "topics" in frontmatter:
        return normalize_topic_list(frontmatter.get("topics"))
    return normalize_topic_list(fallback)


def load_note_wd_tags(file_hash: str, storage_id: str) -> dict:
    frontmatter = load_note_frontmatter(file_hash, storage_id)
    has_wd_fields = "tag_groups" in frontmatter or any(field in frontmatter for field in WD_FRONTMATTER_FIELDS)
    rating = str(frontmatter.get("wd_rating") or "").strip()
    characters = normalize_topic_list(frontmatter.get("wd_character_tags"))
    tags = normalize_topic_list(frontmatter.get("wd_tags"))
    return {
        "status": "ok" if has_wd_fields else "missing",
        "source": "yaml",
        "rating": {"label": rating} if rating else {},
        "character_tags": [{"name": tag} for tag in characters],
        "tags": [{"name": tag} for tag in tags],
        "groups": extra_groups(frontmatter.get("tag_groups")),
    }
