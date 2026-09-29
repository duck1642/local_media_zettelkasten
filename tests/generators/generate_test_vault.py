import argparse
import hashlib
import importlib
import json
import os
import random
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config_schema import WorkspaceConfig


DEFAULT_ROOT = ROOT / "tests" / "generated"
FORBIDDEN_OUTPUTS = {
    ROOT / "data",
    ROOT / "config",
    ROOT / "logs",
    ROOT / "secrets",
}
LOG_FILES = [
    "system.jsonl",
    "auth.jsonl",
    "review.jsonl",
    "ingest_local.jsonl",
    "ingest_online.jsonl",
    "ingestion_audit.jsonl",
]
WD_RATINGS = ("safe", "questionable", "explicit")


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    return "-".join(part for part in cleaned.split("-") if part) or "vault"


def _topic_slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    return "_".join(part for part in cleaned.split("_") if part) or "topic"


def _next_numbered_output(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    highest = 0
    for child in root.iterdir():
        if child.is_dir() and len(child.name) >= 3 and child.name[:3].isdigit():
            highest = max(highest, int(child.name[:3]))
    return root / f"{highest + 1:03d}-{_slug(name)}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _guard_output(output: Path, generated_root: Path, allow_outside_generated: bool):
    output = _resolve(output)
    generated_root = _resolve(generated_root)
    forbidden = {_resolve(path) for path in FORBIDDEN_OUTPUTS}
    if output == _resolve(ROOT):
        raise SystemExit(f"Refusing dangerous output path: {output}")
    if output in forbidden:
        raise SystemExit(f"Refusing dangerous output path: {output}")
    for path in forbidden:
        if _is_relative_to(output, path):
            raise SystemExit(f"Refusing output inside runtime path: {output}")
    if not allow_outside_generated and not _is_relative_to(output, generated_root):
        raise SystemExit(f"Output must be inside {generated_root}")


def _write_yaml(path: Path, data: dict):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _topic_file(topic_dir: Path, label: str) -> Path:
    return topic_dir / f"{_topic_slug(label)}.md"


def _ensure_topic_file(topic_dir: Path, label: str):
    path = _topic_file(topic_dir, label)
    if path.exists():
        return
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "created_at": timestamp,
        "updated_at": timestamp,
        "aliases": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n\n", encoding="utf-8")


def _topic_link(note_path: Path, topic_dir: Path, label: str) -> str:
    _ensure_topic_file(topic_dir, label)
    topic_path = _topic_file(topic_dir, label)
    rel = os.path.relpath(topic_path.resolve(), note_path.parent.resolve()).replace("\\", "/")
    return f"[{topic_path.stem}]({rel})"


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _storage_id(index: int) -> str:
    return f"lmz{index:06d}"


def _timestamp(index: int) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S")


def _shard(item_hash: str) -> str:
    return item_hash[:2]


def _svg_bytes(label: str, width: int, height: int) -> bytes:
    text = label[:24]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<rect width="100%" height="100%" fill="#1f2937"/>'
        f'<rect x="16" y="16" width="{max(1, width - 32)}" height="{max(1, height - 32)}" fill="#334155"/>'
        f'<text x="24" y="{max(40, height // 2)}" fill="#f8fafc" font-family="monospace" font-size="18">{text}</text>'
        "</svg>"
    )
    return svg.encode("utf-8")


def _asset_bytes(storage_id: str, width: int, height: int, is_video: bool) -> bytes:
    if is_video:
        return f"lmz synthetic video placeholder:{storage_id}:{width}x{height}".encode("utf-8")
    return _svg_bytes(storage_id, width, height)


def _tag_item(name: str, score: float = 0.9) -> dict:
    return {
        "name": name.replace(" ", "_"),
        "display_name": name,
        "score": round(float(score), 6),
    }


