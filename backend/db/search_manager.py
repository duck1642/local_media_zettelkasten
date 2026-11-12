"""SQLite kayıtlarının kasa başına RAM arama indekslerini yönetir."""

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple, Optional

from db.searchers import BKTreeSearcher, URLRegistry
from db.sqlite_operator import get_all_phashes, get_all_tiles, get_all_urls, get_all_video_signatures
from logger import log_system
from runtime_context import WorkspaceContext, get_runtime_context

def _visual_distances(query: bytes, candidates: tuple[tuple[str, bytes], ...]):
    if not candidates:
        return
    import numpy as np

    vector = np.frombuffer(query, dtype=np.float32)
    norm = np.linalg.norm(vector)
    for item_hash, signature in candidates:
        other = np.frombuffer(signature, dtype=np.float32)
        distance = 1.0
        if len(vector) == len(other) and len(vector) and norm != 0:
            other_norm = np.linalg.norm(other)
            if other_norm != 0:
                distance -= float(np.dot(vector, other) / (norm * other_norm))
        yield item_hash, distance


def _audio_distances(query: bytes, candidates: tuple[tuple[str, bytes], ...]):
    from fingerprint import compare_audio_fingerprints

    for item_hash, signature in candidates:
        yield item_hash, 1.0 - compare_audio_fingerprints(query, signature)


def _rank_matches(distances, threshold: float, kind: str) -> list[tuple[str, float, str]]:
    matches = [(item_hash, distance) for item_hash, distance in distances if distance <= threshold]
    matches.sort(key=lambda match: match[1])
    return [(item_hash, 1.0 - distance, kind) for item_hash, distance in matches]


@dataclass
class _SearchIndexState:
    """Tek SQLite dosyasına ait arama yapıları ve bunları koruyan kilit."""

    db_path: Path
    global_tree: BKTreeSearcher = field(default_factory=BKTreeSearcher)
    tile_tree: BKTreeSearcher = field(default_factory=BKTreeSearcher)
    url_registry: URLRegistry = field(default_factory=URLRegistry)
    # Cosine distance and prefix-truncated audio distance are not metrics.
    # Scan them exactly to preserve their existing thresholds and scores.
    video_signatures: dict[str, bytes] = field(default_factory=dict)
    audio_signatures: dict[str, bytes] = field(default_factory=dict)
    is_hydrated: bool = False
    needs_recovery: bool = False
    sync_lock: threading.Lock = field(default_factory=threading.Lock)


class SearchIndexUnavailableError(RuntimeError):
    """SQLite could not restore a consistent search index."""


