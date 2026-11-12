import asyncio
import importlib
import json
import sys
import types
from contextlib import nullcontext

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from .test_mock_vault_isolation import fresh_backend, insert_mock_item, storage_id_for


def test_new_settings_recommend_pixai_and_legacy_settings_keep_wd():
    from config_schema import TaggingSettings

    assert TaggingSettings().backend == "pixai"
    legacy = TaggingSettings.model_validate(
        {"model_repo": "SmilingWolf/wd-vit-tagger-v3", "threshold": 0.4}
    )
    assert legacy.backend == "wd"
    assert legacy.threshold == 0.4
    assert TaggingSettings.model_validate({"enabled": False}).backend == "wd"
    assert TaggingSettings.model_validate({"backend": "pixai"}).backend == "pixai"


@pytest.mark.parametrize(
    "profile",
    [
        {"thresholds": {"general": 1.1}},
        {"category_map": {"style": ""}},
        {"category_map": {"style": "invalid category"}},
        {"label_map": {"rating": {"rating:s": ""}}},
    ],
)
def test_invalid_output_profiles_are_rejected(profile):
    from config_schema import TagOutputProfile

    with pytest.raises(ValidationError):
        TagOutputProfile.model_validate(profile)


def test_profile_keeps_source_names_and_unknown_categories(monkeypatch, tmp_path):
    fresh_backend(monkeypatch, tmp_path, "tagging")
    from tagging.contracts import Prediction
    from tagging.profiles import apply_profile

    predictions = [
        Prediction("rating:s", "rating", 0.9),
        Prediction("series_a", "copyright", 0.8),
        Prediction("soft_style", "style", 0.6),
        Prediction("new_label", "new_category"),
    ]
    groups = apply_profile(
        predictions,
        {
            "category_map": {"copyright": "franchise", "style": None},
            "label_map": {"rating": {"rating:s": "sensitive"}},
        },
        30,
    )
    assert "style" not in groups
    assert groups["rating"][0]["name"] == "sensitive"
    assert groups["rating"][0]["source_name"] == "rating:s"
    assert groups["franchise"][0]["source_category"] == "copyright"
    assert "score" not in groups["new_category"][0]
    assert predictions[0].name == "rating:s"


def test_pixai_output_survives_cache_note_index_and_manual_edit(monkeypatch, tmp_path):
    service, utils, db, md, index, api = fresh_backend(
        monkeypatch,
        tmp_path,
        "tagging.service",
        "utils",
        "db.sqlite_operator",
        "md_generator",
        "metadata_index",
        "api.library",
    )
    from tagging.contracts import Prediction

    item_hash = "af" * 32
    conn = insert_mock_item(db, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(asset)
    fake = types.SimpleNamespace(
        provider="PyTorch:cpu",
        warning="",
        predict=lambda image, thresholds: [
            Prediction("rating:s", "rating", 0.9),
            Prediction("hero", "character", 0.8),
            Prediction("series_a", "copyright", 0.9),
            Prediction("1girl", "general", 0.9),
            Prediction("soft", "style", 0.8),
        ],
    )
    monkeypatch.setattr(service, "get_backend", lambda *args: fake)
    monkeypatch.setattr(service, "get_mime_type", lambda path: "image/jpeg")
    result = service.tag_media(
        asset,
        item_hash=item_hash,
        storage_id=storage_id,
        config={
            "tagging": {
                "backend": "pixai",
                "profiles": {"pixai": {"category_map": {"style": None}}},
            }
        },
    )
    assert result.status == "ok", result.error
    assert result.backend == "pixai"
    assert result.rating["label"] == "rating:s"
    assert "style" not in result.groups
    assert any(tag["category"] == "style" for tag in result.predictions)
    cached = json.loads(
        utils.wd_tag_cache_path_for(item_hash, storage_id).read_text(encoding="utf-8")
    )
    assert cached["groups"]["copyright"][0]["name"] == "series_a"
    note = utils.note_path_for(item_hash, storage_id)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        md.generate_markdown(conn, item_hash, force_wd_from_cache=True), encoding="utf-8"
    )
    index.reindex_item_metadata(conn, item_hash)
    conn.commit()
    detail = api._get_item_details(
        item_hash,
        conn.execute(
            "SELECT hash, file_extension, mime_type, original_filename, source_url, date_added, platform, source_artist, width, height, storage_id FROM items WHERE hash = ?",
            (item_hash,),
        ).fetchone(),
        conn,
    )
    assert detail["wd_tags"]["groups"] == {"copyright": ["series a"]}
    assert detail["wd_tags"]["characters"] == ["hero"]
    assert detail["wd_tag_counts"]["series a"] == 1
    conn.close()
    updated = api._update_item_sync(item_hash, api.ItemUpdate(tag_groups={"copyright": []}))
    assert updated["wd_tags"]["groups"] == {}
    assert md.load_note_frontmatter(item_hash, storage_id)["tag_groups"] == {"copyright": []}


