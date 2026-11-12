"""Kasanın SQLite şemasını, medya kayıtlarını ve arama imzalarını yönetir."""

import sqlite3
import threading
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from utils import utc_now_str
from runtime_context import WorkspaceContext, get_runtime_context

_SCHEMA_READY_PATHS: set[Path] = set()
_SCHEMA_LOCK = threading.Lock()
STORAGE_ID_WIDTH = 12
STORAGE_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _active_db_path(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.db_path.resolve()


def int_to_storage_id(value: int) -> str:
    """Negatif olmayan sayacı 12 karakterlik, sıfır dolgulu base36 kimliğe çevir."""
    value = int(value)
    if value < 0:
        raise ValueError("storage id value must be non-negative")
    if value == 0:
        encoded = "0"
    else:
        chars = []
        base = len(STORAGE_ID_ALPHABET)
        while value:
            value, remainder = divmod(value, base)
            chars.append(STORAGE_ID_ALPHABET[remainder])
        encoded = "".join(reversed(chars))
    if len(encoded) > STORAGE_ID_WIDTH:
        raise ValueError("storage id counter exceeded 12 base36 characters")
    return encoded.rjust(STORAGE_ID_WIDTH, "0")


def storage_id_to_int(storage_id: str) -> int | None:
    """Base36 kimliği sayıya çevir; boş veya tanınmayan karakterler varsa None ver."""
    text = str(storage_id or "").strip().lower()
    if not text or any(ch not in STORAGE_ID_ALPHABET for ch in text):
        return None
    value = 0
    base = len(STORAGE_ID_ALPHABET)
    for ch in text:
        value = value * base + STORAGE_ID_ALPHABET.index(ch)
    return value


def normalize_source_url(url: str) -> str:
    """URL karşılaştırması için fragment'ı kaldır ve sorgu parametrelerini sırala."""

    if not url:
        return ""
    raw = str(url).strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.lower()
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))

def init_database(db_path: Path | None = None, ctx: WorkspaceContext | None = None):
    """SQLite'ı aç, eksik şema ve eski satırlar için gerekli güncellemeleri uygula.

    Şema işlemleri dönüşten önce commit edilir. Dönen açık bağlantıyı
    kapatmak çağıranın sorumluluğudur.
    """
    target_path = Path(db_path or _active_db_path(ctx)).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=5)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA foreign_keys = ON;')


    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            hash TEXT PRIMARY KEY,
            original_filename TEXT,
            file_extension TEXT,
            mime_type TEXT,
            size_bytes INTEGER,
            date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
            source_url TEXT,
            source_url_norm TEXT,
            platform TEXT,
            source_artist TEXT,
            phash TEXT,
            audio_hash BLOB,
            visual_embedding BLOB,
            width INTEGER,
            height INTEGER,
            storage_id TEXT UNIQUE,
            thumbnail_status TEXT DEFAULT 'pending',
            thumbnail_error TEXT
        )
    ''')


    cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_tiles (
            parent_hash TEXT,
            tile_index INTEGER,
            tile_phash TEXT,
            UNIQUE(parent_hash, tile_index),
            FOREIGN KEY(parent_hash) REFERENCES items(hash) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_tile_phash ON item_tiles(tile_phash)')


    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source_url ON items(source_url)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_date_hash ON items(date_added DESC, hash DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_date_hash_oldest ON items(date_added ASC, hash ASC)')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_artist_date ON items(COALESCE(source_artist, '') COLLATE NOCASE ASC, date_added DESC, hash DESC)")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_platform ON items(platform)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_source_artist ON items(source_artist)')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_source_artist_norm ON items(LOWER(TRIM(source_artist)))")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_platform_norm ON items(LOWER(TRIM(platform)))")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_source_artist_norm_date ON items(LOWER(TRIM(source_artist)), date_added DESC, hash DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_platform_norm_date ON items(LOWER(TRIM(platform)), date_added DESC, hash DESC)")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_mime_date ON items(mime_type, date_added DESC, hash DESC)')


    cursor.execute("PRAGMA table_info(items)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'audio_hash' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN audio_hash BLOB")
    if 'visual_embedding' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN visual_embedding BLOB")
    if 'width' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN width INTEGER")
    if 'height' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN height INTEGER")
    if 'source_url_norm' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN source_url_norm TEXT")
    if 'storage_id' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN storage_id TEXT")
    if 'thumbnail_status' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN thumbnail_status TEXT DEFAULT 'pending'")
    if 'thumbnail_error' not in columns:
        cursor.execute("ALTER TABLE items ADD COLUMN thumbnail_error TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS storage_id_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_value INTEGER NOT NULL
        )
    ''')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_items_storage_id ON items(storage_id)')
    _ensure_storage_counter(conn)
    _backfill_missing_storage_ids(conn)

    cursor.execute('SELECT hash, source_url FROM items WHERE source_url IS NOT NULL AND source_url != "" AND (source_url_norm IS NULL OR source_url_norm = "")')
    rows = cursor.fetchall()
    cursor.executemany(
        'UPDATE items SET source_url_norm = ? WHERE hash = ?',
        [(normalize_source_url(source_url), file_hash) for file_hash, source_url in rows]
    )
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source_url_norm ON items(source_url_norm)')

    from metadata_index import ensure_metadata_schema
    ensure_metadata_schema(conn)
    from artists import ensure_artist_schema
    ensure_artist_schema(conn, backfill=False)
    from platforms import ensure_platform_schema
    ensure_platform_schema(conn, backfill=False)

    conn.commit()
    _SCHEMA_READY_PATHS.add(target_path)
    return conn


