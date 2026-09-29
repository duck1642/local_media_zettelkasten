import asyncio
import base64
import json
import sys
import time
import traceback
from pathlib import Path

from artists import (
    add_artist_alias,
    add_artist_link,
    delete_artist_alias,
    delete_artist_link,
    get_artist_detail,
    list_artists,
    merge_artists,
    normalize_artist_name,
    preview_artist_merge,
    resolve_artist_name,
    update_artist,
)
from db.search_manager import search_manager
from db.sqlite_operator import connect_database, init_database, normalize_source_url
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from logger import log_review, log_system
from md_generator import (
    generate_markdown,
    load_note_topics,
    load_note_wd_tags,
    normalize_topic_list,
)
from media_lifecycle import storage_lifecycle_lock
from metadata_index import (
    indexed_item_metadata,
    item_core_facet_values,
    item_facet_values,
    metadata_facets,
    metadata_index_ready,
    refresh_metadata_facet_counts_for_values,
    refresh_metadata_index_counters,
    safe_reindex_item_metadata,
    start_metadata_repair_worker,
)
from metadata_maintenance import (
    delete_topic_across_workspace,
    delete_wd_tag_across_workspace,
    merge_topic_across_workspace,
    rename_topic_across_workspace,
    rename_wd_tag_across_workspace,
    rewrite_metadata_notes_for_hashes,
)
from platforms import list_platforms, resolve_platform_label
from pydantic import BaseModel
from runtime_context import WorkspaceContext
from tagging import load_tag_cache, tag_media
from thumbnails import ThumbnailBusyError, get_or_generate_thumbnail
from topics import ensure_topic_file, slugify_topic_label
from utils import asset_path_for, asset_url_for, atomic_write_text, get_app_settings, note_path_for
from workspace_db import connect_workspace_database, upsert_wd_dictionary_tags

from api.common import _item_file_paths, _open_path_external, _review_dir, _topics_dir
from api.guards import require_usable_vault_context

router = APIRouter(dependencies=[Depends(require_usable_vault_context)])

@router.get("/api/stats")
async def get_stats():
    return await asyncio.to_thread(_get_stats_sync)

class ArtistUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    notes: str | None = None

class ArtistAliasCreate(BaseModel):
    alias: str

class ArtistLinkCreate(BaseModel):
    platform: str
    url: str
    handle: str | None = None
    is_primary: bool = False

class ArtistMergeRequest(BaseModel):
    source_artist_ids: list[int]

class TopicRenameRequest(BaseModel):
    old_label: str
    new_label: str

class TopicCreateRequest(BaseModel):
    label: str

class TopicDeleteRequest(BaseModel):
    label: str

class TopicMergeRequest(BaseModel):
    source_label: str
    target_label: str

class WdTagRenameRequest(BaseModel):
    old_tag: str
    new_tag: str
    tag_type: str | None = None

class WdTagDeleteRequest(BaseModel):
    tag: str
    tag_type: str | None = None

@router.get("/api/platforms")
async def get_platforms(q: str = "", limit: int = 100, scope: str = "all"):
    return await asyncio.to_thread(_get_platforms_sync, q, limit, scope)

def _get_platforms_sync(q: str = "", limit: int = 100, scope: str = "all"):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        items = list_platforms(workspace_conn, q, limit, used_only=str(scope or "").casefold() == "used", item_conn=conn)
        workspace_conn.commit()
        return {"items": items}
    finally:
        conn.close()
        workspace_conn.close()

@router.get("/api/artists")
async def get_artists(q: str = "", limit: int = 100, scope: str = "all"):
    return await asyncio.to_thread(_get_artists_sync, q, limit, scope)

def _get_artists_sync(q: str = "", limit: int = 100, scope: str = "all"):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        items = list_artists(workspace_conn, q, limit, used_only=str(scope or "").casefold() == "used", item_conn=conn)
        workspace_conn.commit()
        return {"items": items}
    finally:
        conn.close()
        workspace_conn.close()

@router.get("/api/artists/{artist_id}")
async def get_artist(artist_id: int):
    return await asyncio.to_thread(_get_artist_sync, artist_id)

def _get_artist_sync(artist_id: int):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        detail = get_artist_detail(workspace_conn, artist_id, item_conn=conn)
        if detail is None:
            raise HTTPException(status_code=404, detail="Artist not found")
        return detail
    finally:
        conn.close()
        workspace_conn.close()

def _rewrite_metadata_notes_for_hashes(conn, item_hashes: list[str]):
    return rewrite_metadata_notes_for_hashes(conn, item_hashes)

def _public_artist_merge_payload(payload: dict) -> dict:
    public = dict(payload)
    public.pop("source_norms", None)
    public.pop("facet_norms", None)
    public.pop("item_hashes", None)
    return public

@router.patch("/api/artists/{artist_id}")
async def patch_artist(artist_id: int, update: ArtistUpdate):
    return await asyncio.to_thread(_patch_artist_sync, artist_id, update)

def _patch_artist_sync(artist_id: int, update: ArtistUpdate):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        try:
            previous = get_artist_detail(workspace_conn, artist_id, item_conn=conn)
            if previous is None:
                raise KeyError("artist not found")
            previous_norm = normalize_artist_name(previous["name"])
            detail = update_artist(workspace_conn, artist_id, update.name, update.kind, update.notes, item_conn=conn)
            current_norm = normalize_artist_name(detail["name"])
            if update.name is not None and previous_norm and current_norm and previous_norm != current_norm:
                rows = conn.execute(
                    "SELECT hash FROM items WHERE LOWER(TRIM(source_artist)) = ?",
                    (previous_norm,),
                ).fetchall()
                item_hashes = [row[0] for row in rows]
                if item_hashes:
                    conn.executemany(
                        "UPDATE items SET source_artist = ? WHERE hash = ?",
                        [(detail["name"], item_hash) for item_hash in item_hashes],
                    )
                    _rewrite_metadata_notes_for_hashes(conn, item_hashes)
                    refresh_metadata_facet_counts_for_values(conn, {("artist", previous_norm), ("artist", current_norm)})
                    detail = get_artist_detail(workspace_conn, artist_id, item_conn=conn) or detail
            workspace_conn.commit()
            conn.commit()
            return detail
        except KeyError:
            raise HTTPException(status_code=404, detail="Artist not found")
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()
        workspace_conn.close()

