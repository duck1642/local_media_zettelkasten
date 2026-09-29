
import shutil
import json
import time
import secrets
from pathlib import Path
from typing import Tuple, Optional, List
from utils import (
    calculate_file_hash, calculate_phash,
    atomic_write_text, flatten_image, get_normalization_color, note_path_for,
    storage_asset_path_for, storage_shard_for_hash, utc_now, utc_now_str
)
from runtime_context import get_runtime_context, WorkspaceContext
from fingerprint import (
    get_audio_fingerprint, get_visual_embedding,
    get_video_duration
)
from validators import get_mime_type, is_allowed_mime
from db.sqlite_operator import (
    connect_database, check_duplicate_hash, insert_to_database, allocate_storage_id,
    insert_tiles
)
from db.search_manager import search_manager
from md_generator import generate_markdown
from metadata_index import safe_reindex_item_metadata
from logger import log_activity, log_system
from tagging import tag_media
from thumbnails import ensure_thumbnail
from review_cache import pending_review_match as cached_pending_review_match, upsert_review_cache_entry

IMAGE_REVIEW_PHASH_THRESHOLD = 12

def _ctx(ctx: WorkspaceContext | None = None) -> WorkspaceContext:
    return ctx or get_runtime_context()


def _safe_review_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if char in invalid or ord(char) < 32 else char for char in str(name or "")).strip(" .")
    if not safe:
        safe = "review_item"
    if len(safe) > 180:
        original = Path(safe)
        safe = f"{original.stem[:140]}{original.suffix[:20]}"
    return safe

def _review_original_name(filepath: Path, metadata: dict) -> str:
    explicit = str(metadata.get("original_name") or "").strip()
    if explicit:
        return Path(explicit).name
    source_path = str(metadata.get("original_path") or metadata.get("source_path") or "").strip()
    if source_path:
        source_name = Path(source_path).name
        if source_name:
            return source_name
    return filepath.name

def _move_to_review(filepath: Path, file_hash: str, metadata: dict, sidecar_fields: dict, ctx: WorkspaceContext | None = None) -> Path:
    review_dir = _ctx(ctx).active_vault.review_dir
    review_dir.mkdir(parents=True, exist_ok=True)
    original_name = _review_original_name(filepath, metadata)
    safe_original_name = _safe_review_name(original_name)
    for _ in range(20):
        review_id = f"{utc_now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
        storage_name = f"{review_id}_{file_hash[:8]}_{safe_original_name}"
        dest_path = review_dir / storage_name
        sidecar_path = dest_path.with_suffix(dest_path.suffix + ".json")
        if not dest_path.exists() and not sidecar_path.exists():
            break
    else:
        raise RuntimeError("Could not allocate unique review filename")

    shutil.move(filepath, dest_path)
    source_path = str(metadata.get("original_path") or metadata.get("source_path") or filepath)
    staged_from = str(metadata.get("staged_from") or ("online" if metadata.get("source_url") else "unknown"))
    sidecar = {
        "review_id": review_id,
        "storage_name": storage_name,
        "original_name": original_name,
        "source_path": source_path,
        "staged_from": staged_from,
        "state": "pending",
        "timestamp": utc_now_str(),
        "metadata": metadata,
        "file_hash": file_hash,
        **sidecar_fields,
    }
    with open(sidecar_path, 'w', encoding='utf-8') as f:
        json.dump(sidecar, f, indent=4, ensure_ascii=False)
    upsert_review_cache_entry(dest_path, sidecar, ctx=ctx)
    return dest_path

def _pending_review_match(file_hash: str, ctx: WorkspaceContext | None = None) -> dict | None:
    return cached_pending_review_match(file_hash, ctx=ctx)