def connect_database(ctx: WorkspaceContext | None = None):
    """Kasa veritabanına bağlan; süreçte ilk erişimde veya dosya yoksa şemayı kur."""
    target_path = _active_db_path(ctx)
    if target_path not in _SCHEMA_READY_PATHS or not target_path.exists():
        with _SCHEMA_LOCK:
            if target_path not in _SCHEMA_READY_PATHS or not target_path.exists():
                return init_database(target_path)
    conn = sqlite3.connect(target_path, timeout=5)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA foreign_keys = ON;')
    return conn

def check_duplicate_hash(conn: sqlite3.Connection, file_hash: str) -> bool:

    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM items WHERE hash = ?', (file_hash,))
    return cursor.fetchone() is not None


def _ensure_storage_counter(conn: sqlite3.Connection):
    cursor = conn.cursor()
    row = cursor.execute("SELECT next_value FROM storage_id_counter WHERE id = 1").fetchone()
    max_value = 0
    for (storage_id,) in cursor.execute('SELECT storage_id FROM items WHERE storage_id IS NOT NULL AND storage_id != ""'):
        parsed = storage_id_to_int(storage_id)
        if parsed is not None:
            max_value = max(max_value, parsed)
    next_value = max_value + 1
    if row is None:
        cursor.execute("INSERT INTO storage_id_counter(id, next_value) VALUES (1, ?)", (next_value,))
    elif int(row[0]) < next_value:
        cursor.execute("UPDATE storage_id_counter SET next_value = ? WHERE id = 1", (next_value,))


def allocate_storage_id(conn: sqlite3.Connection) -> str:
    """Kullanılmayan sıradaki kimliği ayır; commit işlemini çağırana bırak."""
    cursor = conn.cursor()
    while True:
        # UPDATE sayacı ilerletir; RETURNING - 1 aynı sorguda ayrılan eski değeri verir.
        row = cursor.execute(
            "UPDATE storage_id_counter SET next_value = next_value + 1 WHERE id = 1 RETURNING next_value - 1"
        ).fetchone()
        if row is None:
            _ensure_storage_counter(conn)
            continue
        next_value = int(row[0] if row else 1)
        storage_id = int_to_storage_id(next_value)
        exists = cursor.execute("SELECT 1 FROM items WHERE storage_id = ?", (storage_id,)).fetchone()
        if not exists:
            return storage_id