@router.post("/api/artists/{artist_id}/merge-preview")
async def preview_artist_merge_route(artist_id: int, body: ArtistMergeRequest):
    return await asyncio.to_thread(_preview_artist_merge_sync, artist_id, body)

def _preview_artist_merge_sync(artist_id: int, body: ArtistMergeRequest):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        try:
            return _public_artist_merge_payload(preview_artist_merge(workspace_conn, artist_id, body.source_artist_ids, item_conn=conn))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()
        workspace_conn.close()

@router.post("/api/artists/{artist_id}/merge")
async def merge_artist_route(artist_id: int, body: ArtistMergeRequest):
    return await asyncio.to_thread(_merge_artist_sync, artist_id, body)

def _merge_artist_sync(artist_id: int, body: ArtistMergeRequest):
    conn = connect_database()
    workspace_conn = connect_workspace_database()
    try:
        try:
            result = merge_artists(workspace_conn, artist_id, body.source_artist_ids, item_conn=conn)
            if result.get("item_hashes"):
                _rewrite_metadata_notes_for_hashes(conn, result["item_hashes"])
                refresh_metadata_facet_counts_for_values(
                    conn,
                    {("artist", norm) for norm in result.get("facet_norms", [])},
                )
                result["target_detail"] = get_artist_detail(workspace_conn, artist_id, item_conn=conn)
            workspace_conn.commit()
            conn.commit()
            return _public_artist_merge_payload(result)
        except KeyError as exc:
            workspace_conn.rollback()
            conn.rollback()
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            workspace_conn.rollback()
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            workspace_conn.rollback()
            conn.rollback()
            raise
    finally:
        conn.close()
        workspace_conn.close()

@router.post("/api/artists/{artist_id}/aliases")
async def post_artist_alias(artist_id: int, body: ArtistAliasCreate):
    return await asyncio.to_thread(_post_artist_alias_sync, artist_id, body)

def _post_artist_alias_sync(artist_id: int, body: ArtistAliasCreate):
    conn = connect_workspace_database()
    try:
        try:
            alias = add_artist_alias(conn, artist_id, body.alias)
            conn.commit()
            return alias
        except KeyError:
            raise HTTPException(status_code=404, detail="Artist not found")
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()

@router.delete("/api/artists/{artist_id}/aliases/{alias_id}")
async def delete_alias(artist_id: int, alias_id: int):
    return await asyncio.to_thread(_delete_alias_sync, artist_id, alias_id)

def _delete_alias_sync(artist_id: int, alias_id: int):
    conn = connect_workspace_database()
    try:
        if not delete_artist_alias(conn, artist_id, alias_id):
            raise HTTPException(status_code=404, detail="Alias not found")
        conn.commit()
        return {"status": "success"}
    finally:
        conn.close()

@router.post("/api/artists/{artist_id}/links")
async def post_artist_link(artist_id: int, body: ArtistLinkCreate):
    return await asyncio.to_thread(_post_artist_link_sync, artist_id, body)

def _post_artist_link_sync(artist_id: int, body: ArtistLinkCreate):
    conn = connect_workspace_database()
    try:
        try:
            link = add_artist_link(conn, artist_id, body.platform, body.url, body.handle, body.is_primary)
            conn.commit()
            return link
        except KeyError:
            raise HTTPException(status_code=404, detail="Artist not found")
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()

@router.delete("/api/artists/{artist_id}/links/{link_id}")
async def delete_link(artist_id: int, link_id: int):
    return await asyncio.to_thread(_delete_link_sync, artist_id, link_id)

def _delete_link_sync(artist_id: int, link_id: int):
    conn = connect_workspace_database()
    try:
        if not delete_artist_link(conn, artist_id, link_id):
            raise HTTPException(status_code=404, detail="Link not found")
        conn.commit()
        return {"status": "success"}
    finally:
        conn.close()


def _get_stats_sync():
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM items")
        count = cursor.fetchone()[0]
        return {"total_items": count}
    finally:
        conn.close()

@router.get("/api/search/suggestions")
async def get_search_suggestions(kind: str, q: str = "", limit: int = 20):
    return await asyncio.to_thread(_get_search_suggestions_sync, kind, q, limit)

@router.get("/api/facets")
async def get_facets(kind: str, q: str = "", limit: int = 100, scope: str = "used"):
    return await asyncio.to_thread(_get_facets_sync, kind, q, limit, scope)

def _get_search_suggestions_sync(kind: str, q: str = "", limit: int = 20):
    kind = (kind or "").strip().lower()
    needle = (q or "").strip().lower()
    limit = max(1, min(int(limit or 20), 50))

    if kind == "command":
        commands = [
            "/masonry",
            "/grid",
            "/zoom-in",
            "/zoom-out",
            ">toggle-inspector",
            "/ram-track",
            "/scan-auth",
            "/cleanup-review",
            "/sort-newest",
            "/sort-oldest",
            "/sort-artist",
            "/media-all",
            "/media-image",
            "/media-video",
        ]
        items = [
            {"value": cmd, "count": 0}
            for cmd in commands
            if cmd.lower().startswith(f">{needle}") or cmd.lower().lstrip(">").startswith(needle)
        ][:limit]
        return {"suggestions": [item["value"] for item in items], "items": items}

    if kind not in {"artist", "platform", "topic", "wd_tag"}:
        raise HTTPException(status_code=400, detail="Invalid suggestion kind")

    result = _get_facets_sync(kind, q, limit)
    return {"suggestions": [item["value"] for item in result["items"]], "items": result["items"]}

def _sort_facets(items, needle, limit):
    needle = needle.lower()
    filtered = [
        item for item in items
        if not needle or needle in item["value"].lower()
    ]
    filtered.sort(
        key=lambda item: (
            0 if needle and item["value"].lower().startswith(needle) else 1,
            -item["count"],
            item["value"].lower()
        )
    )
    return filtered[:limit]