def test_video_uses_common_groups_and_preserves_frame_predictions(monkeypatch, tmp_path):
    (service,) = fresh_backend(monkeypatch, tmp_path, "tagging.service")
    from tagging.contracts import Prediction

    asset = tmp_path / "sample.mp4"
    asset.write_bytes(b"fake-video")
    monkeypatch.setattr(service, "get_mime_type", lambda path: "video/mp4")
    monkeypatch.setattr(
        service,
        "extract_sampled_video_frames",
        lambda *args: [
            (0.0, Image.new("RGB", (4, 4))),
            (1.0, Image.new("RGB", (4, 4))),
        ],
    )
    monkeypatch.setattr(
        service,
        "get_backend",
        lambda *args: types.SimpleNamespace(
            provider="fake",
            warning="",
            predict=lambda *args: [Prediction("series", "copyright", 0.6)],
        ),
    )
    result = service.tag_media(asset, config={"tagging": {"backend": "pixai"}})
    assert result.status == "ok", result.error
    assert result.frame_count == 2
    assert result.groups["copyright"][0]["frame_count"] == 2
    assert result.sampled_frames[0]["predictions"][0]["category"] == "copyright"


def test_disabled_tagging_never_loads_a_model(monkeypatch, tmp_path):
    (service,) = fresh_backend(monkeypatch, tmp_path, "tagging.service")
    monkeypatch.setattr(
        service, "get_backend", lambda *args: pytest.fail("Loaded a disabled model")
    )
    result = service.tag_media(tmp_path / "missing.jpg", config={"tagging": {"enabled": False}})
    assert result.status == "skipped"


def test_model_cache_separates_roots_and_releases_previous_model(monkeypatch, tmp_path):
    registry, wd = fresh_backend(monkeypatch, tmp_path, "tagging.registry", "tagging.wd")
    created = []

    class FakeBackend:
        def __init__(self, *args):
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(wd, "WdTagger", FakeBackend)
    first = registry.get_backend("wd", "org/model", "cpu", tmp_path / "a")
    assert registry.get_backend("wd", "org/model", "cpu", tmp_path / "a") is first
    second = registry.get_backend("wd", "org/model", "cpu", tmp_path / "b")
    assert first.closed
    assert second is not first
    registry.unload_models()
    assert second.closed


def test_pixai_pipeline_parses_categories_without_downloading_local_model(monkeypatch, tmp_path):
    fresh_backend(monkeypatch, tmp_path, "tagging")
    pixai = importlib.import_module("tagging.pixai")
    local = tmp_path / "local-model"
    local.mkdir()
    (local / "config.json").write_text("{}")
    calls = []
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        float16="fp16",
        float32="fp32",
        inference_mode=nullcontext,
    )

    def pipeline(*args, **kwargs):
        calls.append(kwargs)
        return lambda image, **options: {"results": {"copyright": {"test_series": 0.9}}}

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(pipeline=pipeline))
    monkeypatch.setitem(sys.modules, "timm", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torchvision", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(
            snapshot_download=lambda **kwargs: pytest.fail("Unexpected download")
        ),
    )
    backend = pixai.PixaiTagger("org/model", "auto", tmp_path, str(local))
    predictions = backend.predict(Image.new("RGB", (4, 4)), {"copyright": 0.24})
    assert predictions[0].category == "copyright"
    assert predictions[0].name == "test_series"
    assert backend.provider == "PyTorch:cpu"
    assert calls[0]["device"] == -1
    assert calls[0]["dtype"] == "fp32"
    with pytest.raises(RuntimeError, match="cannot access CUDA"):
        pixai.PixaiTagger("org/model", "cuda", tmp_path, str(local))


