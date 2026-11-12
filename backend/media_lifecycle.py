"""Aynı medya kaydına ait dosya işlemlerini koordine eden yardımcılar."""

import hashlib
import threading
from pathlib import Path

from runtime_context import WorkspaceContext, get_runtime_context


# RLock, aynı iş parçacığının kilidi tekrar almasına izin verir.
# Örneğin etiketleme akışı kilidi tutarken önbellek yazımı da aynı kilidi alabilir.
_LOCK_POOL = tuple(threading.RLock() for _ in range(64))


def storage_lifecycle_lock(
    storage_id: str,
    ctx: WorkspaceContext | None = None,
    vault_root: Path | None = None,
) -> threading.RLock:
    """Kasa kökü ve depolama kimliği için tekrar alınabilir bir kilit seç.

    Kilitler sabit bir havuzdan gelir; farklı kayıtlar aynı kilidi paylaşabilir.
    """
    root = Path(vault_root).resolve() if vault_root is not None else (ctx or get_runtime_context()).active_vault.root.resolve()
    key = f"{root}\0{str(storage_id or '').strip()}"
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=2).digest()
    return _LOCK_POOL[int.from_bytes(digest, "big") % len(_LOCK_POOL)]


def discover_owned_paths(storage_id: str, ctx: WorkspaceContext | None = None) -> list[Path]:
    """Kasanın alt dizinlerinde bu kimliğe ait dosyaları bul; hiçbirini silme."""
    runtime = ctx or get_runtime_context()
    value = str(storage_id or "").strip()
    if not value:
        return []
    vault = runtime.active_vault
    specs = (
        (vault.assets_dir, lambda name: name.startswith(f"{value}.") and len(name) > len(value) + 1),
        (vault.notes_dir, lambda name: name == f"{value}.md"),
        (vault.wd_tags_dir, lambda name: name == f"{value}.json"),
        (vault.thumbnails_dir, lambda name: name in {f"{value}.jpg", f"{value}_video.jpg"}),
    )
    paths: list[Path] = []
    for base_dir, owns_name in specs:
        if not base_dir.exists():
            continue
        for shard_dir in base_dir.iterdir():
            if not shard_dir.is_dir():
                continue
            for candidate in shard_dir.iterdir():
                if candidate.is_file() and owns_name(candidate.name):
                    paths.append(candidate)
    return sorted(set(paths), key=lambda path: str(path))


def remove_stale_derived_files(
    base_dir: Path,
    storage_id: str,
    expected_path: Path,
    candidate_names: set[str],
) -> tuple[int, list[dict]]:
    """Beklenen dosyayı bırakıp diğer alt dizinlerdeki eşleşen kopyaları sil.

    Dönüşte silinen dosya sayısı ve silinemeyen her dosya için hata bilgisi vardır.
    """
    removed = 0
    errors: list[dict] = []
    if not base_dir.exists():
        return removed, errors
    expected = expected_path.resolve()
    for shard_dir in base_dir.iterdir():
        if not shard_dir.is_dir():
            continue
        for name in candidate_names:
            candidate = shard_dir / name
            if not candidate.is_file() or candidate.resolve() == expected:
                continue
            try:
                candidate.unlink()
                removed += 1
            except OSError as exc:
                errors.append({"storage_id": storage_id, "path": str(candidate), "error": str(exc)})
    return removed, errors
