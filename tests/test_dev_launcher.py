import io
import json

import pytest

import dev


class FakeProcess:
    def __init__(self, return_code=None):
        self.return_code = return_code

    def poll(self):
        return self.return_code


def health_response(nonce: str):
    payload = {
        "service": "lmz-api",
        "ready": True,
        "protocol_version": 1,
        "nonce": nonce,
    }
    return io.BytesIO(json.dumps(payload).encode())


def test_wait_for_api_accepts_matching_health_response(monkeypatch):
    monkeypatch.setattr(dev, "urlopen", lambda *args, **kwargs: health_response("expected"))

    dev._wait_for_api(FakeProcess(), "expected")


def test_wait_for_api_retries_until_nonce_matches(monkeypatch):
    responses = iter([health_response("stale"), health_response("expected")])
    monkeypatch.setattr(dev, "urlopen", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(dev.time, "sleep", lambda _seconds: None)

    dev._wait_for_api(FakeProcess(), "expected")


def test_wait_for_api_fails_when_backend_exits():
    with pytest.raises(RuntimeError, match=r"exited before becoming ready \(code 3\)"):
        dev._wait_for_api(FakeProcess(return_code=3), "expected")


def test_wait_for_api_times_out(monkeypatch):
    timestamps = iter([0.0, 0.0, 31.0])
    requests = []

    def backend_not_ready(url, timeout):
        requests.append((url, timeout))
        raise OSError("backend is not ready")

    monkeypatch.setattr(dev.time, "monotonic", lambda: next(timestamps))
    monkeypatch.setattr(dev, "urlopen", backend_not_ready)
    monkeypatch.setattr(dev.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="within 30 seconds"):
        dev._wait_for_api(FakeProcess(), "expected")

    assert requests == [(dev.API_HEALTH_URL, 1)]
