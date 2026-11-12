
import json
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image


_model = None

def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer('clip-ViT-B-32')
    return _model

def is_silent(video_path: Path, threshold_db: float = -60.0) -> bool:

    try:
        cmd = [
            'ffmpeg', '-i', str(video_path),
            '-af', 'volumedetect',
            '-vn', '-sn', '-dn',
            '-f', 'null', '-'
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=60,
        )


        import re
        match = re.search(r"max_volume:\s+(-?\d+\.\d*)\s+dB", result.stderr)

        if match:
            max_vol = float(match.group(1))
            return max_vol <= threshold_db


        return True
    except Exception:
        from logger import log_system
        log_system("WARNING", "Video silence detection failed", file=str(video_path), exc_info=True)
        return True

def get_audio_fingerprint(video_path: Path) -> bytes:

    try:

        if is_silent(video_path):
            return b''


        result = subprocess.run(
            ['fpcalc', '-raw', '-json', '-length', '120', str(video_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=120
        )

        if result.stdout:
            data = json.loads(result.stdout)
            raw_fp = data.get('fingerprint', [])
            if raw_fp:
                import numpy as np

                return np.array(raw_fp, dtype=np.int32).tobytes()
        return b''
    except Exception:
        from logger import log_system
        log_system("WARNING", "Audio fingerprint failed", file=str(video_path), exc_info=True)
        return b''

def get_video_duration(video_path: Path) -> float:

    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30
        )
        if probe.returncode != 0:
            from logger import log_system
            log_system("WARNING", "ffprobe failed - possibly corrupt or unsupported video",
                       file=str(video_path.name), stderr=probe.stderr.strip()[:200])
            return 0.0
        duration_str = probe.stdout.strip()
        return float(duration_str) if duration_str and duration_str != 'N/A' else 0.0
    except Exception:
        from logger import log_system
        log_system("WARNING", "Video duration probe failed", file=str(video_path), exc_info=True)
        return 0.0

def sample_video_timestamps(duration: float, frame_count: int = 5) -> list[float]:

    if duration <= 0:
        return []
    frame_count = max(1, int(frame_count or 5))
    if frame_count == 5:
        ratios = [0.10, 0.30, 0.50, 0.70, 0.90]
    else:
        step = 0.80 / max(1, frame_count - 1)
        ratios = [0.10 + (step * index) for index in range(frame_count)]
    return [max(0.0, min(duration, duration * ratio)) for ratio in ratios]

def extract_video_frame(video_path: Path, timestamp: float) -> Image.Image:

    try:
        frame_cmd = [
            'ffmpeg', '-y', '-ss', f"{timestamp:.3f}", '-i', str(video_path),
            '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'
        ]
        frame_proc = subprocess.run(
            frame_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=60,
        )
        return Image.open(BytesIO(frame_proc.stdout)).convert('RGB')
    except Exception as exc:
        from logger import log_system
        log_system("WARNING", "Video frame extraction failed", file=str(video_path), timestamp=timestamp, error=str(exc))
        return None

def extract_sampled_video_frames(video_path: Path, frame_count: int = 5) -> list[tuple[float, Image.Image]]:

    duration = get_video_duration(video_path)
    timestamps = sample_video_timestamps(duration, frame_count)
    if not timestamps:
        return []

    frames = []
    try:
        with tempfile.TemporaryDirectory(prefix="lmz_frames_") as tmp:
            tmp_dir = Path(tmp)
            cmd = ['ffmpeg', '-y', '-loglevel', 'error']
            for timestamp in timestamps:
                cmd.extend(['-ss', f"{timestamp:.3f}", '-i', str(video_path)])
            frame_paths = []
            for index in range(len(timestamps)):
                frame_path = tmp_dir / f"frame_{index:03d}.png"
                frame_paths.append(frame_path)
                cmd.extend(['-map', f"{index}:v:0", '-frames:v', '1', str(frame_path)])

            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=90,
            )
            for timestamp, frame_path in zip(timestamps, frame_paths):
                if not frame_path.exists():
                    continue
                with Image.open(frame_path) as image:
                    frames.append((timestamp, image.convert('RGB').copy()))
    except Exception as exc:
        from logger import log_system
        log_system("WARNING", "Sampled video frame extraction failed", file=str(video_path), error=str(exc))
    return frames

def get_visual_embedding(video_path: Path) -> bytes:

    try:
        model = get_model()
        vectors = []

        import numpy as np
        for _, img in extract_sampled_video_frames(video_path, 5):
            vectors.append(model.encode(img))

        if not vectors:
            return b''

        avg_vector = np.mean(vectors, axis=0)


        norm = np.linalg.norm(avg_vector)
        if norm > 0:
            avg_vector = avg_vector / norm


        return avg_vector.astype(np.float32).tobytes()

    except Exception:
        from logger import log_system
        log_system("WARNING", "Video visual embedding failed", file=str(video_path), exc_info=True)
        return b''

def compare_audio_fingerprints(fp1_bytes: bytes, fp2_bytes: bytes) -> float:

    if not fp1_bytes or not fp2_bytes:
        return 0.0

    if fp1_bytes == fp2_bytes:
        return 1.0

    import numpy as np

    a = np.frombuffer(fp1_bytes, dtype=np.int32)
    b = np.frombuffer(fp2_bytes, dtype=np.int32)


    length = min(len(a), len(b))
    if length == 0:
        return 0.0

    a = a[:length]
    b = b[:length]


    xor_res = a.astype(np.uint32) ^ b.astype(np.uint32)


    diff_bits = int(np.bitwise_count(xor_res).sum())
    total_bits = length * 32

    return 1.0 - (diff_bits / total_bits)

def compare_embeddings(emb1_bytes: bytes, emb2_bytes: bytes) -> float:

    if not emb1_bytes or not emb2_bytes:
        return 0.0

    import numpy as np
    emb1 = np.frombuffer(emb1_bytes, dtype=np.float32)
    emb2 = np.frombuffer(emb2_bytes, dtype=np.float32)

    if len(emb1) != len(emb2) or len(emb1) == 0:
        return 0.0

    dot_product = np.dot(emb1, emb2)
    norm1 = np.linalg.norm(emb1)
    norm2 = np.linalg.norm(emb2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)