def _topic_library_facets(conn, needle: str, limit: int) -> list[dict]:
    used = {
        str(item["value"]).casefold(): {"value": item["value"], "count": int(item["count"] or 0)}
        for item in metadata_facets(conn, "topic", needle.casefold(), 10000)
    }
    merged = dict(used)
    topics_dir = _topics_dir()
    if topics_dir.exists():
        for path in topics_dir.glob("*.md"):
            label = path.stem
            key = label.casefold()
            if needle and needle not in key:
                continue
            merged.setdefault(key, {"value": label, "count": 0})
    items = list(merged.values())
    items.sort(
        key=lambda item: (
            0 if needle and str(item["value"]).casefold().startswith(needle) else 1,
            -int(item["count"] or 0),
            str(item["value"]).casefold(),
        )
    )
    return items[:limit]

def _wd_dictionary_facets(workspace_conn, item_conn, needle: str, limit: int) -> list[dict]:
    used = {
        str(item["value"]).casefold(): {
            "value": item["value"],
            "count": int(item["count"] or 0),
            "tag_type": item.get("tag_type") or "general",
        }
        for item in metadata_facets(item_conn, "wd_tag", needle.casefold(), 10000)
    }
    merged = dict(used)
    where_sql = ""
    params: list = []
    if needle:
        where_sql = "WHERE tag_norm LIKE ? OR LOWER(tag) LIKE ?"
        params.extend([f"%{needle}%", f"%{needle}%"])
    for tag_norm, tag, tag_type in workspace_conn.execute(
        f"""
        SELECT tag_norm, tag, tag_type
        FROM wd_tag_dictionary
        {where_sql}
        ORDER BY tag COLLATE NOCASE ASC
        LIMIT 10000
        """,
        params,
    ).fetchall():
        merged.setdefault(str(tag_norm), {"value": tag, "count": 0, "tag_type": tag_type or "general"})
    items = list(merged.values())
    items.sort(
        key=lambda item: (
            0 if needle and str(item["value"]).casefold().startswith(needle) else 1,
            -int(item["count"] or 0),
            str(item["value"]).casefold(),
        )
    )
    return items[:limit]

def _get_facets_sync(kind: str, q: str = "", limit: int = 100, scope: str = "used"):
    kind = (kind or "").strip().lower()
    needle = (q or "").strip().lower()
    scope = (scope or "used").strip().lower()
    limit = max(1, min(int(limit or 100), 500))

    if kind not in {"artist", "platform", "topic", "wd_tag"}:
        raise HTTPException(status_code=400, detail="Invalid facet kind")

    conn = connect_database()
    try:
        if kind == "topic" and scope == "all":
            return {"kind": kind, "items": _topic_library_facets(conn, needle.casefold(), limit)}

        if kind == "wd_tag" and scope == "all":
            workspace_conn = connect_workspace_database()
            try:
                return {"kind": kind, "items": _wd_dictionary_facets(workspace_conn, conn, needle.casefold(), limit)}
            finally:
                workspace_conn.close()

        if kind == "artist":
            if scope == "all":
                workspace_conn = connect_workspace_database()
                try:
                    artists = list_artists(workspace_conn, needle, limit, used_only=False, item_conn=conn)
                    return {"kind": kind, "items": [{"value": a["name"], "count": a["item_count"]} for a in artists]}
                finally:
                    workspace_conn.close()
            return {"kind": kind, "items": metadata_facets(conn, kind, needle.casefold(), limit)}

        if kind == "platform":
            if scope == "all":
                workspace_conn = connect_workspace_database()
                try:
                    platforms = list_platforms(workspace_conn, needle, limit, used_only=False, item_conn=conn)
                    return {"kind": kind, "items": [{"value": p["display_name"], "count": p["item_count"]} for p in platforms]}
                finally:
                    workspace_conn.close()
            return {"kind": kind, "items": metadata_facets(conn, kind, needle.casefold(), limit)}

        if not metadata_index_ready(conn):
            start_metadata_repair_worker(full=False)
            log_system("WARNING", "Metadata index not ready; starting background repair", kind=kind)

        return {"kind": kind, "items": metadata_facets(conn, kind, needle.casefold(), limit)}
    finally:
        conn.close()

@router.post("/api/topics/rename")
async def rename_topic_route(body: TopicRenameRequest):
    return await asyncio.to_thread(_rename_topic_sync, body.old_label, body.new_label)

@router.post("/api/topics")
async def create_topic_route(body: TopicCreateRequest):
    return await asyncio.to_thread(_create_topic_sync, body.label)

def _create_topic_sync(label: str):
    clean = str(label or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="topic label is required")
    path = ensure_topic_file(clean)
    return {
        "status": "success",
        "label": path.stem,
        "slug": slugify_topic_label(clean),
        "path": str(path),
    }

def _rename_topic_sync(old_label: str, new_label: str):
    try:
        return rename_topic_across_workspace(old_label, new_label)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/topics/delete")
async def delete_topic_route(body: TopicDeleteRequest):
    return await asyncio.to_thread(_delete_topic_sync, body.label)

def _delete_topic_sync(label: str):
    try:
        return delete_topic_across_workspace(label)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/topics/merge")
async def merge_topic_route(body: TopicMergeRequest):
    return await asyncio.to_thread(_merge_topic_sync, body.source_label, body.target_label)

def _merge_topic_sync(source_label: str, target_label: str):
    try:
        return merge_topic_across_workspace(source_label, target_label)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/wd-tags/rename")
async def rename_wd_tag_route(body: WdTagRenameRequest):
    return await asyncio.to_thread(_rename_wd_tag_sync, body.old_tag, body.new_tag, body.tag_type)