def _write_wd_cache(path: Path, item_hash: str, storage_id: str, mime_type: str, wd_tags: dict, args: argparse.Namespace):
    path.parent.mkdir(parents=True, exist_ok=True)
    rating = None
    if wd_tags.get("rating"):
        rating = _tag_item(str(wd_tags["rating"]), 0.99)
        rating["label"] = str(wd_tags["rating"])
    payload = {
        "hash": item_hash,
        "status": "ok",
        "model": "generated/test-vault",
        "threshold": 0.35,
        "created_at": _timestamp(0),
        "rating": rating,
        "character_tags": [_tag_item(tag, 0.95) for tag in wd_tags.get("characters") or []],
        "tags": [_tag_item(tag, 0.9) for tag in wd_tags.get("general") or []],
        "provider": "generator",
        "device": "cpu",
        "max_tags": max(0, int(args.wd_tags_per_item or 0)),
        "error": "",
        "media_type": "video" if str(mime_type or "").startswith("video/") else "image",
        "sampled_frames": [],
        "frame_count": 0,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _wd_values(index: int, args: argparse.Namespace) -> dict:
    wd_tag_count = max(0, int(args.wd_tags or 0))
    wd_character_count = max(0, int(args.wd_character_tags or 0))
    general_per_item = max(0, int(args.wd_tags_per_item or 0))
    characters_per_item = max(0, int(args.wd_character_tags_per_item or 0))
    enabled = wd_tag_count > 0 or wd_character_count > 0
    if not enabled:
        return {"rating": "", "characters": [], "general": []}
    characters = [
        f"character-{(index + offset) % wd_character_count:06d}"
        for offset in range(min(characters_per_item, wd_character_count))
    ] if wd_character_count else []
    general = [
        f"wd-tag-{(index * max(1, general_per_item) + offset) % wd_tag_count:06d}"
        for offset in range(min(general_per_item, wd_tag_count))
    ] if wd_tag_count else []
    return {
        "rating": WD_RATINGS[index % len(WD_RATINGS)],
        "characters": characters,
        "general": general,
    }


def _frontmatter(item: dict, topics: list[str], wd_tags: dict) -> str:
    data = {
        "title": item["original_filename"],
        "hash": item["hash"],
        "storage_id": item["storage_id"],
        "date_added": item["date_added"],
        "platform": item["platform"],
        "artist": item["artist"],
        "source_url": item["source_url"],
        "topics": topics,
    }
    if wd_tags.get("rating"):
        data["wd_rating"] = wd_tags["rating"]
    if wd_tags.get("characters"):
        data["wd_character_tags"] = wd_tags["characters"]
    if wd_tags.get("general"):
        data["wd_tags"] = wd_tags["general"]
    return f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n\nSynthetic test vault item.\n"


def _config() -> dict:
    return WorkspaceConfig(
        active_vault="default",
        vaults={"default": {"name": "Default", "root": "data/vaults/default"}},
    ).model_dump(mode="json")


def _reset_backend_modules():
    for name in list(sys.modules):
        if name in {"api", "utils", "runtime_context", "metadata_index", "md_generator", "thumbnails", "artists", "platforms", "topics", "workspace_db", "vaults", "workspaces"} or name.startswith(("api.", "db.", "logger", "tagging")):
            del sys.modules[name]


def _init_database(config_path: Path):
    os.environ["LMZ_CONFIG_PATH"] = str(config_path)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    _reset_backend_modules()
    sqlite_operator = importlib.import_module("db.sqlite_operator")
    conn = sqlite_operator.init_database()
    conn.close()


def _rebuild_metadata_index(config_path: Path) -> dict:
    os.environ["LMZ_CONFIG_PATH"] = str(config_path)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    _reset_backend_modules()
    sqlite_operator = importlib.import_module("db.sqlite_operator")
    metadata_index = importlib.import_module("metadata_index")
    conn = sqlite_operator.init_database()
    try:
        metadata_index.ensure_metadata_schema(conn)
        result = metadata_index.rebuild_all_metadata(conn, batch_size=500, context="generated_vault")
        conn.commit()
        status = metadata_index.metadata_index_status(conn, deep=False)
        return {
            "indexed": result["indexed"],
            "errors": result["errors"],
            "topics": status["topics"],
            "wd_tags": status["wd_tags"],
            "facet_counts": status["facet_counts"],
        }
    finally:
        conn.close()


def _rebuild_workspace_metadata(config_path: Path) -> dict:
    os.environ["LMZ_CONFIG_PATH"] = str(config_path)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    _reset_backend_modules()
    workspace_db = importlib.import_module("workspace_db")
    return workspace_db.rebuild_workspace_metadata()


def _insert_rows(db_path: Path, rows: list[dict]):
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO items(
            hash, original_filename, file_extension, mime_type, size_bytes,
            date_added, source_url, source_url_norm, platform, source_artist,
            phash, audio_hash, visual_embedding, width, height, storage_id
        )
        VALUES (
            :hash, :original_filename, :file_extension, :mime_type, :size_bytes,
            :date_added, :source_url, :source_url_norm, :platform, :artist,
            :phash, NULL, NULL, :width, :height, :storage_id
        )
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO storage_id_counter(id, next_value)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET next_value = excluded.next_value
        """,
        (len(rows) + 1,),
    )
    conn.commit()
    conn.close()


def _write_review_fixture(output: Path, index: int, item: dict):
    review_dir = output / "data" / "vaults" / "default" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".mp4" if item["mime_type"].startswith("video/") else ".jpg"
    media_name = f"review_{index:04d}{suffix}"
    media_path = review_dir / media_name
    media_path.write_bytes(b"lmz synthetic review video" if suffix == ".mp4" else _svg_bytes(media_name, 320, 240))
    sidecar = {
        "status": "pending",
        "reason": "synthetic_review",
        "original_path": str(media_path),
        "file_hash": item["hash"],
        "storage_id": item["storage_id"],
        "metadata": {
            "source_url": item["source_url"],
            "platform": item["platform"],
            "artist": item["artist"],
        },
    }
    (review_dir / f"{media_name}.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")


def generate_vault(args: argparse.Namespace) -> Path:
    generated_root = _resolve(args.generated_root)
    output = _resolve(args.output) if args.output else _next_numbered_output(generated_root, args.name)
    _guard_output(output, generated_root, args.allow_outside_generated)
    if output.exists():
        if not args.force:
            raise SystemExit(f"Output exists; pass --force to overwrite: {output}")
        tombstone = output.with_name(f".{output.name}.delete-{os.getpid()}-{time.time_ns()}")
        output.replace(tombstone)
        shutil.rmtree(tombstone)
    for attempt in range(20):
        try:
            output.mkdir(parents=True)
            break
        except PermissionError:
            if attempt == 19:
                raise
            # Windows can briefly retain a removed directory name.
            time.sleep(0.05)

    config_path = output / "config.yaml"
    _write_yaml(config_path, _config())

    data_dir = output / "data"
    vault_root = data_dir / "vaults" / "default"
    assets_dir = vault_root / "vault" / "assets"
    notes_dir = vault_root / "vault" / "notes"
    topics_dir = data_dir / "topics"
    thumbs_dir = vault_root / "ui_cache" / "thumbnails"
    logs_dir = vault_root / "logs"
    for directory in [
        vault_root / "db",
        assets_dir,
        notes_dir,
        thumbs_dir,
        topics_dir,
        vault_root / "review",
        logs_dir / "raw",
        logs_dir / "structured",
        vault_root / "queues",
        vault_root / "batches",
        vault_root / "input",
        vault_root / "local_ingest",
        vault_root / "online_ingest",
        vault_root / "wd-tags",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    _init_database(config_path)

    for filename in LOG_FILES:
        (logs_dir / "structured" / filename).touch()
    (logs_dir / "raw" / "console.log").touch()

    rng = random.Random(args.seed)
    platforms = [value.strip() for value in args.platforms.split(",") if value.strip()]
    platform_count = max(1, len(platforms))
    topic_count = max(1, args.topics)
    artist_count = max(1, args.artists)
    unknown_artist_count = min(args.items, max(0, int(round(args.items * float(args.unknown_artist_ratio or 0)))))
    group_count = max(1, args.groups)
    rows = []
    items = []

    for index in range(args.items):
        ordinal = index + 1
        storage_id = _storage_id(ordinal)
        is_video = rng.random() < args.video_ratio
        ext = ".mp4" if is_video else ".jpg"
        mime_type = "video/mp4" if is_video else "image/jpeg"
        width = 640 + (index % 5) * 80
        height = 360 + (index % 7) * 40
        asset_bytes = _asset_bytes(storage_id, width, height, is_video)
        item_hash = _content_hash(asset_bytes)
        group_id = index % group_count
        platform = platforms[index % platform_count]
        if index < unknown_artist_count:
            artist = "Unknown"
        else:
            artist = f"artist-{(index - unknown_artist_count) % artist_count:06d}"
        topic_labels = [f"topic-{(index + offset) % topic_count:03d}" for offset in range(1 + (index % min(3, topic_count)))]
        wd_tags = _wd_values(index, args)
        source_url = f"https://synthetic.local/group/{group_id:06d}"
        original_filename = f"{storage_id}{ext}"
        date_added = _timestamp(index)
        row = {
            "hash": item_hash,
            "storage_id": storage_id,
            "original_filename": original_filename,
            "file_extension": ext,
            "mime_type": mime_type,
            "size_bytes": len(asset_bytes),
            "date_added": date_added,
            "source_url": source_url,
            "source_url_norm": source_url.rstrip("/").lower(),
            "platform": platform,
            "artist": artist,
            "phash": f"{index:016x}"[-16:],
            "width": width,
            "height": height,
        }
        rows.append(row)

        shard = _shard(item_hash)
        asset_path = assets_dir / shard / original_filename
        note_path = notes_dir / shard / f"{storage_id}.md"
        thumb_path = thumbs_dir / shard / f"{storage_id}{'_video' if is_video else ''}.jpg"
        wd_path = vault_root / "wd-tags" / shard / f"{storage_id}.json"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(asset_bytes)
        linked_topics = [_topic_link(note_path, topics_dir, label) for label in topic_labels]
        note_path.write_text(_frontmatter(row, linked_topics, wd_tags), encoding="utf-8")
        _write_wd_cache(wd_path, item_hash, storage_id, mime_type, wd_tags, args)
        thumb_path.write_bytes(_svg_bytes(f"thumb-{storage_id}", 320, 240))

        items.append({
            "hash": item_hash,
            "storage_id": storage_id,
            "artist": artist,
            "platform": platform,
            "source_url": source_url,
            "date_added": date_added,
            "mime_type": mime_type,
            "extension": ext,
            "original_filename": original_filename,
            "width": width,
            "height": height,
            "topics": topic_labels,
            "topic_links": linked_topics,
            "wd_tags": wd_tags,
            "url": f"/vault/{shard}/{original_filename}",
            "thumbnail_url": f"/api/thumbnails/{item_hash}",
        })

    _insert_rows(vault_root / "db" / "lmz_main.db", rows)
    metadata_report = _rebuild_metadata_index(config_path)
    workspace_report = _rebuild_workspace_metadata(config_path)

    for index, item in enumerate(items[: max(0, args.review)], start=1):
        _write_review_fixture(output, index, item)

    manifest = {
        "generator": "generate_test_vault.py",
        "version": 1,
        "seed": args.seed,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "config_path": "config.yaml",
        "db_path": "data/vaults/default/db/lmz_main.db",
        "counts": {
            "items": args.items,
            "videos": sum(1 for item in items if item["mime_type"].startswith("video/")),
            "images": sum(1 for item in items if item["mime_type"].startswith("image/")),
            "review": max(0, args.review),
            "groups": group_count,
            "artists": artist_count,
            "unknown_artist_items": unknown_artist_count,
            "platforms": platform_count,
            "topics": topic_count,
            "topic_files": len(list(topics_dir.glob("*.md"))),
            "wd_tag_pool": max(0, args.wd_tags),
            "wd_character_tag_pool": max(0, args.wd_character_tags),
            "wd_rows_estimated": sum(
                (1 if item["wd_tags"].get("rating") else 0)
                + len(item["wd_tags"].get("characters") or [])
                + len(item["wd_tags"].get("general") or [])
                for item in items
            ),
            "metadata_index_topics": metadata_report["topics"],
            "metadata_index_wd_tags": metadata_report["wd_tags"],
            "metadata_index_facet_counts": metadata_report["facet_counts"],
            "workspace_wd_tags": workspace_report["after"]["wd_tags"],
        },
        "items": items,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an isolated LMZ synthetic test vault")
    parser.add_argument("--name", default="vault", help="Name suffix for auto-numbered output")
    parser.add_argument("--output", type=Path, help="Explicit output path")
    parser.add_argument("--generated-root", type=Path, default=DEFAULT_ROOT, help="Generated vault root")
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--groups", type=int, default=100)
    parser.add_argument("--review", type=int, default=10)
    parser.add_argument("--video-ratio", type=float, default=0.1)
    parser.add_argument("--artists", type=int, default=50)
    parser.add_argument("--unknown-artist-ratio", type=float, default=0.05)
    parser.add_argument("--platforms", default="Local,Pixiv,Instagram,X,Pinterest,YouTube")
    parser.add_argument("--topics", type=int, default=25)
    parser.add_argument("--wd-tags", type=int, default=0, help="Unique general WD tag pool size")
    parser.add_argument("--wd-character-tags", type=int, default=0, help="Unique WD character tag pool size")
    parser.add_argument("--wd-tags-per-item", type=int, default=20)
    parser.add_argument("--wd-character-tags-per-item", type=int, default=1)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-outside-generated", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print generated vault paths as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.items < 1 or args.items > 100_000:
        parser.error("--items must be between 1 and 100000")
    if args.groups < 1:
        parser.error("--groups must be at least 1")
    if args.review < 0:
        parser.error("--review must be non-negative")
    if args.video_ratio < 0 or args.video_ratio > 1:
        parser.error("--video-ratio must be between 0 and 1")
    if args.unknown_artist_ratio < 0 or args.unknown_artist_ratio > 1:
        parser.error("--unknown-artist-ratio must be between 0 and 1")
    if args.wd_tags < 0:
        parser.error("--wd-tags must be non-negative")
    if args.wd_character_tags < 0:
        parser.error("--wd-character-tags must be non-negative")
    if args.wd_tags_per_item < 0:
        parser.error("--wd-tags-per-item must be non-negative")
    if args.wd_character_tags_per_item < 0:
        parser.error("--wd-character-tags-per-item must be non-negative")
    output = generate_vault(args)
    if args.json:
        payload = {
            "output": str(output),
            "config_path": str(output / "config.yaml"),
            "manifest_path": str(output / "manifest.json"),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
