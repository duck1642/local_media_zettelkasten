"""Small, best-effort SQLite cache for generated thumbnails."""

import sqlite3
from contextlib import closing
from pathlib import Path


_BUSY_TIMEOUT_SECONDS = 0.25
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS thumbnails (
    storage_id TEXT PRIMARY KEY,
    source_mtime_ns INTEGER NOT NULL,
    source_size INTEGER NOT NULL,
    data BLOB NOT NULL,
    mime_type TEXT NOT NULL
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_SECONDS)


def _read_connection(db_path: Path) -> sqlite3.Connection:
    # A read must not create a missing cache or acquire a schema write lock.
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=_BUSY_TIMEOUT_SECONDS)


def read_thumbnail(
    db_path: Path, storage_id: str, source_mtime_ns: int, source_size: int
) -> tuple[bytes, str] | None:
    """Return a cached thumbnail only when its source fingerprint still matches."""
    try:
        with closing(_read_connection(db_path)) as conn:
            row = conn.execute(
                """SELECT data, mime_type FROM thumbnails
                   WHERE storage_id = ? AND source_mtime_ns = ? AND source_size = ?""",
                (storage_id, source_mtime_ns, source_size),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0]), str(row[1])
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None


def write_thumbnail(
    db_path: Path,
    storage_id: str,
    source_mtime_ns: int,
    source_size: int,
    data: bytes,
    mime_type: str,
) -> bool:
    """Store or replace a thumbnail; return False if the cache is unavailable."""
    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(path)) as conn:
            with conn:
                conn.execute(_CREATE_TABLE)
                conn.execute(
                    """INSERT INTO thumbnails
                       (storage_id, source_mtime_ns, source_size, data, mime_type)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(storage_id) DO UPDATE SET
                         source_mtime_ns = excluded.source_mtime_ns,
                         source_size = excluded.source_size,
                         data = excluded.data,
                         mime_type = excluded.mime_type""",
                    (storage_id, source_mtime_ns, source_size, sqlite3.Binary(data), mime_type),
                )
        return True
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return False


def delete_thumbnail(db_path: Path, storage_id: str) -> bool:
    """Remove one cache entry, ignoring unavailable or malformed cache files."""
    try:
        if not db_path.is_file():
            return False
        with closing(_connect(db_path)) as conn:
            with conn:
                return conn.execute("DELETE FROM thumbnails WHERE storage_id = ?", (storage_id,)).rowcount > 0
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return False


def list_thumbnail_fingerprints(db_path: Path) -> dict[str, tuple[int, int]]:
    try:
        with closing(_read_connection(db_path)) as conn:
            return {key: (mtime, size) for key, mtime, size in conn.execute(
                "SELECT storage_id, source_mtime_ns, source_size FROM thumbnails"
            )}
    except (sqlite3.Error, OSError):
        return {}
