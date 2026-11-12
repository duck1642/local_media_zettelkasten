import numpy as np
import pytest

from .test_mock_vault_isolation import fresh_backend


def test_audio_popcount_matches_previous_bit_expansion(monkeypatch, tmp_path):
    fingerprint, = fresh_backend(monkeypatch, tmp_path, "fingerprint")
    rng = np.random.default_rng(20261112)

    for a_words, b_words in [(1, 1), (16, 16), (17, 5), (5, 17), (1024, 1024)]:
        for _ in range(12):
            a, b = rng.bytes(a_words * 4), rng.bytes(b_words * 4)
            words = min(a_words, b_words)
            xor = np.frombuffer(a, dtype=np.uint32, count=words) ^ np.frombuffer(b, dtype=np.uint32, count=words)
            expected = 1.0 - int(np.unpackbits(xor.view(np.uint8)).sum()) / (words * 32)
            assert fingerprint.compare_audio_fingerprints(a, b) == expected


def test_audio_similarity_preserves_prefix_and_input_edges(monkeypatch, tmp_path):
    fingerprint, = fresh_backend(monkeypatch, tmp_path, "fingerprint")
    compare = fingerprint.compare_audio_fingerprints
    zeros = b"\0" * 4
    ones = b"\xff" * 4
    assert compare(zeros, zeros + ones) == 1.0
    assert compare(ones + zeros, zeros) == 0.0
    assert compare(b"", zeros) == compare(zeros, b"") == 0.0
    assert compare(b"x", b"x") == 1.0
    with pytest.raises(ValueError, match="multiple of element size"):
        compare(b"x", zeros)
