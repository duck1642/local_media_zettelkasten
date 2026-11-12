"""PixAI's official Transformers pipeline, loaded only when selected."""

import gc
import math
from pathlib import Path

from PIL import Image

from tagging.contracts import Prediction


class PixaiTagger:
    """PixAI Transformers modelini PyTorch CPU veya CUDA üzerinde çalıştırır."""

    def __init__(self, model: str, device: str, models_dir: Path, local_path: str = ""):
        """Yerel model klasörünü tercih et; açık CUDA isteğinde CPU'ya düşme."""
        try:
            import timm  # noqa: F401 -- required by the official model code
            import torch
            import torchvision  # noqa: F401
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                'PixAI dependencies are missing. Install LMZ with: pip install ".[pixai]"'
            ) from exc
        from huggingface_hub import snapshot_download

        self._torch = torch
        cuda = torch.cuda.is_available()
        if device == "cuda" and not cuda:
            raise RuntimeError("PixAI CUDA was requested but PyTorch cannot access CUDA")
        self._device = "cuda" if device != "cpu" and cuda else "cpu"
        if local_path:
            model_dir = Path(local_path).expanduser().resolve()
        else:
            model_dir = models_dir / "pixai" / model.replace("/", "--")
            required = (
                "config.json",
                "preprocessor_config.json",
                "model.safetensors",
                "tagger_pipeline.py",
            )
            if not all((model_dir / name).is_file() for name in required):
                model_dir = Path(
                    snapshot_download(
                        repo_id=model,
                        local_dir=str(model_dir),
                        allow_patterns=["*.json", "*.safetensors", "*.py"],
                    )
                )
        if not (model_dir / "config.json").is_file():
            raise FileNotFoundError(f"PixAI model files missing from {model_dir}")
        self.pipeline = pipeline(
            "tagger",
            model=str(model_dir),
            image_processor=str(model_dir),
            trust_remote_code=True,
            device=0 if self._device == "cuda" else -1,
            dtype=torch.float16 if self._device == "cuda" else torch.float32,
            model_kwargs={"local_files_only": True},
        )

    @property
    def provider(self) -> str:
        return f"PyTorch:{self._device}"

    @property
    def warning(self) -> str:
        return ""

    def predict(self, image: Image.Image, thresholds: dict[str, float]) -> list[Prediction]:
        """Görseli RGB olarak modele ver ve kategori skorlarını ortak biçime çevir."""
        with self._torch.inference_mode():
            output = self.pipeline(image.convert("RGB"), threshold=thresholds)
        groups = output.get("results")
        if not isinstance(groups, dict):
            raise TypeError("PixAI returned an unsupported output format")
        predictions = []
        for category, tags in groups.items():
            if not isinstance(tags, dict):
                raise TypeError(f"PixAI category {category!r} is not a tag/score mapping")
            for name, score in tags.items():
                score = float(score)
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError(f"Invalid PixAI score for {name!r}")
                predictions.append(Prediction(name, category, round(score, 6)))
        return predictions

    def close(self) -> None:
        """Pipeline referansını bırak ve CUDA kullanılmışsa boş önbelleği temizle."""
        self.pipeline = None
        gc.collect()
        if self._device == "cuda":
            self._torch.cuda.empty_cache()