def calculate_tiles(filepath: Path, ratio_threshold: float = 3.0) -> list:

    try:
        from PIL import Image
        import imagehash
        from utils import get_app_settings

        config = get_app_settings()
        proc_config = config.get('ingestion', {}).get('processing', {})
        do_flatten = proc_config.get('flatten_transparency', False)
        bg_color = get_normalization_color(proc_config)

        tiles = []
        with Image.open(filepath) as raw_img:
            img = flatten_image(raw_img, bg_color) if do_flatten else raw_img
            try:
                w, h = img.size

                if w == 0 or h == 0:
                    from logger import log_system
                    log_system("WARNING", "Degenerate image with 0-pixel dimension", file=str(filepath), width=w, height=h)
                    return []
                ratio = max(w, h) / min(w, h)

                if ratio < ratio_threshold:
                    return []

                if h > w:
                    num_tiles = h // w
                    for i in range(num_tiles):
                        top = i * w
                        bottom = top + w
                        tile = img.crop((0, top, w, bottom))
                        try:
                            tiles.append((i, str(imagehash.phash(tile))))
                        finally:
                            tile.close()
                else:
                    num_tiles = w // h
                    for i in range(num_tiles):
                        left = i * h
                        right = left + h
                        tile = img.crop((left, 0, right, h))
                        try:
                            tiles.append((i, str(imagehash.phash(tile))))
                        finally:
                            tile.close()
            finally:
                if img is not raw_img:
                    img.close()
        return tiles
    except Exception:
        return []

def find_visual_duplicate(new_phash: str, threshold: int = IMAGE_REVIEW_PHASH_THRESHOLD, new_tiles: list = None, ctx: WorkspaceContext | None = None, return_all: bool = False) -> Tuple[Optional[str], Optional[str], int, Optional[int], List[str]] | Tuple[Optional[str], Optional[str], int, Optional[int]]:

    best_match = None
    match_type = None
    min_distance = None
    total_conflicts = 0
    all_hashes = []

    try:
        all_matches_dict = {}

        try:
            matches = search_manager.query_image(new_phash, threshold, ctx=ctx)
        except TypeError:
            matches = search_manager.query_image(new_phash, threshold)

        for f_hash, dist, m_type in matches:
            total_conflicts += 1
            if f_hash not in all_matches_dict or dist < all_matches_dict[f_hash]:
                all_matches_dict[f_hash] = dist
            if min_distance is None or dist < min_distance:
                min_distance = dist
                best_match = f_hash
                match_type = f"{m_type} (Dist: {dist})"

        if not best_match and new_tiles:
            for t_index, t_phash in new_tiles:
                try:
                    tile_matches = search_manager.query_global_only(t_phash, threshold, ctx=ctx)
                except TypeError:
                    tile_matches = search_manager.query_global_only(t_phash, threshold)
                if tile_matches:
                    for fh, dist in tile_matches:
                        if fh not in all_matches_dict or dist < all_matches_dict[fh]:
                            all_matches_dict[fh] = dist
                    f_hash, dist = tile_matches[0]
                    total_conflicts += len(tile_matches)
                    best_match = f_hash
                    min_distance = dist
                    match_type = f"Whole-to-Fragment (Tile #{t_index})"
                    break

        sorted_keys = sorted(all_matches_dict.keys(), key=lambda h: all_matches_dict[h])
        if best_match and best_match in sorted_keys:
            sorted_keys.remove(best_match)
            sorted_keys.insert(0, best_match)
        elif best_match and best_match not in sorted_keys:
            sorted_keys.insert(0, best_match)
        all_hashes = sorted_keys

    except Exception as e:
        log_system("ERROR", f"Search error in find_visual_duplicate: {e}")

    if return_all:
        return best_match, match_type, total_conflicts, min_distance, all_hashes
    return best_match, match_type, total_conflicts, min_distance

