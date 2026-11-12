"""WD's ONNX format, preprocessing and category decoding."""

import csv
from pathlib import Path

import numpy as np
from PIL import Image

from tagging.contracts import Prediction


class WdTagger:
    """WD ONNX modelini, CUDA kullanılamazsa CPU'ya düşebilecek şekilde çalıştırır."""

    def __init__(self, model: str, device: str, models_dir: Path, local_path: str = ""):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        model_path, tags_path = _ensure_model_files(model, hf_hub_download, models_dir, local_path)
        self.labels = _load_labels(tags_path)
        providers, self._warning = _providers_for_device(device, ort)
        self.session = ort.InferenceSession(str(model_path), providers=providers)
        self.input_meta = self.session.get_inputs()[0]
        self.output_meta = self.session.get_outputs()[0]
        if (
            providers[0] == "CUDAExecutionProvider"
            and "CUDAExecutionProvider" not in self.session.get_providers()
        ):
            self._warning = "CUDA initialization failed; using CPUExecutionProvider"

    @property
    def provider(self) -> str:
        providers = self.session.get_providers() if self.session is not None else []
        return providers[0] if providers else ""

    @property
    def warning(self) -> str:
        return self._warning

    def predict(self, image: Image.Image, thresholds: dict[str, float]) -> list[Prediction]:
        """Görseli etiketle; çalışma sağlayıcısı değişirse bunu uyarı olarak sakla."""
        before = self.provider
        result = _predict_image_tags(
            self.session, self.input_meta, self.output_meta, self.labels, image, thresholds
        )
        if before != self.provider:
            self._warning = f"Execution provider changed from {before} to {self.provider}"
        return result

    def close(self) -> None:
        self.session = None


def _ensure_model_files(model_repo: str, download, models_dir: Path, local_path: str = ""):
    """ONNX ve CSV yollarını seç; açıkça verilen yerel dizinde eksik dosyayı indirme."""
    if local_path:
        model_dir = Path(local_path).expanduser().resolve()
        if not all((model_dir / name).is_file() for name in ("model.onnx", "selected_tags.csv")):
            raise FileNotFoundError(f"WD model files missing from {model_dir}")
    else:
        legacy = models_dir / model_repo.split("/")[-1]
        if model_repo == "SmilingWolf/wd-vit-tagger-v3" and all(
            (legacy / name).is_file() for name in ("model.onnx", "selected_tags.csv")
        ):
            model_dir = legacy
        else:
            model_dir = models_dir / "wd" / model_repo.replace("/", "--")
            model_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename in ("model.onnx", "selected_tags.csv"):
        path = model_dir / filename
        if not path.is_file():
            path = Path(download(repo_id=model_repo, filename=filename, local_dir=str(model_dir)))
        paths.append(path)
    return tuple(paths)


def _load_labels(tags_path: Path) -> list[dict[str, str]]:
    with tags_path.open("r", encoding="utf-8-sig", newline="") as handle:
        labels = [dict(row) for row in csv.DictReader(handle) if row.get("name")]
    if not labels:
        raise ValueError("WD tag list is empty")
    return labels


def _providers_for_device(device: str, ort):
    if device == "cpu":
        return ["CPUExecutionProvider"], ""
    if "CUDAExecutionProvider" in ort.get_available_providers():
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], ""
    warning = (
        "CUDAExecutionProvider unavailable; using CPUExecutionProvider" if device == "cuda" else ""
    )
    return ["CPUExecutionProvider"], warning


def _predict_image_tags(session, input_meta, output_meta, labels, image, thresholds):
    array = _prepare_pil_image(image, input_meta.shape)
    scores = session.run([output_meta.name], {input_meta.name: array})[0][0]
    if len(labels) != len(scores):
        raise ValueError(
            f"WD label/prediction count mismatch: {len(labels)} labels, {len(scores)} predictions"
        )
    categories = {"0": "general", "4": "character", "9": "rating"}
    predictions = []
    for label, score in zip(labels, scores):
        category = categories.get(
            str(label.get("category")), f"wd_{label.get('category', 'unknown')}"
        )
        if np.isfinite(score) and score >= thresholds.get(category, 0.35):
            predictions.append(Prediction(label["name"], category, round(float(score), 6)))
    return predictions


def _prepare_pil_image(image: Image.Image, input_shape) -> np.ndarray:
    """Şeffaflığı beyaza birleştir, kareye tamamla ve ONNX için BGR tensörü oluştur."""
    target_size = next(
        (value for value in reversed(input_shape) if isinstance(value, int) and value > 3), 448
    )
    image = image.convert("RGBA")
    canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
    canvas.alpha_composite(image)
    image = canvas.convert("RGB")
    width, height = image.size
    max_dim = max(width, height)
    padded = Image.new("RGB", (max_dim, max_dim), (255, 255, 255))
    padded.paste(image, ((max_dim - width) // 2, (max_dim - height) // 2))
    if max_dim != target_size:
        padded = padded.resize((target_size, target_size), Image.Resampling.BICUBIC)
    array = np.ascontiguousarray(np.asarray(padded, dtype=np.float32)[:, :, ::-1])
    if len(input_shape) == 4 and input_shape[1] == 3:
        array = np.transpose(array, (2, 0, 1))
    return np.expand_dims(array, axis=0)