def _backfill_missing_storage_ids(conn: sqlite3.Connection):
    cursor = conn.cursor()
    rows = cursor.execute(
        'SELECT hash FROM items WHERE storage_id IS NULL OR storage_id = "" ORDER BY date_added ASC, hash ASC'
    ).fetchall()
    if rows:
        _ensure_storage_counter(conn)
    for (file_hash,) in rows:
        cursor.execute("UPDATE items SET storage_id = ? WHERE hash = ?", (allocate_storage_id(conn), file_hash))


def storage_id_for_hash(conn: sqlite3.Connection, file_hash: str) -> str | None:
    """İçerik hash'ini dosya adlarında kullanılan kimliğe eşle; yoksa None ver."""
    row = conn.execute("SELECT storage_id FROM items WHERE hash = ?", (file_hash,)).fetchone()
    if not row:
        return None
    return row[0] or None

def get_all_phashes(conn: sqlite3.Connection) -> list[tuple[str, str]]:

    cursor = conn.cursor()
    cursor.execute('SELECT hash, phash FROM items WHERE phash IS NOT NULL AND phash != ""')
    return cursor.fetchall()

def get_all_urls(conn: sqlite3.Connection) -> list[str]:

    cursor = conn.cursor()
    cursor.execute('SELECT source_url FROM items WHERE source_url IS NOT NULL AND source_url != ""')
    return [row[0] for row in cursor.fetchall()]