def find_video_duplicate(audio_hash: bytes, visual_embedding: bytes, ai_threshold: float = 0.08, ctx: WorkspaceContext | None = None, return_all: bool = False) -> Tuple[Optional[str], Optional[str], int, Optional[float], List[str]] | Tuple[Optional[str], Optional[str], int, Optional[float]]:

    best_match = None
    match_type = None
    max_similarity = 0.0
    total_conflicts = 0
    all_hashes = []

    try:

        try:
            matches = search_manager.query_video(audio_hash, visual_embedding, ai_threshold, ctx=ctx)
        except TypeError:
            matches = search_manager.query_video(audio_hash, visual_embedding, ai_threshold)


        conflict_map = {}
        for f_hash, similarity, m_type in matches:
            total_conflicts += 1
            if f_hash not in conflict_map:
                conflict_map[f_hash] = {"similarity": similarity, "types": [m_type]}
            else:
                conflict_map[f_hash]["types"].append(m_type)
                if similarity > conflict_map[f_hash]["similarity"]:
                    conflict_map[f_hash]["similarity"] = similarity


        for f_hash, data in conflict_map.items():
            if len(data["types"]) > 1:
                best_match = f_hash
                max_similarity = data["similarity"]
                match_type = f"High Confidence (Double-Match: {', '.join(data['types'])})"
                break


        if not best_match and conflict_map:
            sorted_conflicts = sorted(conflict_map.items(), key=lambda x: x[1]["similarity"], reverse=True)
            best_match, data = sorted_conflicts[0]
            max_similarity = data["similarity"]
            match_type = f"{data['types'][0]} Match"

        sorted_conflicts_all = sorted(
            conflict_map.items(),
            key=lambda x: (len(x[1]["types"]) > 1, x[1]["similarity"]),
            reverse=True
        )
        sorted_keys = [fh for fh, _ in sorted_conflicts_all]
        if best_match and best_match in sorted_keys:
            sorted_keys.remove(best_match)
            sorted_keys.insert(0, best_match)
        elif best_match and best_match not in sorted_keys:
            sorted_keys.insert(0, best_match)
        all_hashes = sorted_keys

    except Exception as e:
        log_system("ERROR", f"Search error in find_video_duplicate: {e}")

    if return_all:
        return best_match, match_type, total_conflicts, max_similarity if best_match else None, all_hashes
    return best_match, match_type, total_conflicts, max_similarity if best_match else None