def test_extra_categories_participate_in_metadata_maintenance(monkeypatch, tmp_path):
    (maintenance,) = fresh_backend(monkeypatch, tmp_path, "metadata_maintenance")
    frontmatter = {"wd_tags": ["untouched"], "tag_groups": {"copyright": ["old", "keep"]}}
    changed, values = maintenance._transform_wd_frontmatter(
        frontmatter,
        "copyright",
        lambda value: "new" if value == "old" else value,
    )
    assert changed
    assert values == {"old", "new"}
    assert ("copyright", "new") in maintenance._wd_rows_from_frontmatter(frontmatter)
    assert frontmatter["wd_tags"] == ["untouched"]


def test_model_descriptions_are_available_before_runtime_load(monkeypatch, tmp_path):
    (app_module,) = fresh_backend(monkeypatch, tmp_path, "api.app")
    from runtime_context import set_runtime_context

    set_runtime_context(None)
    response = TestClient(app_module.app).get("/api/tagging/models")
    assert response.status_code == 200, response.text
    models = response.json()
    assert [model["id"] for model in models] == ["pixai", "wd"]
    assert models[0]["recommended"] is True
    assert "copyright" in models[0]["categories"]


def test_failed_manual_tagging_preserves_existing_note_and_cache(monkeypatch, tmp_path):
    utils, db, _md, api, service = fresh_backend(
        monkeypatch,
        tmp_path,
        "utils",
        "db.sqlite_operator",
        "md_generator",
        "api.library",
        "tagging.service",
    )
    item_hash = "ba" * 32
    conn = insert_mock_item(db, item_hash)
    storage_id = storage_id_for(conn, item_hash)
    asset = utils.asset_path_for(item_hash, ".jpg", "image/jpeg", storage_id=storage_id)
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4)).save(asset)
    note = utils.note_path_for(item_hash, storage_id)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\nwd_tags: [keep]\ntag_groups:\n  copyright: [keep-series]\n---\n", encoding="utf-8"
    )
    cache = utils.wd_tag_cache_path_for(item_hash, storage_id)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "hash": item_hash,
        "schema_version": 2,
        "status": "ok",
        "predictions": [{"name": "keep-series", "category": "copyright", "score": 0.91}],
    }), encoding="utf-8")
    previous_note = note.read_bytes()
    previous_cache = cache.read_bytes()
    conn.close()

    def fail_inference(*args):
        raise RuntimeError("PixAI model unavailable")

    monkeypatch.setattr(service, "_infer_media", fail_inference)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.trigger_tagging(item_hash))
    assert exc.value.status_code == 503
    assert exc.value.detail == "PixAI model unavailable"
    assert note.read_bytes() == previous_note
    assert cache.read_bytes() == previous_cache


def test_wd_reports_provider_changes_after_inference(monkeypatch, tmp_path):
    wd, = fresh_backend(monkeypatch, tmp_path, "tagging.wd")
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    backend = object.__new__(wd.WdTagger)
    backend.session = types.SimpleNamespace(get_providers=lambda: providers)
    backend.input_meta = backend.output_meta = None
    backend.labels = []
    backend._warning = ""

    def fallback(*args):
        providers[:] = ["CPUExecutionProvider"]
        return []

    monkeypatch.setattr(wd, "_predict_image_tags", fallback)
    backend.predict(Image.new("RGB", (4, 4)), {})
    assert backend.provider == "CPUExecutionProvider"
    assert "changed from CUDAExecutionProvider to CPUExecutionProvider" in backend.warning
