"""Etiketleme servisinin diğer backend modüllerine açtığı ortak giriş noktası."""

from tagging.contracts import Prediction, TaggerBackend, TagResult
from tagging.registry import describe_models
from tagging.service import load_tag_cache, tag_media, unload_models, wd_frontmatter_fields

__all__ = [
    "Prediction",
    "TagResult",
    "TaggerBackend",
    "describe_models",
    "load_tag_cache",
    "tag_media",
    "unload_models",
    "wd_frontmatter_fields",
]
