from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from PIL import Image


@dataclass(frozen=True)
class Prediction:
    name: str
    category: str
    score: float | None = None
    display_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class TaggerBackend(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def warning(self) -> str: ...

    def predict(self, image: Image.Image, thresholds: dict[str, float]) -> list[Prediction]: ...

    def close(self) -> None: ...


@dataclass
class TagResult:
    hash: str
    status: str
    model: str
    threshold: float
    created_at: str
    rating: dict[str, Any] | None
    character_tags: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    provider: str = ""
    device: str = ""
    max_tags: int = 0
    error: str = ""
    media_type: str = "image"
    sampled_frames: list[dict[str, Any]] = field(default_factory=list)
    frame_count: int = 0
    backend: str = "wd"
    groups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    output_profile: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 2

    @property
    def item_hash(self) -> str:
        return self.hash

    def to_dict(self) -> dict:
        return asdict(self)