def process_file(filepath: Path, config: dict, metadata: dict = None, delete_source: bool = False, skip_similarity: bool = False, sync_index: bool = True, ctx: WorkspaceContext | None = None, allow_pending_review: bool = False) -> Tuple[bool, str, Optional[dict]]:

    metadata = metadata or {}
    index_data = None

    accepted_media = config.get('ingestion', {}).get('accepted_media', {})
    allowed_mimes = accepted_media.get('mime_types', [])


    mime_type = get_mime_type(filepath) or "unknown"
    if not is_allowed_mime(mime_type, allowed_mimes):
        log_system("WARNING", "Skipped: Invalid MIME type", file=filepath.name, mime=mime_type)
        return False, f"Invalid MIME: {mime_type}", None


    allowed_exts_config = accepted_media.get('extensions', [])
    allowed_exts = {ext.lstrip('.').lower() for ext in allowed_exts_config}

    if filepath.suffix.lstrip('.').lower() not in allowed_exts:
        log_system("WARNING", "Skipped: Invalid extension", file=filepath.name, extension=filepath.suffix)
        return False, f"Invalid extension: {filepath.suffix}", None

    file_hash = calculate_file_hash(filepath)
    try:
        pending_review = _pending_review_match(file_hash, ctx=ctx)
    except TypeError:
        pending_review = _pending_review_match(file_hash)
    if pending_review and not allow_pending_review:
        log_system(
            "INFO",
            "Skipped: Already pending review",
            hash=file_hash,
            file=filepath.name,
            review_file=pending_review.get("filename") or "",
        )
        return False, f"Already pending review: {file_hash[:8]}...", None

    conn = connect_database(ctx=ctx)
    if check_duplicate_hash(conn, file_hash):
        conn.close()
        log_system("INFO", "Skipped: Duplicate hash", hash=file_hash, file=filepath.name)
        return False, f"Duplicate ignored: {file_hash[:8]}...", None


    phash = None
    audio_hash = None
    visual_embedding = None
    tiles = []
    width = None
    height = None

    if mime_type.startswith('image/'):
        phash = calculate_phash(filepath)
        tiles = calculate_tiles(filepath)
        frames = 1
        try:
            from PIL import Image as PILImage
            with PILImage.open(filepath) as img:
                width, height = img.size
                frames = getattr(img, "n_frames", 1)
        except Exception:
            pass

        if phash and not skip_similarity:
            conflict_hash, match_type, total_conflicts, distance, all_matches = find_visual_duplicate(phash, threshold=IMAGE_REVIEW_PHASH_THRESHOLD, new_tiles=tiles, ctx=ctx, return_all=True)
            if conflict_hash:

                conn.close()

                _move_to_review(
                    filepath,
                    file_hash,
                    metadata,
                    {
                    "phash": phash,
                    "match_type": match_type,
                    "best_match": conflict_hash,
                    "distance": distance,
                    "total_conflicts": total_conflicts,
                    "is_tiled": bool(tiles),
                    "matches": all_matches,
                    "width": width,
                    "height": height,
                    "size_bytes": filepath.stat().st_size,
                    "frames": frames,
                    },
                    ctx=ctx,
                )

                log_system("WARNING", f"Quarantined: Visual match detected ({match_type})", file=filepath.name, match_type=match_type, conflicts=total_conflicts)
                return False, f"asi   Visual Match ({match_type}, Total: {total_conflicts}) -> Moved to review/", None

    elif mime_type.startswith('video/'):

        audio_hash = get_audio_fingerprint(filepath)
        visual_embedding = get_visual_embedding(filepath)
        codec_name = ""
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=codec_name,width,height', '-of', 'csv=p=0',
                 str(filepath)], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(',')
                if len(parts) >= 3:
                    codec_name = parts[0].strip()
                    width = int(parts[1].strip())
                    height = int(parts[2].strip())
                elif len(parts) == 2:
                    width = int(parts[0].strip())
                    height = int(parts[1].strip())
        except Exception:
            pass

        duration = 0.0
        try:
            duration = get_video_duration(filepath)
        except Exception:
            pass

        if (audio_hash or visual_embedding) and not skip_similarity:
            conflict_hash, match_type, total_conflicts, similarity, all_matches = find_video_duplicate(audio_hash, visual_embedding, ai_threshold=0.08, ctx=ctx, return_all=True)
            if conflict_hash:

                conn.close()

                _move_to_review(
                    filepath,
                    file_hash,
                    metadata,
                    {
                    "match_type": match_type,
                    "best_match": conflict_hash,
                    "similarity": round(float(similarity), 4) if similarity else 0.0,
                    "total_conflicts": total_conflicts,
                    "audio_present": bool(audio_hash),
                    "visual_embedding_present": bool(visual_embedding),
                    "matches": all_matches,
                    "width": width,
                    "height": height,
                    "size_bytes": filepath.stat().st_size,
                    "codec": codec_name,
                    "duration": duration,
                    },
                    ctx=ctx,
                )

                log_system("WARNING", f"Quarantined: Video duplicate detected ({match_type})", file=filepath.name, match_type=match_type, conflicts=total_conflicts)
                return False, f"asi   Video Duplicate ({match_type}, Total: {total_conflicts}) -> Moved to review/", None

    orig_ext = filepath.suffix.lower()
    target_ext = orig_ext
    if orig_ext in ['.jfif', '.jpeg']:
        target_ext = '.jpg'

    master_timestamp = utc_now()
    master_timestamp_str = master_timestamp.strftime('%Y-%m-%d %H:%M:%S')

    vault_path = None
    md_path = None

    try:
        storage_id = allocate_storage_id(conn)
        shard_folder = storage_shard_for_hash(file_hash)
        new_filename = f"{storage_id}{target_ext}"

        shard_path = _ctx(ctx).active_vault.assets_dir / shard_folder
        shard_path.mkdir(parents=True, exist_ok=True)

        vault_path = storage_asset_path_for(file_hash, storage_id, target_ext, mime_type, ctx=ctx)
        asset_rel_path = f"../../assets/{shard_folder}/{new_filename}"

        file_size = filepath.stat().st_size

        shutil.copy2(filepath, vault_path)

        storage_id = insert_to_database(conn, filepath, file_hash, mime_type, target_ext, metadata, file_size=file_size, timestamp=master_timestamp, phash=phash, audio_hash=audio_hash, visual_embedding=visual_embedding, width=width, height=height, storage_id=storage_id)

        if tiles:
            insert_tiles(conn, file_hash, tiles)


        title = metadata.get('title', '')

        md_content = generate_markdown(conn, file_hash, asset_rel_path, title=title)
        if md_content:
            md_path = note_path_for(file_hash, storage_id=storage_id, ctx=ctx)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(md_path, md_content)


        source_url = metadata.get('source_url', '')
        index_data = {
            "file_hash": file_hash,
            "phash": phash,
            "url": source_url,
            "tiles": tiles,
            "audio_hash": audio_hash,
            "visual_embedding": visual_embedding,
            "tagging_status": "not_run",
            "tagging_error": "",
            "tagging_tag_count": 0,
        }

        try:
            tag_result = tag_media(vault_path, item_hash=file_hash, config=config, storage_id=storage_id, ctx=ctx)
            index_data["tagging_status"] = tag_result.status
            index_data["tagging_error"] = tag_result.error or ""
            index_data["tagging_tag_count"] = len(tag_result.tags or [])
            if tag_result.status != "ok":
                log_system("WARNING", "Tagging enrichment did not complete", hash=file_hash, status=tag_result.status, error=tag_result.error)
                if config.get('tagging', {}).get('fail_ingestion_on_error', False):
                    raise RuntimeError(tag_result.error or f"tagging ended with status {tag_result.status}")
            else:
                md_content = generate_markdown(conn, file_hash, asset_rel_path, title=title, force_wd_from_cache=True)
                if md_content:
                    md_path = note_path_for(file_hash, storage_id=storage_id, ctx=ctx)
                    md_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(md_path, md_content)
        except Exception as tag_exc:
            index_data["tagging_status"] = "error"
            index_data["tagging_error"] = str(tag_exc)
            log_system("WARNING", "Tagging enrichment crashed", hash=file_hash, error=str(tag_exc))
            if config.get('tagging', {}).get('fail_ingestion_on_error', False):
                raise

        safe_reindex_item_metadata(conn, file_hash, "ingest")

        mime = str(mime_type or "").lower()
        is_media = mime.startswith("image/") or mime.startswith("video/")
        if not is_media:
            conn.execute(
                "UPDATE items SET thumbnail_status = 'skipped', thumbnail_error = NULL WHERE hash = ?",
                (file_hash,)
            )
        conn.commit()

        if is_media:
            try:
                ensure_thumbnail(file_hash, target_ext, mime_type, wait=True, storage_id=storage_id, ctx=ctx)
                conn.execute(
                    "UPDATE items SET thumbnail_status = 'ready', thumbnail_error = NULL WHERE hash = ?",
                    (file_hash,)
                )
                conn.commit()
            except Exception as thumb_exc:
                log_system("WARNING", "Ingest thumbnail pregeneration failed", hash=file_hash, error=str(thumb_exc), exc_info=True)
                try:
                    conn.execute(
                        "UPDATE items SET thumbnail_status = 'failed', thumbnail_error = ? WHERE hash = ?",
                        (str(thumb_exc), file_hash)
                    )
                    conn.commit()
                except Exception as db_exc:
                    log_system("ERROR", "Failed to update thumbnail error status in DB", hash=file_hash, error=str(db_exc))
        if sync_index:
            search_index_data = {
                "file_hash": index_data["file_hash"],
                "phash": index_data["phash"],
                "url": index_data["url"],
                "tiles": index_data["tiles"],
                "audio_hash": index_data["audio_hash"],
                "visual_embedding": index_data["visual_embedding"],
            }
            try:
                search_manager.update_indexes(**search_index_data, ctx=ctx)
            except TypeError:
                search_manager.update_indexes(**search_index_data)

        if delete_source:
            cleanup_error = None
            for _ in range(5):
                try:
                    filepath.unlink()
                    cleanup_error = None
                    break
                except OSError as exc:
                    cleanup_error = exc
                    time.sleep(0.2)
            if cleanup_error is not None:
                # Ingest has already committed at this point; treat source cleanup as non-fatal.
                log_system("WARNING", "Ingest succeeded but source cleanup failed", file=str(filepath), error=str(cleanup_error))

        artist = metadata.get('artist', 'Local')
        platform = metadata.get('platform', 'Local')
        ingest_type = metadata.get('ingest_type') or ("local" if metadata.get("staged_from") == "local" else "online" if source_url else "local")
        run_id = metadata.get('run_id', '')

        log_activity(
            original_name=filepath.name,
            vault_id=file_hash,
            platform=platform,
            artist=artist,
            source_url=source_url,
            timestamp_str=master_timestamp_str,
            ingest_type=ingest_type,
            run_id=run_id,
        )

        return True, f"Success: {filepath.name} -> {new_filename}", index_data

    except Exception as e:

        try:
            conn.rollback()
        except Exception:
            pass

        try:
            if vault_path and vault_path.exists():
                vault_path.unlink()
            if md_path and md_path.exists():
                md_path.unlink()
        except Exception:
            pass

        log_system("ERROR", "Pipeline crash during processing", file=filepath.name, error=str(e))
        return False, f"System Error (Rolled back): {str(e)}", None

    finally:
        if 'conn' in locals():
            try:
                conn.close()
            except Exception:
                pass
