"""The built-in adapters and their output descriptions. Imports stay lazy."""

from pathlib import Path

from tagging.contracts import TaggerBackend

MODEL_DESCRIPTORS = {
    "pixai": {
        "id": "pixai",
        "name": "PixAI Tagger v1.0",
        "recommended": True,
        "default_repo": "pixai-labs/pixai-tagger-v1.0",
        "thresholds": {
            "general": 0.17,
            "character": 0.27,
            "copyright": 0.24,
            "style": 0.15,
            "meta": 0.17,
            "rating": 0.41,
        },
    },
    "wd": {
        "id": "wd",
        "name": "WD ViT Tagger v3",
        "recommended": False,
        "default_repo": "SmilingWolf/wd-vit-tagger-v3",
        "thresholds": {"general": 0.35, "character": 0.35, "rating": 0.0},
    },
}

_BACKENDS: dict[tuple, TaggerBackend] = {}


def describe_models() -> list[dict]:
    """Ayar ekranı için model listesini çıkarım kütüphanelerini yüklemeden ver."""
    return [
        dict(value, categories=list(value["thresholds"])) for value in MODEL_DESCRIPTORS.values()
    ]


def resolve_model(config: dict) -> tuple[str, str, dict[str, float], dict]:
    """Adaptör, model deposu, etkili eşikler ve çıktı profilini ayarlardan seç.

    Adaptör bilgisi olmayan eski çağrılar WD kullanır. Profil eşikleri
    model varsayılanlarının ve eski WD eşiğinin üzerine uygulanır.
    """
    # Raw settings from older callers did not include a backend discriminator.
    backend = config.get("backend") or "wd"
    if backend not in MODEL_DESCRIPTORS:
        raise ValueError(f"Unsupported tagging backend: {backend}")
    descriptor = MODEL_DESCRIPTORS[backend]
    model = config.get("model_repo") or descriptor["default_repo"]
    thresholds = dict(descriptor["thresholds"])
    if backend == "wd":
        thresholds.update(
            {key: float(config.get("threshold", 0.35)) for key in ("general", "character")}
        )
    profile = (config.get("profiles") or {}).get(backend) or {}
    thresholds.update(profile.get("thresholds") or {})
    return backend, model, thresholds, profile


def get_backend(
    backend: str, model: str, device: str, models_dir: Path, local_path: str = ""
) -> TaggerBackend:
    """Aynı ayarlarla modeli yeniden kullan; ayarlar değişirse eski modeli kapat."""
    key = (
        backend,
        model,
        device,
        str(models_dir.resolve()),
        str(Path(local_path).resolve()) if local_path else "",
    )
    if key not in _BACKENDS:
        unload_models()
        if backend == "wd":
            from tagging.wd import WdTagger

            loaded = WdTagger(model, device, models_dir, local_path)
        elif backend == "pixai":
            from tagging.pixai import PixaiTagger

            loaded = PixaiTagger(model, device, models_dir, local_path)
        else:
            raise ValueError(f"Unsupported tagging backend: {backend}")
        _BACKENDS[key] = loaded
    return _BACKENDS[key]


def unload_models() -> None:
    for backend in _BACKENDS.values():
        backend.close()
    _BACKENDS.clear()
