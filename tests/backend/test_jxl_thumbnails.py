from __future__ import annotations

import sys
import threading
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _save_jxl(path: Path, image: Image.Image) -> None:
    # Loading the app's lazy registration function also registers JXL saving
    # in Pillow, letting these tests build deterministic fixtures in memory.
    from jxl_preview import register_jxl_decoder

    register_jxl_decoder()
    image.save(path, format="JXL", lossless=True)


def _decoded(path_or_bytes) -> Image.Image:
    source = BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with Image.open(source) as result:
        result.load()
        return result.copy()


def test_jxl_preview_uses_jpeg_for_opaque_and_png_for_transparency(tmp_path):
    from jxl_preview import make_jxl_preview

    opaque_path = tmp_path / "opaque.jxl"
    _save_jxl(opaque_path, Image.new("RGB", (7, 5), (210, 40, 20)))
    opaque_bytes, opaque_mime = make_jxl_preview(opaque_path)

    assert opaque_mime == "image/jpeg"
    assert opaque_bytes.startswith(b"\xff\xd8")
    opaque_preview = _decoded(opaque_bytes)
    assert opaque_preview.size == (7, 5)
    assert opaque_preview.getpixel((3, 2))[0] > 180

    alpha_path = tmp_path / "alpha.jxl"
    alpha_image = Image.new("RGBA", (4, 3), (20, 80, 200, 255))
    alpha_image.putpixel((1, 1), (230, 10, 40, 64))
    _save_jxl(alpha_path, alpha_image)
    alpha_bytes, alpha_mime = make_jxl_preview(alpha_path)

    assert alpha_mime == "image/png"
    alpha_preview = _decoded(alpha_bytes)
    assert alpha_preview.mode == "RGBA"
    assert alpha_preview.getpixel((1, 1))[3] == 64
    assert alpha_preview.getpixel((0, 0))[3] == 255


def test_generated_jxl_thumbnail_reports_configured_format_and_pixels(tmp_path, monkeypatch):
    import thumbnails

    asset = tmp_path / "fixture.jxl"
    _save_jxl(asset, Image.new("RGB", (12, 8), (30, 150, 70)))
    cache_db = tmp_path / "thumbs.db"

    monkeypatch.setattr(thumbnails, "storage_lifecycle_lock", lambda *_a, **_k: threading.Lock())
    monkeypatch.setattr(thumbnails, "asset_path_for", lambda *_a, **_k: asset)
    monkeypatch.setattr(thumbnails, "thumbnail_db_path", lambda *_a, **_k: cache_db)
    monkeypatch.setattr(thumbnails, "read_thumbnail", lambda *_a, **_k: None)
    monkeypatch.setattr(thumbnails, "write_thumbnail", lambda *_a, **_k: True)
    monkeypatch.setattr(thumbnails, "_configured_format", lambda: "png")

    data, mime_type = thumbnails.ensure_thumbnail(
        "a" * 64,
        ".jxl",
        "image/jxl",
        storage_id="fixture-storage",
        ctx=object(),
    )

    assert mime_type == "image/png"
    preview = _decoded(data)
    assert preview.size == (12, 8)
    red, green, blue = preview.convert("RGB").getpixel((5, 4))
    assert abs(red - 30) <= 1
    assert abs(green - 150) <= 1
    assert abs(blue - 70) <= 1


def test_default_accepted_media_includes_jxl():
    from config_schema import AcceptedMedia

    defaults = AcceptedMedia()

    assert ".jxl" in defaults.extensions
    assert "image/jxl" in defaults.mime_types


def test_legacy_default_media_allowlist_gains_jxl_but_custom_list_is_preserved():
    from config_schema import IngestionSettings

    old_defaults = {"extensions": [".jpg", ".jpeg", ".png", ".webp", ".mp4"],
                    "mime_types": ["image/jpeg", "image/png", "image/webp", "video/mp4"]}
    assert ".jxl" in IngestionSettings.model_validate({"accepted_media": old_defaults}).accepted_media.extensions
    assert ".jxl" not in IngestionSettings.model_validate({
        "accepted_media": old_defaults, "processing": {"thumbnail_format": "jpeg"}
    }).accepted_media.extensions
    assert ".jxl" not in IngestionSettings.model_validate({
        "accepted_media": {**old_defaults, "extensions": [".jpg"]}
    }).accepted_media.extensions
