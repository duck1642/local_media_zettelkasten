
from pathlib import Path
from typing import Optional

_EXTENSION_MIME_MAP = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.jfif': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.jxl': 'image/jxl',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.ogv': 'video/ogg',
}

def _mime_from_extension(filepath: Path) -> Optional[str]:
    return _EXTENSION_MIME_MAP.get(filepath.suffix.lower())

def get_mime_type(filepath: Path) -> Optional[str]:
    if filepath.suffix.lower() == ".jxl":
        # Older libmagic builds do not recognize either JPEG XL signature.
        try:
            with filepath.open("rb") as source:
                signature = source.read(12)
        except OSError:
            return None
        if signature.startswith(b"\xff\x0a") or signature == b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a":
            return "image/jxl"
        return None

    try:
        import magic
        mime = magic.from_file(str(filepath), mime=True)
        normalized = mime.lower().strip() if mime else ""
        if "/" in normalized:
            return normalized
        return _mime_from_extension(filepath)

    except Exception:

        return _mime_from_extension(filepath)

def is_allowed_mime(mime_type: str, allowed_list: list) -> bool:

    return mime_type in set(allowed_list or [])
