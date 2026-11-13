"""Decode JPEG XL images and encode browser-friendly full-size previews."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore


_PREVIEW_SLOTS = BoundedSemaphore(2)


def register_jxl_decoder() -> None:
    # Import registers the decoder with Pillow; unrelated media need not load it.
    import pillow_jxl  # noqa: F401


def make_jxl_preview(source: Path) -> tuple[bytes, str]:
    """Return (encoded preview bytes, MIME type) for a JPEG XL image.

    JPEG is used for opaque images (quality 90, 4:4:4 chroma); PNG is used
    only when the decoded image contains at least one non-opaque pixel.
    """
    # Rapid focus changes can start overlapping full-size decodes.
    with _PREVIEW_SLOTS:
        return _encode_preview(source)


def _encode_preview(source: Path) -> tuple[bytes, str]:
    register_jxl_decoder()
    from PIL import Image, ImageOps

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()

    has_alpha = (
        "A" in image.getbands()
        or (image.mode == "P" and "transparency" in image.info)
    )
    rgba = image.convert("RGBA") if has_alpha else None
    if rgba is not None and rgba.getchannel("A").getextrema()[0] < 255:
        output = BytesIO()
        rgba.save(output, format="PNG")
        return output.getvalue(), "image/png"

    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=90,
        subsampling=0,
    )
    return output.getvalue(), "image/jpeg"