def get_all_tiles(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:

    cursor = conn.cursor()
    cursor.execute('SELECT parent_hash, tile_index, tile_phash FROM item_tiles')
    return cursor.fetchall()

def insert_tiles(conn: sqlite3.Connection, parent_hash: str, tiles: list[tuple[int, str]]):
    """Boş olmayan parça listesini eskisinin yerine yaz; commit etme."""

    if not tiles:
        return

    cursor = conn.cursor()

    cursor.execute('DELETE FROM item_tiles WHERE parent_hash = ?', (parent_hash,))

    cursor.executemany('''
        INSERT INTO item_tiles (parent_hash, tile_index, tile_phash)
        VALUES (?, ?, ?)
    ''', [(parent_hash, index, phash) for index, phash in tiles])

def get_all_video_signatures(conn: sqlite3.Connection) -> list[tuple[str, bytes, bytes]]:
    """En az bir imzası bulunan videoların hash, ses ve görsel imzalarını getir."""

    cursor = conn.cursor()
    cursor.execute('SELECT hash, audio_hash, visual_embedding FROM items WHERE audio_hash IS NOT NULL OR visual_embedding IS NOT NULL')
    return cursor.fetchall()

def reset_database(ctx: WorkspaceContext | None = None):
    """Kasa kayıt tablolarını silip şemayı yeniden kur; medya dosyalarını silme."""

    target_path = _active_db_path(ctx)
    conn = sqlite3.connect(target_path, timeout=5)
    try:
        cursor = conn.cursor()
        cursor.execute('PRAGMA foreign_keys = ON;')
        cursor.execute('BEGIN IMMEDIATE')
        cursor.execute('DROP TABLE IF EXISTS metadata_dirty_queue')
        cursor.execute('DROP TABLE IF EXISTS metadata_facet_counts')
        cursor.execute('DROP TABLE IF EXISTS item_wd_tags')
        cursor.execute('DROP TABLE IF EXISTS item_topics')
        cursor.execute('DROP TABLE IF EXISTS item_metadata_files')
        cursor.execute('DROP TABLE IF EXISTS metadata_index_state')
        cursor.execute('DROP TABLE IF EXISTS artist_links')
        cursor.execute('DROP TABLE IF EXISTS artist_aliases')
        cursor.execute('DROP TABLE IF EXISTS artists')
        cursor.execute('DROP TABLE IF EXISTS platform_aliases')
        cursor.execute('DROP TABLE IF EXISTS platforms')
        cursor.execute('DROP TABLE IF EXISTS item_tiles')
        cursor.execute('DROP TABLE IF EXISTS storage_id_counter')
        cursor.execute('DROP TABLE IF EXISTS items')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _SCHEMA_READY_PATHS.discard(target_path)
    conn = init_database(target_path)
    try:
        conn.commit()
    finally:
        conn.close()

def check_duplicate_url(conn: sqlite3.Connection, url: str) -> bool:
    """Normalleştirilmiş kaynak URL'sinin kayıtlı olup olmadığını denetle."""

    if not url: return False
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM items WHERE source_url_norm = ?', (normalize_source_url(url),))
    return cursor.fetchone() is not None

def insert_to_database(conn: sqlite3.Connection, filepath: Path, file_hash: str, mime_type: str, target_ext: str, metadata: dict = None, file_size: int = None, timestamp: datetime = None, phash: str = None, audio_hash: bytes = None, visual_embedding: bytes = None, width: int = None, height: int = None, storage_id: str = None) -> str:
    """Medya satırını ekle ve dosya adlarında kullanılacak depolama kimliğini ver.

    Gerekirse artist/platform sözlüğü çalışma alanı veritabanında çözülür
    ve ayrı commit edilir. Kasa bağlantısındaki commit çağırana aittir.
    """
    metadata = metadata or {}
    source_url = metadata.get('source_url', "")
    source_url_norm = normalize_source_url(source_url)
    platform = metadata.get('platform', "")
    source_artist = metadata.get('artist', "")
    if platform or source_artist:
        from workspace_db import connect_workspace_database
        workspace_conn = connect_workspace_database()
        try:
            if platform:
                from platforms import resolve_platform_label
                platform = resolve_platform_label(workspace_conn, platform)
            if source_artist:
                from artists import resolve_artist_name
                source_artist = resolve_artist_name(workspace_conn, source_artist)
            workspace_conn.commit()
        finally:
            workspace_conn.close()

    if file_size is None:
        file_size = filepath.stat().st_size

    if timestamp is None:
        timestamp_value = utc_now_str()
    elif isinstance(timestamp, datetime):
        timestamp_value = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        timestamp_value = str(timestamp)

    cursor = conn.cursor()
    existing_storage = storage_id_for_hash(conn, file_hash)
    storage_id = existing_storage or storage_id or allocate_storage_id(conn)
    cursor.execute('''
        INSERT INTO items
        (hash, original_filename, file_extension, mime_type, size_bytes, date_added, source_url, source_url_norm, platform, source_artist, phash, audio_hash, visual_embedding, width, height, storage_id, thumbnail_status, thumbnail_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
        ON CONFLICT(hash) DO UPDATE SET
            original_filename = excluded.original_filename,
            file_extension = excluded.file_extension,
            mime_type = excluded.mime_type,
            size_bytes = excluded.size_bytes,
            date_added = excluded.date_added,
            source_url = excluded.source_url,
            source_url_norm = excluded.source_url_norm,
            platform = excluded.platform,
            source_artist = excluded.source_artist,
            phash = excluded.phash,
            audio_hash = excluded.audio_hash,
            visual_embedding = excluded.visual_embedding,
            width = excluded.width,
            height = excluded.height,
            storage_id = COALESCE(items.storage_id, excluded.storage_id),
            thumbnail_status = 'pending',
            thumbnail_error = NULL
    ''', (
        file_hash,
        filepath.name,
        target_ext,
        mime_type,
        file_size,
        timestamp_value,
        source_url,
        source_url_norm,
        platform,
        source_artist,
        phash,
        audio_hash,
        visual_embedding,
        width,
        height,
        storage_id
    ))
    return storage_id