def _rename_wd_tag_sync(old_tag: str, new_tag: str, tag_type: str | None = None):
    try:
        return rename_wd_tag_across_workspace(old_tag, new_tag, tag_type=tag_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/wd-tags/delete")
async def delete_wd_tag_route(body: WdTagDeleteRequest):
    return await asyncio.to_thread(_delete_wd_tag_sync, body.tag, body.tag_type)

def _delete_wd_tag_sync(tag: str, tag_type: str | None = None):
    try:
        return delete_wd_tag_across_workspace(tag, tag_type=tag_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.get("/api/thumbnails/{item_hash}")
async def get_thumbnail(item_hash: str):
    return await asyncio.to_thread(_get_thumbnail_sync, item_hash)

def _get_thumbnail_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT file_extension, mime_type, storage_id, thumbnail_status FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row: raise HTTPException(status_code=404)
        ext, mime, storage_id, thumb_status = row
        is_image = str(mime or "").startswith("image/")
        if thumb_status == 'failed':
            if is_image:
                return RedirectResponse(url=asset_url_for(item_hash, ext, mime, storage_id=storage_id), status_code=302)
            from fastapi import Response
            return Response(
                content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"><rect width="100%" height="100%" fill="#333"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#666" font-family="sans-serif" font-size="12">FAILED</text></svg>',
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=31536000, immutable"}
            )
        try:
            thumb_path = get_or_generate_thumbnail(item_hash, ext, mime, storage_id=storage_id)
        except ThumbnailBusyError:
            raise HTTPException(status_code=503, detail="Thumbnail generation busy")
        if not thumb_path:
            try:
                cursor.execute(
                    "UPDATE items SET thumbnail_status = 'failed', thumbnail_error = 'get_or_generate_thumbnail returned None' WHERE hash = ?",
                    (item_hash,)
                )
                conn.commit()
            except Exception:
                pass
            if is_image:
                return RedirectResponse(url=asset_url_for(item_hash, ext, mime, storage_id=storage_id), status_code=302)
            from fastapi import Response
            return Response(
                content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"><rect width="100%" height="100%" fill="#333"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#666" font-family="sans-serif" font-size="12">FAILED</text></svg>',
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=31536000, immutable"}
            )
        return FileResponse(
            thumb_path, media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )
    finally:
        conn.close()

@router.get("/api/items")
async def get_items(
    field: str = None, value: str = None,
    sort: str = 'newest', media_type: str = 'all',
    artist: list[str] = Query(default=[]), platform: list[str] = Query(default=[]),
    filename: list[str] = Query(default=[]), topic: list[str] = Query(default=[]),
    wd_tag: list[str] = Query(default=[]), text: list[str] = Query(default=[]),
    cursor: str = None, limit: int = 50
):
    return await asyncio.to_thread(
        _get_items_sync,
        field, value, sort, media_type, artist, platform, filename, topic, wd_tag, text, cursor, limit
    )

def _encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "v2:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _decode_cursor(cursor: str | None) -> dict:
    if not cursor:
        return {}
    if cursor.startswith("v2:"):
        try:
            raw = cursor[3:]
            raw += "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
    try:
        cursor_date, cursor_hash = cursor.rsplit("_", 1)
    except ValueError:
        cursor_date, cursor_hash = cursor, ""
    return {"date": cursor_date, "hash": cursor_hash}

def _cursor_for_item(item: dict, sort: str) -> str:
    payload = {
        "sort": sort,
        "date": str(item.get("date_added") or ""),
        "hash": str(item.get("hash") or ""),
    }
    if sort == "artist":
        payload["artist"] = str(item.get("artist") or "")
    return _encode_cursor(payload)

def _clean_filter_values(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    cleaned = []
    for value in values:
        text = str(value or "").strip()
        if text:
            cleaned.append(text)
    return cleaned

def _append_or_like(conditions, params, column, values):
    values = _clean_filter_values(values)
    if not values:
        return
    conditions.append("(" + " OR ".join([f"{column} LIKE ?"] * len(values)) + ")")
    params.extend([f"%{value}%" for value in values])

def _core_norm_expr(column: str) -> str:
    return f"LOWER(TRIM({column}))"

def _core_filter_has_exact(conn, column: str, value: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM items WHERE {_core_norm_expr(column)} = ? LIMIT 1",
        (value.casefold(),),
    ).fetchone()
    return row is not None

def _append_core_filter(conn, conditions, params, column, values):
    values = _clean_filter_values(values)
    if not values:
        return
    clauses = []
    for value in values:
        norm_value = value.casefold()
        if _core_filter_has_exact(conn, column, norm_value):
            clauses.append(f"{_core_norm_expr(column)} = ?")
            params.append(norm_value)
        else:
            clauses.append(f"{_core_norm_expr(column)} LIKE ?")
            params.append(f"%{norm_value}%")
    conditions.append("(" + " OR ".join(clauses) + ")")

def _append_text_terms(conditions, params, terms):
    for term in _clean_filter_values(terms):
        conditions.append("(original_filename LIKE ? OR hash LIKE ? OR source_url LIKE ? OR source_artist LIKE ? OR platform LIKE ?)")
        params.extend([f"%{term}%"] * 5)

def _metadata_filter_has_exact(conn, table: str, norm_column: str, value: str) -> bool:
    exact_row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {norm_column} = ? LIMIT 1",
        (value,),
    ).fetchone()
    return exact_row is not None

def _append_metadata_filter(conn, conditions, params, table: str, alias: str, norm_column: str, value: str):
    norm_value = value.casefold()
    if _metadata_filter_has_exact(conn, table, norm_column, norm_value):
        conditions.append(f"hash IN (SELECT item_hash FROM {table} WHERE {norm_column} = ?)")
        params.append(norm_value)
        return
    conditions.append(
        f"EXISTS (SELECT 1 FROM {table} {alias} WHERE {alias}.item_hash = items.hash AND {alias}.{norm_column} LIKE ?)"
    )
    params.append(f"%{norm_value}%")

def _get_items_sync(field, value, sort, media_type, artist, platform, filename, topic, wd_tag, text, cursor, limit):
    limit = max(1, min(limit, 100))
    topic_filters = _clean_filter_values(topic)
    wd_tag_filters = _clean_filter_values(wd_tag)
    conn = connect_database()
    cursor_obj = conn.cursor()
    try:
        base_query = "SELECT hash, file_extension, mime_type, original_filename, source_url, date_added, platform, source_artist, width, height, storage_id FROM items"
        conditions = []
        params = []
        use_metadata_index = bool(topic_filters or wd_tag_filters) and metadata_index_ready(conn)

        if field and value:
            allowed = {"source_artist", "platform", "original_filename"}
            if field in allowed:
                if field in {"source_artist", "platform"}:
                    _append_core_filter(conn, conditions, params, field, [value])
                else:
                    conditions.append(f"{field} LIKE ?")
                    params.append(f"%{value}%")

        _append_core_filter(conn, conditions, params, "source_artist", artist)
        _append_core_filter(conn, conditions, params, "platform", platform)
        _append_or_like(conditions, params, "original_filename", filename)
        _append_text_terms(conditions, params, text)

        if media_type == 'image':
            conditions.append("mime_type LIKE 'image/%'")
        elif media_type == 'video':
            conditions.append("mime_type LIKE 'video/%'")

        if use_metadata_index:
            for topic_value in topic_filters:
                _append_metadata_filter(conn, conditions, params, "item_topics", "mt", "topic_norm", topic_value)
            for wd_value in wd_tag_filters:
                _append_metadata_filter(conn, conditions, params, "item_wd_tags", "mw", "tag_norm", wd_value)

        has_frontmatter_filter = bool(topic_filters or wd_tag_filters)
        if has_frontmatter_filter and not use_metadata_index:
            start_metadata_repair_worker(full=False)
            log_system("WARNING", "Metadata index not ready; skipping topic/WD filter scan and starting repair")
            return {"items": [], "has_more": False, "next_cursor": None}

        if cursor:
            cursor_payload = _decode_cursor(cursor)
            cursor_date = str(cursor_payload.get("date") or "")
            cursor_hash = str(cursor_payload.get("hash") or "")
            if sort == 'artist':
                cursor_artist = str(cursor_payload.get("artist") or "")
                conditions.append(
                    "("
                    "COALESCE(source_artist, '') COLLATE NOCASE > ? COLLATE NOCASE "
                    "OR (COALESCE(source_artist, '') COLLATE NOCASE = ? COLLATE NOCASE "
                    "AND (date_added < ? OR (date_added = ? AND hash < ?)))"
                    ")"
                )
                params.extend([cursor_artist, cursor_artist, cursor_date, cursor_date, cursor_hash])
            elif sort == 'oldest':
                conditions.append("(date_added > ? OR (date_added = ? AND hash > ?))")
                params.extend([cursor_date, cursor_date, cursor_hash])
            else:
                conditions.append("(date_added < ? OR (date_added = ? AND hash < ?))")
                params.extend([cursor_date, cursor_date, cursor_hash])

        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        order_clause = " ORDER BY date_added DESC, hash DESC"
        if sort == 'oldest': order_clause = " ORDER BY date_added ASC, hash ASC"
        elif sort == 'artist': order_clause = " ORDER BY COALESCE(source_artist, '') COLLATE NOCASE ASC, date_added DESC, hash DESC"

        cursor_obj.execute(f"{base_query}{where_clause}{order_clause} LIMIT {limit + 1}", tuple(params))
        rows = cursor_obj.fetchall()

        items = []

        for row in rows:
            h, ext = row[0], (row[1] or "")

            items.append({
                "hash": h, "extension": ext, "mime_type": row[2],
                "original_filename": row[3], "source_url": row[4],
                "date_added": row[5], "platform": row[6], "artist": row[7],
                "url": asset_url_for(h, ext, row[2], storage_id=row[10]),
                "thumbnail_url": f"/api/thumbnails/{h}",
                "width": row[8], "height": row[9]
            })

        has_more = len(items) > limit
        if has_more:
            items = items[:limit]

        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _cursor_for_item(last, sort)

        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
    finally:
        conn.close()

def _get_item_details(h, row, conn=None):
    ext = row[1] or ""
    storage_id = row[10] if len(row) > 10 else None
    try:
        if conn is not None:
            metadata = indexed_item_metadata(conn, h)
            topics = metadata.get("topics", [])
            wd_data = metadata.get("wd_data", {"status": "missing"})
        else:
            raise RuntimeError("metadata index connection unavailable")
    except Exception as exc:
        log_system("WARNING", "Metadata index detail fallback", hash=h, error=str(exc))
        if not storage_id:
            raise RuntimeError(f"item {h} is missing storage_id")
        topics = load_note_topics(h, storage_id)
        wd_data = load_note_wd_tags(h, storage_id)
        if wd_data.get("status") != "ok":
            cache_data = load_tag_cache(h, storage_id)
            if cache_data.get("status") == "ok":
                wd_data = {
                    "status": "ok",
                    "source": "cache",
                    "rating": cache_data.get("rating") or {},
                    "character_tags": cache_data.get("character_tags") or [],
                    "tags": cache_data.get("tags") or []
                }
    
    def get_names(tag_list):
        names = []
        for t in tag_list:
            if isinstance(t, str): names.append(t)
            elif isinstance(t, dict): names.append(t.get("display_name") or t.get("name") or "")
        return [n for n in names if n]

    def facet_counts(kind: str, values: list[str]) -> dict[str, int]:
        if conn is None:
            return {}
        cleaned = [(value, str(value or "").strip().casefold()) for value in values if str(value or "").strip()]
        if not cleaned:
            return {}
        counts: dict[str, int] = {}
        for value, value_norm in cleaned:
            row = conn.execute(
                "SELECT count FROM metadata_facet_counts WHERE kind = ? AND value_norm = ?",
                (kind, value_norm),
            ).fetchone()
            if row:
                counts[value] = int(row[0] or 0)
                continue
            if kind == "topic":
                fallback = conn.execute("SELECT COUNT(*) FROM item_topics WHERE topic_norm = ?", (value_norm,)).fetchone()
            elif kind == "wd_tag":
                fallback = conn.execute("SELECT COUNT(DISTINCT item_hash) FROM item_wd_tags WHERE tag_norm = ?", (value_norm,)).fetchone()
            else:
                fallback = None
            if fallback:
                counts[value] = int(fallback[0] or 0)
        return counts

    formatted_wd = {
        "rating": wd_data.get("rating", {}).get("label") or wd_data.get("rating", {}).get("name") or "None",
        "characters": get_names(wd_data.get("character_tags", [])),
        "general": get_names(wd_data.get("tags", []))
    }
    wd_values = [
        formatted_wd["rating"] if formatted_wd["rating"] != "None" else "",
        *formatted_wd["characters"],
        *formatted_wd["general"],
    ]
    
    return {
        "hash": h, "extension": ext, "mime_type": row[2] or "",
        "original_filename": row[3], "source_url": row[4],
        "date_added": row[5], "platform": row[6], "artist": row[7],
        "url": asset_url_for(h, ext, row[2] or "", storage_id=storage_id),
        "thumbnail_url": f"/api/thumbnails/{h}",
        "width": row[8], "height": row[9],
        "topics": topics,
        "topic_counts": facet_counts("topic", topics),
        "wd_tags": formatted_wd,
        "wd_tag_counts": facet_counts("wd_tag", wd_values)
    }

@router.get("/api/items/{item_hash}")
async def get_item(item_hash: str):
    return await asyncio.to_thread(_get_item_sync, item_hash)

def _get_item_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT hash, file_extension, mime_type, original_filename, source_url, date_added, platform, source_artist, width, height, storage_id FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row: raise HTTPException(status_code=404)
        return _get_item_details(item_hash, row, conn)
    finally:
        conn.close()

@router.get("/api/items/{item_hash}/path")
async def get_item_path(item_hash: str):
    return await asyncio.to_thread(_get_item_path_sync, item_hash)

def _get_item_path_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT file_extension, mime_type, storage_id FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row: raise HTTPException(status_code=404)
        path = asset_path_for(item_hash, row[0] or "", row[1] or "", storage_id=row[2])
        return {"absolute_path": str(path.resolve())}
    finally:
        conn.close()

@router.get("/api/items/{item_hash}/note_path")
async def get_item_note_path(item_hash: str):
    return await asyncio.to_thread(_get_item_note_path_sync, item_hash)

def _get_item_note_path_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row: raise HTTPException(status_code=404)
        path = note_path_for(item_hash, row[0])
        return {"absolute_path": str(path.resolve())}
    finally:
        conn.close()

@router.post("/api/items/{item_hash}/open_folder")
async def open_item_folder(item_hash: str):
    return await asyncio.to_thread(_open_item_folder_sync, item_hash)

def _open_item_folder_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT file_extension, mime_type, storage_id FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404)
        path = asset_path_for(item_hash, row[0] or "", row[1] or "", storage_id=row[2])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Asset missing")
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(path.resolve())])
        else:
            _open_path_external(path.parent)
        return {"status": "success"}
    finally:
        conn.close()

