"""Farklı kurulum akışlarının kullandığı ortak kasa dizin yapısı."""

from pathlib import Path


VAULT_LOCAL_DIRS = (
    "vault/notes",
    "vault/assets",
    "db",
    "logs/raw",
    "logs/structured",
    "review",
    "wd-tags",
    "ui_cache/thumbnails",
    "queues",
    "batches",
    "input",
    "local_ingest",
    "online_ingest",
)


def ensure_vault_directories(root: Path) -> list[Path]:
    """Eksik kasa dizinlerini oluştur ve yollarını sabit sırada döndür.

    Mevcut dizinlere dokunmaz ve SQLite şemasını başlatmaz.
    """
    paths = [root / relative for relative in VAULT_LOCAL_DIRS]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths
