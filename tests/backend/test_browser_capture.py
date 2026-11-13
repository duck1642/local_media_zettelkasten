import base64
import concurrent.futures
import json
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from .test_mock_vault_isolation import fresh_backend


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)

APPROVED_EXTENSION_ORIGIN = "chrome-extension://ccpkdmcgagkelbfmbakapnminicjmmlk"


def _client(monkeypatch, tmp_path):
    app_module, common = fresh_backend(monkeypatch, tmp_path, "api.app", "api.common")
    client = TestClient(app_module.app)
    client.capture_module = app_module.capture
    client.runtime_context = app_module.capture.get_runtime_context
    return client, common._api_key()


def test_capture_stage_writes_file_and_sidecar(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={
            "source_url": "https://x.com/creator/status/1",
            "media_url": "https://pbs.twimg.com/media/sample.png",
            "page_title": "Sample post",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform_guess"] == "X"
    stage_dir = client.capture_module._capture_stage_dir(client.runtime_context())
    sidecar = json.loads((stage_dir / f"{payload['staged_id']}.json").read_text(encoding="utf-8"))
    assert sidecar["source_url"] == "https://x.com/creator/status/1"
    assert sidecar["media_url"] == "https://pbs.twimg.com/media/sample.png"
    assert (stage_dir / sidecar["stored_name"]).exists()


def test_capture_stage_rejects_unsupported_media(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.txt", b"not media", "text/plain")},
        data={"source_url": "https://example.com"},
    )

    assert response.status_code == 400


def test_capture_preview_requires_api_key(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    staged = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    ).json()

    missing = client.get(f"/api/capture/preview/{staged['staged_id']}")
    ok = client.get(
        f"/api/capture/preview/{staged['staged_id']}",
        headers={"X-LMZ-API-KEY": api_key},
    )

    assert missing.status_code == 403
    assert ok.status_code == 200
    assert ok.content


def test_capture_jxl_blob_previews_as_jpeg_and_rejects_invalid_signature(monkeypatch, tmp_path):
    from PIL import Image
    from jxl_preview import register_jxl_decoder

    client, api_key = _client(monkeypatch, tmp_path)
    headers = {"X-LMZ-API-KEY": api_key}
    monkeypatch.setattr(client.capture_module, "get_app_settings", lambda: {
        "ingestion": {"accepted_media": {"mime_types": ["image/jxl"], "extensions": [".jxl"]}}
    })
    register_jxl_decoder()
    output = BytesIO()
    Image.new("RGB", (12, 8), "red").save(output, format="JXL")
    staged = client.post("/api/capture/stage", headers=headers,
                         files={"file": ("blob", output.getvalue(), "image/jxl")})
    assert staged.status_code == 200
    assert staged.json()["original_name"].endswith(".jxl")
    preview = client.get(f"/api/capture/preview/{staged.json()['staged_id']}", headers=headers)
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"

    invalid = client.post("/api/capture/stage", headers=headers,
                          files={"file": ("blob", b"not a jxl", "image/jxl")})
    assert invalid.status_code == 400

    truncated = client.post("/api/capture/stage", headers=headers,
                            files={"file": ("blob", b"\xff\x0abad", "image/jxl")})
    assert truncated.status_code == 200  # Staging checks the signature, not the whole image.
    response = client.get(f"/api/capture/preview/{truncated.json()['staged_id']}", headers=headers)
    assert response.status_code == 422


def test_capture_delete_removes_staged_artifacts(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    staged = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    ).json()

    response = client.delete(
        f"/api/capture/stage/{staged['staged_id']}",
        headers={"X-LMZ-API-KEY": api_key},
    )

    assert response.status_code == 200
    leftovers = list(client.capture_module._capture_stage_dir(client.runtime_context()).glob(f"{staged['staged_id']}.*"))
    assert leftovers == []


def test_approved_extension_can_use_capture_and_queue_endpoints(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    headers = {"Origin": APPROVED_EXTENSION_ORIGIN, "X-LMZ-API-KEY": api_key}

    staged_response = client.post(
        "/api/capture/stage",
        headers=headers,
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )
    assert staged_response.status_code == 200
    staged_id = staged_response.json()["staged_id"]

    preview = client.get(f"/api/capture/preview/{staged_id}", headers=headers)
    assert preview.status_code == 200
    assert preview.content

    disposable = client.post(
        "/api/capture/stage",
        headers=headers,
        files={"file": ("discard.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com/discard"},
    ).json()["staged_id"]
    deleted = client.delete(f"/api/capture/stage/{disposable}", headers=headers)
    assert deleted.status_code == 200

    monkeypatch.setattr(
        client.capture_module,
        "process_file",
        lambda *args, **kwargs: (True, "Success: sample.png -> 000001.png", {"tagging_status": "not_run"}),
    )
    committed = client.post(
        "/api/capture/commit",
        headers=headers,
        json={"staged_id": staged_id, "artist": "Alice", "platform": "General Web"},
    )
    assert committed.status_code == 200

    queued = client.post(
        "/api/queue/normal/append",
        headers={**headers, "Content-Type": "application/json"},
        json={"url": "https://www.pixiv.net/artworks/123", "artist": "Alice", "platform": "Pixiv"},
    )
    assert queued.status_code == 200


def test_capture_invalid_staged_id_rejected(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)

    response = client.delete(
        "/api/capture/stage/../bad",
        headers={"X-LMZ-API-KEY": api_key},
    )

    assert response.status_code in {400, 404}


def test_capture_commit_uses_processor_metadata_and_cleans_stage(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    staged = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={
            "source_url": "https://artist.example/post",
            "media_url": "https://cdn.example/sample.png",
        },
    ).json()
    capture = client.capture_module
    seen = {}

    def fake_process_file(filepath, config, metadata=None, delete_source=False, skip_similarity=False, ctx=None):
        seen["filepath"] = filepath
        seen["metadata"] = metadata
        seen["delete_source"] = delete_source
        seen["skip_similarity"] = skip_similarity
        seen["ctx"] = ctx
        return True, "Success: sample.png -> 000001.png", {"tagging_status": "not_run"}

    monkeypatch.setattr(capture, "process_file", fake_process_file)

    response = client.post(
        "/api/capture/commit",
        headers={"X-LMZ-API-KEY": api_key},
        json={"staged_id": staged["staged_id"], "artist": "Alice", "platform": "General Web"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ingested"
    assert seen["metadata"]["source_url"] == "https://artist.example/post"
    assert seen["metadata"]["media_url"] == "https://cdn.example/sample.png"
    assert seen["metadata"]["artist"] == "Alice"
    assert seen["metadata"]["staged_from"] == "browser_capture"
    assert seen["metadata"]["ingest_type"] == "capture"
    assert seen["delete_source"] is True
    leftovers = list(capture._capture_stage_dir(client.runtime_context()).glob(f"{staged['staged_id']}.*"))
    assert leftovers == []


def test_capture_commit_same_staged_id_is_locked_and_idempotent(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    staged = client.post(
        "/api/capture/stage",
        headers={"X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    ).json()
    capture = client.capture_module
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_process_file(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2)
        return True, "Success: sample.png -> 000001.png", {"tagging_status": "ok"}

    monkeypatch.setattr(capture, "process_file", fake_process_file)
    body = capture.CaptureCommitRequest(staged_id=staged["staged_id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(capture._commit_capture_sync, body)
        assert started.wait(2)
        with pytest.raises(HTTPException) as exc:
            capture._commit_capture_sync(body)
        assert exc.value.status_code == 409
        release.set()
        first_result = first.result(timeout=2)

    assert first_result["status"] == "ingested"
    assert calls == 1

    repeat_result = capture._commit_capture_sync(body)
    assert repeat_result["status"] == "ingested"
    assert repeat_result["already_handled"] is True
    assert calls == 1


def test_capture_commit_maps_review_and_duplicate_statuses(monkeypatch, tmp_path):
    client, api_key = _client(monkeypatch, tmp_path)
    capture = client.capture_module

    def stage_one():
        return client.post(
            "/api/capture/stage",
            headers={"X-LMZ-API-KEY": api_key},
            files={"file": ("sample.png", PNG_BYTES, "image/png")},
            data={"source_url": "https://example.com"},
        ).json()

    monkeypatch.setattr(
        capture,
        "process_file",
        lambda *args, **kwargs: (False, "asi   Visual Match (phash) -> Moved to review/", None),
    )
    review_response = client.post(
        "/api/capture/commit",
        headers={"X-LMZ-API-KEY": api_key},
        json={"staged_id": stage_one()["staged_id"]},
    )

    monkeypatch.setattr(
        capture,
        "process_file",
        lambda *args, **kwargs: (False, "Duplicate ignored: abcdef12...", None),
    )
    duplicate_response = client.post(
        "/api/capture/commit",
        headers={"X-LMZ-API-KEY": api_key},
        json={"staged_id": stage_one()["staged_id"]},
    )

    assert review_response.json()["status"] == "quarantined"
    assert duplicate_response.json()["status"] == "duplicate"


def test_queue_append_writes_metadata_block(monkeypatch, tmp_path):
    queue_service, ingestion_api = fresh_backend(monkeypatch, tmp_path, "queue_service", "api.ingestion")

    result = ingestion_api._append_queue_entry_sync(
        "normal",
        ingestion_api.QueueAppendRequest(
            url="https://www.pixiv.net/artworks/123",
            artist="Painter",
            platform="Pixiv",
        ),
    )
    text = queue_service.read_queue("normal")
    preview = queue_service.parse_queue_preview(text)

    assert result["count"] == 2
    assert "@artist: Painter" in text
    assert "@platform: Pixiv" in text
    assert "https://www.pixiv.net/artworks/123" in text
    assert preview["entries"][-1]["artist"] == "Painter"
    assert preview["entries"][-1]["platform"] == "Pixiv"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/post/Ugkxabc123",
        "https://www.youtube.com/@name/community?lb=Ugkxabc123",
    ],
)
def test_queue_append_accepts_ingestible_youtube_urls(monkeypatch, tmp_path, url):
    queue_service, ingestion_api = fresh_backend(monkeypatch, tmp_path, "queue_service", "api.ingestion")

    result = ingestion_api._append_queue_entry_sync(
        "normal",
        ingestion_api.QueueAppendRequest(url=url, platform="YouTube"),
    )
    text = queue_service.read_queue("normal")

    assert result["status"] == "success"
    assert url in text
    assert "@platform: YouTube" in text


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/",
        "https://www.youtube.com/results?search_query=test",
        "https://www.youtube.com/@name",
        "https://www.youtube.com/playlist?list=PLabc123",
    ],
)
def test_queue_append_rejects_non_ingestible_youtube_urls(monkeypatch, tmp_path, url):
    _queue_service, ingestion_api = fresh_backend(monkeypatch, tmp_path, "queue_service", "api.ingestion")

    with pytest.raises(HTTPException) as exc:
        ingestion_api._append_queue_entry_sync(
            "normal",
            ingestion_api.QueueAppendRequest(url=url, platform="YouTube"),
        )

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://ccpkdmcgagkelbfmbakapnminicjmmlk",
    ],
)
def test_extension_origin_requires_valid_api_key(monkeypatch, tmp_path, origin):
    client, api_key = _client(monkeypatch, tmp_path)

    missing_key = client.post(
        "/api/capture/stage",
        headers={"Origin": origin},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )
    valid_key = client.post(
        "/api/capture/stage",
        headers={"Origin": origin, "X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )
    web_origin = client.post(
        "/api/capture/stage",
        headers={"Origin": "https://evil.example", "X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )

    assert missing_key.status_code == 403
    assert valid_key.status_code == 200
    assert web_origin.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://ccpkdmcgagkelbfmbakapnminicjmmlk",
    ],
)
def test_extension_origin_cors_preflight_allowed(monkeypatch, tmp_path, origin):
    client, _api_key = _client(monkeypatch, tmp_path)

    response = client.options(
        "/api/capture/stage",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-LMZ-API-KEY",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "moz-extension://123e4567-e89b-12d3-a456-426614174000",
        "https://evil.example",
    ],
)
def test_unapproved_origins_cannot_get_key_or_mutate(monkeypatch, tmp_path, origin):
    client, api_key = _client(monkeypatch, tmp_path)

    session_key = client.get("/api/session-key", headers={"Origin": origin})
    staged = client.post(
        "/api/capture/stage",
        headers={"Origin": origin, "X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )

    assert session_key.status_code == 403
    assert staged.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
)
def test_existing_local_origins_remain_allowed(monkeypatch, tmp_path, origin):
    client, api_key = _client(monkeypatch, tmp_path)

    session_key = client.get("/api/session-key", headers={"Origin": origin})
    staged = client.post(
        "/api/capture/stage",
        headers={"Origin": origin, "X-LMZ-API-KEY": api_key},
        files={"file": ("sample.png", PNG_BYTES, "image/png")},
        data={"source_url": "https://example.com"},
    )

    assert session_key.status_code == 200
    assert staged.status_code == 200


def test_unapproved_origin_cors_preflight_is_rejected(monkeypatch, tmp_path):
    client, _api_key = _client(monkeypatch, tmp_path)

    response = client.options(
        "/api/capture/stage",
        headers={
            "Origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-LMZ-API-KEY",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
