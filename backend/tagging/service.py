"""Media orchestration and legacy metadata compatibility for all taggers."""

import atexit
import json
import threading
from pathlib import Path

from fingerprint import extract_sampled_video_frames
from logger import log_system
from media_lifecycle import storage_lifecycle_lock
from PIL import Image, ImageOps
from runtime_context import WorkspaceContext, get_runtime_context
from utils import (
    atomic_write_text,
    calculate_file_hash,
    get_app_settings,
    utc_now_str,
    wd_tag_cache_path_for,
)
from validators import get_mime_type

from tagging.contracts import TagResult
from tagging.profiles import apply_profile, legacy_fields, limit_predictions, score_key
from tagging.registry import get_backend, resolve_model
from tagging.registry import unload_models as _unload_models

_INFERENCE_LOCK = threading.RLock()


def unload_models() -> None:
    with _INFERENCE_LOCK:
        _unload_models()


atexit.register(unload_models)


def tag_media(
    media_path: str | Path,
    item_hash: str | None = None,
    config: dict | None = None,
    storage_id: str | None = None,
    ctx: WorkspaceContext | None = None,
) -> TagResult:
    path = Path(media_path)
    result = TagResult(
        hash=item_hash or "",
        status="failed",
        model="",
        threshold=0.35,
        created_at=utc_now_str(),
        rating=None,
        character_tags=[],
        tags=[],
    )
    try:
        settings = (config if config is not None else get_app_settings()).get("tagging", {})
        _infer_media(result, path, settings, ctx)
    except Exception as exc:  # noqa: BLE001 -- model failures are an enrichment result for callers
        result.status = "failed"
        result.error = str(exc)
    try:
        _write_result(result, storage_id, path, ctx)
    except Exception as exc:  # noqa: BLE001 -- persistence errors must reach the ingestion result
        result.status = "failed"
        result.error = f"Could not save tagging result: {exc}"
    if result.status != "skipped":
        log_system(
            "INFO" if result.status == "ok" else "WARNING",
            "AI tagging completed",
            hash=result.hash,
            backend=result.backend,
            provider=result.provider,
            status=result.status,
            error=result.error,
            tag_count=sum(map(len, result.groups.values())),
        )
    return result


def _infer_media(result: TagResult, path: Path, config: dict, ctx) -> None:
    backend_id, model, thresholds, profile = resolve_model(config)
    result.backend, result.model = backend_id, model
    result.thresholds, result.output_profile = thresholds, profile
    result.device = config.get("device", "auto")
    result.threshold = float(config.get("threshold", 0.35))
    result.max_tags = int(config.get("max_tags", 30))
    if result.max_tags < 1:
        raise ValueError("max_tags must be positive")
    if not result.hash and path.is_file():
        result.hash = calculate_file_hash(path)
    if not config.get("enabled", True):
        result.status, result.error = "skipped", "tagging disabled"
        return
    if not path.is_file():
        raise FileNotFoundError("media path does not exist")
    mime_type = get_mime_type(path) or ""
    result.media_type = (
        "video"
        if mime_type.startswith("video/")
        else "image"
        if mime_type.startswith("image/")
        else "unknown"
    )
    video = config.get("video") or {}
    if result.media_type == "unknown":
        result.status, result.error = "skipped", f"unsupported media type: {mime_type or 'unknown'}"
        return
    if result.media_type == "video" and not video.get("enabled", True):
        result.status, result.error = "skipped", "video tagging disabled"
        return
    runtime = ctx or get_runtime_context()
    with _INFERENCE_LOCK:
        backend = get_backend(
            backend_id, model, result.device, runtime.models_dir, config.get("model_path", "")
        )

        def predict(image):
            raw = limit_predictions(
                backend.predict(ImageOps.exif_transpose(image), thresholds), result.max_tags
            )
            return raw, apply_profile(raw, profile, result.max_tags)

        if result.media_type == "video":
            samples = extract_sampled_video_frames(path, int(video.get("frame_count", 5)))
            if not samples:
                raise RuntimeError("could not extract video frames")
            for timestamp, image in samples:
                raw, groups = predict(image)
                rating, characters, tags = legacy_fields(groups)
                result.sampled_frames.append(
                    {
                        "timestamp": round(float(timestamp), 3),
                        "groups": groups,
                        "predictions": [item.to_dict() for item in raw],
                        "rating": rating,
                        "character_tags": characters,
                        "tags": tags,
                    }
                )
            result.groups = _merge_frame_groups(
                result.sampled_frames,
                result.max_tags,
                int(video.get("merge_min_frames", 2)),
                float(video.get("merge_high_confidence", 0.75)),
            )
            result.frame_count = len(result.sampled_frames)
        else:
            with Image.open(path) as image:
                image.seek(0)
                raw, result.groups = predict(image)
                result.predictions = [item.to_dict() for item in raw]
        result.provider, result.error = backend.provider, backend.warning
    result.rating, result.character_tags, result.tags = legacy_fields(result.groups)
    result.status = "ok"