class SearchManager:
    """Süreçte tek örnek; her kasa için ayrı görsel, video, ses ve URL indeksi."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SearchManager, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._states: dict[Path, _SearchIndexState] = {}
        self._states_lock = threading.Lock()
        self._initialized = True

    def _db_path_from_conn(self, conn: sqlite3.Connection) -> Path | None:
        try:
            rows = conn.execute("PRAGMA database_list").fetchall()
            for row in rows:
                if row[1] == "main" and row[2]:
                    return Path(row[2]).resolve()
        except Exception:
            return None
        return None

    def _state_key(self, ctx: WorkspaceContext | None = None, conn: sqlite3.Connection | None = None) -> Path:
        """İndeksi bağlamdan, bağlantıdan veya son çare olarak aktif kasadan seç."""
        if ctx is not None:
            return ctx.active_vault.db_path.resolve()
        if conn is not None:
            db_path = self._db_path_from_conn(conn)
            if db_path is not None:
                return db_path
        return get_runtime_context().active_vault.db_path.resolve()

    def _state_for(self, ctx: WorkspaceContext | None = None, conn: sqlite3.Connection | None = None) -> _SearchIndexState:
        key = self._state_key(ctx, conn)
        with self._states_lock:
            state = self._states.get(key)
            if state is None:
                state = _SearchIndexState(key)
                self._states[key] = state
            return state

    def reset(self, ctx: WorkspaceContext | None = None):
        """Tek kasanın RAM indeksini at; SQLite kayıtlarını değiştirme."""
        key = self._state_key(ctx)
        with self._states_lock:
            self._states.pop(key, None)

    def reset_all(self):
        """Süreçteki tüm RAM indekslerini at; kasaların dosyalarına dokunma."""
        with self._states_lock:
            self._states.clear()

    @property
    def global_tree(self):
        return self._state_for().global_tree

    @global_tree.setter
    def global_tree(self, value):
        self._state_for().global_tree = value

    @property
    def tile_tree(self):
        return self._state_for().tile_tree

    @tile_tree.setter
    def tile_tree(self, value):
        self._state_for().tile_tree = value

    @property
    def url_registry(self):
        return self._state_for().url_registry

    @url_registry.setter
    def url_registry(self, value):
        self._state_for().url_registry = value

    @property
    def is_hydrated(self):
        return self._state_for().is_hydrated

    @is_hydrated.setter
    def is_hydrated(self, value):
        self._state_for().is_hydrated = bool(value)

    def hydrate(self, conn: sqlite3.Connection, ctx: WorkspaceContext | None = None):
        """İndeksi verilen SQLite bağlantısından yükle; sağlıklı indeks varsa atla."""
        state = self._state_for(ctx, conn)

        with state.sync_lock:
            if state.is_hydrated and not state.needs_recovery:
                return
            self._load_indexes_locked(state, conn)

    def _load_indexes_locked(self, state: _SearchIndexState, conn: sqlite3.Connection):
        """Çağıran kasa kilidini tutarken yeni indeksi kur ve tamamlanınca yayınla."""
        log_system('INFO', "Hydrating RAM indexes from SQLite...")
        replacement = _SearchIndexState(state.db_path)
        urls = get_all_urls(conn)
        for url in urls:
            replacement.url_registry.add(url)

        phashes = get_all_phashes(conn)
        for f_hash, phash in phashes:
            replacement.global_tree.add(f_hash, phash)

        tiles = get_all_tiles(conn)
        for parent_hash, _, tile_phash in tiles:
            replacement.tile_tree.add(parent_hash, tile_phash)

        v_sigs = get_all_video_signatures(conn)
        for f_hash, a_hash, v_emb in v_sigs:
            if a_hash:
                replacement.audio_signatures[f_hash] = a_hash
            if v_emb:
                replacement.video_signatures[f_hash] = v_emb

        # Keep the vault's lock stable. Publish only after every index loads.
        state.global_tree = replacement.global_tree
        state.tile_tree = replacement.tile_tree
        state.url_registry = replacement.url_registry
        state.audio_signatures = replacement.audio_signatures
        state.video_signatures = replacement.video_signatures
        state.is_hydrated = True
        state.needs_recovery = False
        log_system('INFO', f"Hydration complete: {len(urls)} URLs | {len(phashes)} Images | {len(v_sigs)} Videos indexed in RAM.")

    def _ensure_indexes_locked(self, state: _SearchIndexState):
        """Onarım gereken indeksi tek SQLite anlık görüntüsünden geri yükle.

        Okuma başarısızsa eski indeksi kullandırmak yerine
        SearchIndexUnavailableError yükseltir.
        """
        if not state.needs_recovery:
            return
        try:
            # Read one SQLite snapshot across all indexes without creating a DB
            # or consulting whichever vault is active when recovery runs.
            with closing(self._open_read_db(state)) as conn:
                # Salt okunur bağlantıda da BEGIN gerekli: ayrı SELECT'ler,
                # araya başka bir commit girse bile aynı veri sürümünü görsün.
                conn.execute("BEGIN")
                self._load_indexes_locked(state, conn)
        except Exception as exc:
            state.needs_recovery = True
            raise SearchIndexUnavailableError("Similarity index could not be restored from SQLite") from exc

    @staticmethod
    def _open_read_db(state: _SearchIndexState) -> sqlite3.Connection:
        return sqlite3.connect(f"{state.db_path.as_uri()}?mode=ro", uri=True)

    def _recover_after_write_locked(self, state: _SearchIndexState, error: Exception | None = None):
        """Commit sonrası RAM yazımı aksarsa kasayı hemen SQLite'tan yüklemeyi dene.

        Başarısız onarım indeksi onarım bekler halde bırakır. SQLite işlemi
        geri alınmaz; bir sonraki sorgu veya güncelleme onarımı tekrar dener.
        """
        # These deltas follow a successful SQLite commit. RAM failure must not
        # undo it or interrupt unrelated file cleanup.
        state.needs_recovery = True
        if error is not None:
            log_system("WARNING", "RAM index update failed; reloading from SQLite", db_path=str(state.db_path), error=str(error))
        try:
            self._ensure_indexes_locked(state)
        except SearchIndexUnavailableError as exc:
            log_system("ERROR", "RAM index recovery failed", db_path=str(state.db_path), error=str(exc.__cause__))

    def query_image(self, phash: str, threshold: int = 5, ctx: WorkspaceContext | None = None) -> List[Tuple[str, int, str]]:
        """Görselin hash'ini tüm görseller ve kayıtlı görsel parçalarıyla karşılaştır."""
        state = self._state_for(ctx)

        with state.sync_lock:
            self._ensure_indexes_locked(state)
            global_matches = state.global_tree.query(phash, threshold)
            tile_matches = state.tile_tree.query(phash, threshold)

        results = []
        for h, dist in global_matches:
            results.append((h, dist, "Global"))


        for h, dist in tile_matches:
            results.append((h, dist, "Fragment-to-Whole"))

        return results

    def query_video(self, audio_hash: bytes, visual_embedding: bytes, ai_threshold: float = 0.08, audio_threshold: float = 0.85, ctx: WorkspaceContext | None = None) -> List[Tuple[str, float, str]]:
        """Kasanın ses ve görsel imzalarını kilidi tutmadan tam taramayla karşılaştır.

        İmzalar önce kilit altında kopyalanır. Sonuçlar (hash, benzerlik, tür)
        üçlüleridir; ses grubu önce gelir. Görsel eşik uzaklık üst sınırı,
        ses eşiği ise benzerlik alt sınırıdır.
        """
        state = self._state_for(ctx)

        with state.sync_lock:
            self._ensure_indexes_locked(state)
            # Capture only the requested signatures under the lock. Exact scans
            # can be expensive and must not block writes while comparing bytes.
            audio_snapshot = tuple(state.audio_signatures.items()) if audio_hash else ()
            video_snapshot = tuple(state.video_signatures.items()) if visual_embedding else ()

        results = []

        if audio_snapshot:
            results.extend(_rank_matches(_audio_distances(audio_hash, audio_snapshot), 1.0 - audio_threshold, "Sonic"))
        if video_snapshot:
            results.extend(_rank_matches(_visual_distances(visual_embedding, video_snapshot), ai_threshold, "Semantic"))

        return results

    def url_exists(self, url: str, ctx: WorkspaceContext | None = None) -> bool:
        state = self._state_for(ctx)

        with state.sync_lock:
            self._ensure_indexes_locked(state)
            return state.url_registry.exists(url)

    def query_global_only(self, phash: str, threshold: int = 5, ctx: WorkspaceContext | None = None) -> list:
        state = self._state_for(ctx)

        with state.sync_lock:
            self._ensure_indexes_locked(state)
            return state.global_tree.query(phash, threshold)

    def update_indexes(self, file_hash: str, phash: Optional[str], url: Optional[str], tiles: List[Tuple[int, str]] = None, audio_hash: bytes = None, visual_embedding: bytes = None, ctx: WorkspaceContext | None = None, *, conn: sqlite3.Connection | None = None):
        """Commit edilmiş kaydın imzalarını ekle; bağlantı verilirse satırın hâlâ var olduğunu doğrula."""
        state = self._state_for(ctx, conn)

        with state.sync_lock:
            if state.needs_recovery:
                self._recover_after_write_locked(state)
                return
            try:
                if conn is not None and not conn.execute("SELECT 1 FROM items WHERE hash = ?", (file_hash,)).fetchone():
                    return
                self._update_indexes_unlocked(state, file_hash, phash, url, tiles, audio_hash, visual_embedding)
            except Exception as exc:
                self._recover_after_write_locked(state, exc)

    def update_indexes_batch(self, items: list[dict], ctx: WorkspaceContext | None = None):
        """Bekleyen eklemeleri tek kilitle yayınla; SQLite'tan silinmiş kayıtları atla."""

        if not items:
            return
        state = self._state_for(ctx)
        with state.sync_lock:
            if state.needs_recovery:
                self._recover_after_write_locked(state)
                return
            try:
                # Ingest workers can queue a committed row before a concurrent
                # deletion. Do not reintroduce it when this batch is published.
                hashes = list(dict.fromkeys(item.get("file_hash") for item in items if item.get("file_hash")))
                live_hashes = set()
                with closing(self._open_read_db(state)) as conn:
                    for offset in range(0, len(hashes), 900):
                        chunk = hashes[offset:offset + 900]
                        placeholders = ",".join("?" for _ in chunk)
                        live_hashes.update(row[0] for row in conn.execute(f"SELECT hash FROM items WHERE hash IN ({placeholders})", chunk))
                for item in items:
                    if item.get("file_hash") not in live_hashes:
                        continue
                    self._update_indexes_unlocked(
                        state,
                        item.get("file_hash"),
                        item.get("phash"),
                        item.get("url"),
                        item.get("tiles"),
                        item.get("audio_hash"),
                        item.get("visual_embedding"),
                    )
            except Exception as exc:
                self._recover_after_write_locked(state, exc)
                return
            log_system("INFO", "RAM index batch update queued", count=len(items))

    def remove_indexes_batch(self, items: list[dict], ctx: WorkspaceContext | None = None):
        """SQLite'tan silinmiş kayıtların imzalarını ve URL'lerini RAM'den kaldır.

        Normal akışta silinen imzaların sayısını döndürür. Onarım yoluna
        girildiyse bu sayaç sıfırdır; yeniden yüklemenin sonucunu ölçmez.
        """

        hashes = {str(item.get("hash") or item.get("file_hash") or "").strip() for item in items or []}
        hashes.discard("")
        urls = [item.get("source_url") or item.get("url") for item in items or [] if item.get("source_url") or item.get("url")]
        if not hashes and not urls:
            return {"removed": 0}

        state = self._state_for(ctx)
        with state.sync_lock:
            if state.needs_recovery:
                self._recover_after_write_locked(state)
                return {"removed": 0}
            try:
                global_stats = state.global_tree.remove_hashes(hashes)
                tile_stats = state.tile_tree.remove_hashes(hashes)
                audio_removed = sum(state.audio_signatures.pop(item_hash, None) is not None for item_hash in hashes)
                video_removed = sum(state.video_signatures.pop(item_hash, None) is not None for item_hash in hashes)
                for url in urls:
                    state.url_registry.remove(url)
            except Exception as exc:
                self._recover_after_write_locked(state, exc)
                return {"removed": 0}

        removed = sum(
            int(stats.get("removed") or 0)
            for stats in (global_stats, tile_stats)
        ) + audio_removed + video_removed
        log_system(
            "INFO",
            "RAM indexes removed deleted items",
            hashes=len(hashes),
            urls=len(urls),
            removed=removed,
            global_removed=global_stats.get("removed", 0),
            tile_removed=tile_stats.get("removed", 0),
            audio_removed=audio_removed,
            video_removed=video_removed,
        )
        return {
            "removed": removed,
            "hashes": len(hashes),
            "urls": len(urls),
            "global": global_stats,
            "tile": tile_stats,
            "audio": {"removed": audio_removed},
            "video": {"removed": video_removed},
        }

    def _update_indexes_unlocked(self, state: _SearchIndexState, file_hash: str, phash: Optional[str], url: Optional[str], tiles: List[Tuple[int, str]] = None, audio_hash: bytes = None, visual_embedding: bytes = None):

        if url:
            state.url_registry.add(url)
        if phash:
            state.global_tree.add(file_hash, phash)
        if tiles:
            for _, tile_phash in tiles:
                state.tile_tree.add(file_hash, tile_phash)

        if audio_hash:
            state.audio_signatures[file_hash] = audio_hash
        if visual_embedding:
            state.video_signatures[file_hash] = visual_embedding

search_manager = SearchManager()