@router.post("/api/items/{item_hash}/open_note")
async def open_item_note(item_hash: str):
    return await asyncio.to_thread(_open_item_note_sync, item_hash)

def _open_item_note_sync(item_hash: str):
    conn = connect_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404)
        path = note_path_for(item_hash, row[0])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Note missing")
        _open_path_external(path)
        return {"status": "success"}
    finally:
        conn.close()

class ItemUpdate(BaseModel):
    artist: str = None
    source_url: str = None
    platform: str = None
    topics: list[str] = None
    wd_rating: str = None
    wd_character_tags: list[str] = None
    wd_tags: list[str] = None

class BulkDeleteRequest(BaseModel):
    hashes: list[str]

@router.patch("/api/items/{item_hash}")
async def update_item(item_hash: str, update: ItemUpdate):
    return await asyncio.to_thread(_update_item_sync, item_hash, update)

def _update_item_sync(item_hash: str, update: ItemUpdate):
    conn = init_database()
    workspace_conn = connect_workspace_database()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM items WHERE hash = ?", (item_hash,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404)
        previous_core_facets = item_core_facet_values(conn, item_hash)
        manual_overrides = {}
        if update.artist is not None:
            resolved_artist = resolve_artist_name(workspace_conn, update.artist)
            cursor.execute("UPDATE items SET source_artist = ? WHERE hash = ?", (resolved_artist, item_hash))
            manual_overrides["artist"] = resolved_artist
        if update.source_url is not None:
            cursor.execute("UPDATE items SET source_url = ?, source_url_norm = ? WHERE hash = ?", (update.source_url, normalize_source_url(update.source_url), item_hash))
        if update.platform is not None:
            cursor.execute("UPDATE items SET platform = ? WHERE hash = ?", (resolve_platform_label(workspace_conn, update.platform), item_hash))
        if update.topics is not None:
            if not isinstance(update.topics, list):
                raise HTTPException(status_code=400, detail="topics must be a list")
            manual_overrides["topics"] = normalize_topic_list(update.topics)
        if update.wd_rating is not None:
            manual_overrides["wd_rating"] = str(update.wd_rating or "").strip()
        if update.wd_character_tags is not None:
            if not isinstance(update.wd_character_tags, list):
                raise HTTPException(status_code=400, detail="wd_character_tags must be a list")
            manual_overrides["wd_character_tags"] = normalize_topic_list(update.wd_character_tags)
        if update.wd_tags is not None:
            if not isinstance(update.wd_tags, list):
                raise HTTPException(status_code=400, detail="wd_tags must be a list")
            manual_overrides["wd_tags"] = normalize_topic_list(update.wd_tags)
        wd_dictionary_rows = []
        if "wd_rating" in manual_overrides and manual_overrides["wd_rating"]:
            wd_dictionary_rows.append(("rating", manual_overrides["wd_rating"]))
        for value in manual_overrides.get("wd_character_tags", []) or []:
            wd_dictionary_rows.append(("character", value))
        for value in manual_overrides.get("wd_tags", []) or []:
            wd_dictionary_rows.append(("general", value))
        upsert_wd_dictionary_tags(workspace_conn, wd_dictionary_rows)
        
        md_content = generate_markdown(conn, item_hash, topics_override=update.topics, manual_overrides=manual_overrides)
        if md_content:
            row = cursor.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
            if not row or not row[0]:
                raise RuntimeError(f"item {item_hash} is missing storage_id")
            with storage_lifecycle_lock(row[0]):
                current = cursor.execute("SELECT storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
                if not current or current[0] != row[0]:
                    raise HTTPException(status_code=404, detail="Item was deleted during update")
                note_path = note_path_for(item_hash, row[0])
                note_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(note_path, md_content)
                safe_reindex_item_metadata(conn, item_hash, "item_patch", update_workspace_wd=False)
                current_core_facets = item_core_facet_values(conn, item_hash)
                refresh_metadata_facet_counts_for_values(conn, previous_core_facets | current_core_facets)
                workspace_conn.commit()
                conn.commit()
            
        row = cursor.execute("SELECT hash, file_extension, mime_type, original_filename, source_url, date_added, platform, source_artist, width, height, storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
        return _get_item_details(item_hash, row, conn) if row else {"status": "success"}
    except Exception:
        workspace_conn.rollback()
        conn.rollback()
        raise
    finally:
        conn.close()
        workspace_conn.close()

@router.delete("/api/items/{item_hash}")
async def delete_item(item_hash: str):
    return await asyncio.to_thread(_delete_item_sync, item_hash)

@router.post("/api/items/bulk_delete")
async def bulk_delete_items(request: BulkDeleteRequest):
    return await asyncio.to_thread(_bulk_delete_items_sync, request.hashes)

def _cleanup_trash_dir(name: str, ctx: WorkspaceContext | None = None) -> Path:
    return _review_dir(ctx) / name


def _trash_path_for(trash_dir: Path, item_hash: str, index: int, path: Path) -> Path:
    base = trash_dir / f"{item_hash}_{index}_{path.name}"
    if not base.exists():
        return base
    for suffix in range(1, 1000):
        candidate = trash_dir / f"{item_hash}_{index}_{suffix}_{path.name}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate cleanup trash path for {path}")


def _retry_file_move(src: Path, dst: Path, attempts: int = 5, backoff_ms: int = 100) -> None:
    """Retry a file move with linear backoff to handle transient OS file locks."""
    for attempt in range(attempts):
        try:
            src.replace(dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(backoff_ms / 1000 * (attempt + 1))

def _restore_moved_cleanup_paths(item_hash: str, moved_paths: list[tuple[Path, Path]]) -> list[dict]:
    restore_errors = []
    for temp_path, original_path in reversed(moved_paths):
        try:
            if temp_path.exists() and not original_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                _retry_file_move(temp_path, original_path)
        except OSError as exc:
            restore_errors.append({"hash": item_hash, "path": str(original_path), "error": str(exc)})
    return restore_errors

def _stage_cleanup_paths(item_hash: str, cleanup_paths: list[Path], trash_dir: Path) -> tuple[list[tuple[Path, Path]], list[dict]]:
    trash_dir.mkdir(parents=True, exist_ok=True)
    moved_paths: list[tuple[Path, Path]] = []
    for index, path in enumerate(path for path in cleanup_paths if path.exists()):
        try:
            temp_path = _trash_path_for(trash_dir, item_hash, index, path)
            _retry_file_move(path, temp_path)
            moved_paths.append((temp_path, path))
        except OSError as exc:
            cleanup_errors = [{"hash": item_hash, "path": str(path), "error": str(exc)}]
            cleanup_errors.extend(_restore_moved_cleanup_paths(item_hash, moved_paths))
            return moved_paths, cleanup_errors
    return moved_paths, []


def _remove_staged_cleanup_paths(item_hash: str, moved_paths: list[tuple[Path, Path]], log_label: str, log_context: str = "system") -> list[dict]:
    cleanup_errors = []
    for temp_path, _original_path in moved_paths:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError as exc:
            error = {"hash": item_hash, "path": str(temp_path), "error": str(exc)}
            cleanup_errors.append(error)
            if log_context == "review":
                log_review("WARNING", log_label, target_hash=item_hash, path=str(temp_path), error=str(exc))
            else:
                log_system("WARNING", log_label, hash=item_hash, path=str(temp_path), error=str(exc))
    return cleanup_errors


def _delete_item_row_locked(
    cursor,
    conn,
    item_hash: str,
    row,
    trash_dir: Path,
    remove_indexes: bool,
    ctx: WorkspaceContext | None,
    log_context: str = "system",
):
    cleanup_paths = _item_file_paths(item_hash, row[0] or "", row[1] or "", row[2], conn, ctx=ctx)
    previous_facet_values = item_facet_values(conn, item_hash)
    moved_paths, cleanup_errors = _stage_cleanup_paths(item_hash, cleanup_paths, trash_dir)
    if cleanup_errors:
        if log_context == "review":
            log_review("WARNING", "Review replace staging failed", target_hash=item_hash, cleanup_errors=cleanup_errors)
        else:
            log_system("WARNING", "Item delete staging failed", hash=item_hash, cleanup_errors=cleanup_errors)
        return {"hash": item_hash, "status": "cleanup_failed", "cleanup_errors": cleanup_errors, "deleted": False}

    try:
        cursor.execute("DELETE FROM items WHERE hash = ?", (item_hash,))
        refresh_metadata_facet_counts_for_values(conn, previous_facet_values)
        refresh_metadata_index_counters(conn)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        cleanup_errors = [{"hash": item_hash, "path": "database", "error": str(exc)}]
        cleanup_errors.extend(_restore_moved_cleanup_paths(item_hash, moved_paths))
        if log_context == "review":
            log_review("WARNING", "Review replace database step failed; restored staged files", target_hash=item_hash, error=str(exc), cleanup_errors=cleanup_errors)
        else:
            log_system("WARNING", "Item delete database step failed; restored staged files", hash=item_hash, error=str(exc), cleanup_errors=cleanup_errors)
        return {"hash": item_hash, "status": "cleanup_failed", "cleanup_errors": cleanup_errors, "deleted": False}

    index_payload = {"hash": item_hash, "source_url": row[3] or ""}
    if remove_indexes:
        if ctx is None:
            search_manager.remove_indexes_batch([index_payload])
        else:
            search_manager.remove_indexes_batch([index_payload], ctx=ctx)

    cleanup_errors = _remove_staged_cleanup_paths(
        item_hash,
        moved_paths,
        "Review replace staged file cleanup failed" if log_context == "review" else "Deleted DB row but staged file cleanup failed",
        log_context=log_context,
    )
    return {"hash": item_hash, "status": "deleted", "cleanup_errors": cleanup_errors, "index": index_payload, "deleted": True}


def _delete_item_row(cursor, conn, item_hash: str, remove_indexes: bool = True, ctx: WorkspaceContext | None = None):
    cursor.execute("SELECT file_extension, mime_type, storage_id, source_url FROM items WHERE hash = ?", (item_hash,))
    row = cursor.fetchone()
    if not row:
        return {"hash": item_hash, "status": "missing", "cleanup_errors": []}
    with storage_lifecycle_lock(row[2], ctx=ctx):
        cursor.execute("SELECT file_extension, mime_type, storage_id, source_url FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row:
            return {"hash": item_hash, "status": "missing", "cleanup_errors": []}
        result = _delete_item_row_locked(
            cursor,
            conn,
            item_hash,
            row,
            _cleanup_trash_dir(".delete-trash", ctx),
            remove_indexes,
            ctx,
        )

    if result["status"] == "deleted":
        log_system("INFO", f"Deleted item {item_hash}")
    return result

def _delete_item_after_replacement(item_hash: str, ctx: WorkspaceContext | None = None):
    conn = init_database(ctx=ctx)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT file_extension, mime_type, storage_id, source_url FROM items WHERE hash = ?", (item_hash,))
        row = cursor.fetchone()
        if not row:
            return {"hash": item_hash, "status": "missing", "cleanup_errors": []}
        with storage_lifecycle_lock(row[2], ctx=ctx):
            cursor.execute("SELECT file_extension, mime_type, storage_id, source_url FROM items WHERE hash = ?", (item_hash,))
            row = cursor.fetchone()
            if not row:
                return {"hash": item_hash, "status": "missing", "cleanup_errors": []}
            return _delete_item_row_locked(
                cursor,
                conn,
                item_hash,
                row,
                _review_dir(ctx) / ".replace-trash",
                True,
                ctx,
                log_context="review",
            )
    finally:
        conn.close()

def _delete_item_sync(item_hash: str):
    conn = init_database()
    cursor = conn.cursor()
    try:
        result = _delete_item_row(cursor, conn, item_hash)
        if result["status"] == "missing":
            raise HTTPException(status_code=404)
        if result["status"] == "cleanup_failed":
            return {
                "status": "warning",
                "deleted": False,
                "message": "Item was not deleted because cleanup staging or database deletion failed.",
                "cleanup_errors": result["cleanup_errors"],
            }
        return {"status": "success", "deleted": True, "cleanup_errors": result["cleanup_errors"]}
    finally:
        conn.close()

def _bulk_delete_items_sync(hashes: list[str]):
    unique_hashes = []
    seen = set()
    for value in hashes or []:
        item_hash = str(value or "").strip()
        if item_hash and item_hash not in seen:
            unique_hashes.append(item_hash)
            seen.add(item_hash)

    conn = init_database()
    cursor = conn.cursor()
    deleted = []
    missing = []
    failed_cleanup = []
    index_payloads = []
    try:
        for item_hash in unique_hashes:
            result = _delete_item_row(cursor, conn, item_hash, remove_indexes=False)
            if result["status"] == "missing":
                missing.append(item_hash)
            elif result["status"] == "cleanup_failed":
                failed_cleanup.extend(result["cleanup_errors"])
            else:
                deleted.append(item_hash)
                failed_cleanup.extend(result["cleanup_errors"])
                index_payloads.append(result.get("index") or {"hash": item_hash})
        search_manager.remove_indexes_batch(index_payloads)
        return {
            "status": "success",
            "requested_count": len(unique_hashes),
            "deleted_count": len(deleted),
            "missing_count": len(missing),
            "failed_cleanup_count": len(failed_cleanup),
            "deleted": deleted,
            "missing": missing,
            "failed_cleanup": failed_cleanup,
        }
    finally:
        conn.close()

@router.post("/api/items/{item_hash}/tag")
async def trigger_tagging(item_hash: str):
    def sync_tagging():
        conn = init_database()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT file_extension, mime_type, storage_id FROM items WHERE hash = ?", (item_hash,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404)
            
            asset_path = asset_path_for(item_hash, row[0] or "", row[1] or "", storage_id=row[2])
            if not asset_path.exists():
                raise HTTPException(status_code=404, detail="Asset missing")

            with storage_lifecycle_lock(row[2]):
                current = cursor.execute("SELECT file_extension, mime_type, storage_id FROM items WHERE hash = ?", (item_hash,)).fetchone()
                if not current or current[2] != row[2] or not asset_path.exists():
                    raise HTTPException(status_code=404, detail="Item was deleted before tagging")
                log_system("INFO", f"Triggering AI tagging for {item_hash}")
                tag_media(asset_path, item_hash=item_hash, config=get_app_settings(), storage_id=row[2])

                md_content = generate_markdown(conn, item_hash, force_wd_from_cache=True)
                if md_content:
                    note_path = note_path_for(item_hash, storage_id=row[2])
                    note_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(note_path, md_content)
                    safe_reindex_item_metadata(conn, item_hash, "manual_tag")
                    conn.commit()
            
            cursor.execute("SELECT hash, file_extension, mime_type, original_filename, source_url, date_added, platform, source_artist, width, height, storage_id FROM items WHERE hash = ?", (item_hash,))
            updated_row = cursor.fetchone()
            return _get_item_details(item_hash, updated_row, conn)
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(sync_tagging)
    except HTTPException:
        raise
    except Exception as e:
        print(f"!!! TAGGING CRASH !!!\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