def _merge_frame_groups(
    frames: list[dict], max_tags: int, min_frames: int, high_confidence: float
) -> dict:
    categories = dict.fromkeys(category for frame in frames for category in frame["groups"])
    groups = {}
    for category in categories:
        candidates = {}
        for frame in frames:
            for tag in frame["groups"].get(category, []):
                candidates.setdefault(tag["name"], []).append(tag)
        merged = []
        for matches in candidates.values():
            best = max(matches, key=score_key)
            if category == "rating":
                merged.append(dict(best))
                continue
            scores = [tag["score"] for tag in matches if tag.get("score") is not None]
            if len(matches) < min_frames and (not scores or max(scores) < high_confidence):
                continue
            tag = dict(best, frame_count=len(matches))
            if scores:
                tag["score"] = round(sum(scores) / len(scores), 6)
            merged.append(tag)
        merged.sort(key=lambda tag: (tag.get("frame_count", 0), score_key(tag)), reverse=True)
        groups[category] = merged[: 1 if category == "rating" else max_tags]
    return groups


def _write_result(
    result: TagResult,
    storage_id: str | None = None,
    media_path: str | Path | None = None,
    ctx: WorkspaceContext | None = None,
) -> bool:
    if not result.hash or not storage_id:
        return False
    source = Path(media_path) if media_path is not None else None
    with storage_lifecycle_lock(storage_id, ctx=ctx):
        if source is not None and not source.exists():
            return False
        target = wd_tag_cache_path_for(result.hash, storage_id=storage_id, ctx=ctx)
        # Keep the last successful predictions and scores if a retag fails or is skipped.
        if result.status != "ok" and _read_tag_cache(target).get("status") == "ok":
            return False
        atomic_write_text(target, json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return True


def load_tag_cache(item_hash: str, storage_id: str) -> dict:
    return _read_tag_cache(wd_tag_cache_path_for(item_hash, storage_id))


def _read_tag_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "hash" not in data and "item_hash" in data:
        data = _old_cache_to_current(data)
    return data


def wd_frontmatter_fields(item_hash: str, storage_id: str) -> dict:
    data = load_tag_cache(item_hash, storage_id)
    if data.get("status") != "ok":
        return {"wd_rating": "", "wd_character_tags": [], "wd_tags": []}
    rating = data.get("rating") or {}
    character_tags = data.get("character_tags") or []
    tags = data.get("tags") or []
    fields = {
        "wd_rating": rating.get("label") or "",
        "wd_character_tags": [_tag_name(tag) for tag in character_tags if _tag_name(tag)],
        "wd_tags": [_tag_name(tag) for tag in tags if _tag_name(tag)],
    }
    if data.get("schema_version") == 2:
        fields["tag_groups"] = {
            category: [_tag_name(tag) for tag in values if _tag_name(tag)]
            for category, values in data.get("groups", {}).items()
            if category not in {"general", "character", "rating"}
        }
    return fields


def _tag_name(tag: dict) -> str:
    return str(tag.get("display_name") or tag.get("name") or "").strip()


def _old_cache_to_current(data: dict) -> dict:
    rating = None
    characters = []
    tags = []
    for tag in data.get("tags") or []:
        category = tag.get("category")
        item = {
            "name": tag.get("name", ""),
            "display_name": tag.get("display_name") or str(tag.get("name", "")).replace("_", " "),
            "score": tag.get("score", 0),
        }
        if category == "rating":
            rating = {"label": item["display_name"], "name": item["name"], "score": item["score"]}
        elif category == "character":
            characters.append(item)
        else:
            tags.append(item)
    return {
        "hash": data.get("item_hash", ""),
        "status": data.get("status", "unknown"),
        "model": data.get("model_repo", ""),
        "threshold": data.get("threshold", ""),
        "created_at": data.get("created_at", ""),
        "rating": rating,
        "character_tags": characters,
        "tags": tags,
        "provider": data.get("provider", ""),
        "device": data.get("device_requested", ""),
        "max_tags": data.get("max_tags", 0),
        "error": data.get("error", ""),
    }
