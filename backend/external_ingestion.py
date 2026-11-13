"""Çevrimiçi bağlantı kuyruğunu platformlara ayırır, indirir ve kasaya aktarır."""

from pathlib import Path
from typing import List, Optional, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
import threading
import time
import shutil
import re

from utils import (
    get_app_settings, asset_path_for, note_path_for, utc_now_str, wd_tag_cache_path_for
)
from runtime_context import WorkspaceContext, get_runtime_context
from db.sqlite_operator import connect_database, normalize_source_url
from db.search_manager import search_manager
from processor import process_file
from logger import log_ingest_audit, log_ingest_online
from media_lifecycle import storage_lifecycle_lock
from metadata_index import (
    _item_facet_values,
    ensure_metadata_schema,
    refresh_metadata_facet_counts_for_values,
    refresh_metadata_index_counters,
)
from queue_service import QueueEntry, parse_queue, queue_path
from thumbnail_store import delete_thumbnail
from thumbnails import thumbnail_db_path
from downloaders.gallery_dl_wrapper import download_gallery, inspect_gallery
from downloaders.yt_dlp_wrapper import download_video, inspect_youtube_community
import random
from ingest_control import online_stop_event


GLOBAL_WORKER_LIMIT: Optional[threading.Semaphore] = None
_GLOBAL_WORKER_LIMIT_SIZE: int | None = None
_GLOBAL_WORKER_LIMIT_LOCK = threading.Lock()


def _online_process_metadata(metadata: dict, run_id: str, user_metadata: dict | None = None) -> dict:
    """İndiriciden kaynak bilgisini al; kuyrukta belirtilen artist/platforma öncelik ver."""
    source = metadata or {}
    process_metadata = {
        key: source[key]
        for key in ("source_url", "platform")
        if key in source
    }
    for key in ("artist", "platform"):
        value = str((user_metadata or {}).get(key) or "").strip()
        if value:
            process_metadata[key] = value
    process_metadata.setdefault("ingest_type", "online")
    process_metadata.setdefault("run_id", run_id)
    return process_metadata


class ExternalIngestor:
    """Tek kuyruk çalışmasını kasa bağlamına bağlar ve indirme işçilerini yönetir."""

    def __init__(self, links_file: str, skip_validation: bool = False, ctx: WorkspaceContext | None = None):
        self.ctx = ctx or get_runtime_context()
        self.links_file = Path(links_file)
        self.config = get_app_settings()
        self.stop_event = online_stop_event(self.ctx)
        self.fail_log_lock = threading.Lock()
        self.skip_validation = skip_validation


        global GLOBAL_WORKER_LIMIT, _GLOBAL_WORKER_LIMIT_SIZE
        max_global = self.config.get("ingestion", {}).get("concurrency", {}).get("global_max_workers", 10)
        with _GLOBAL_WORKER_LIMIT_LOCK:
            # Available permits decrease while workers run; they are not the
            # configured capacity. Keep sharing that same limit between runs.
            if GLOBAL_WORKER_LIMIT is None or _GLOBAL_WORKER_LIMIT_SIZE != max_global:
                GLOBAL_WORKER_LIMIT = threading.Semaphore(max_global)
                _GLOBAL_WORKER_LIMIT_SIZE = max_global
                log_ingest_online("INFO", f"Global Ingestion Semaphore initialized with {max_global} slots.")
            self.worker_limit = GLOBAL_WORKER_LIMIT

    def run(self) -> dict:
        """Kuyruğu çalıştır ve işlenen, atlanan, hatalı kayıt sayaçlarını döndür.

        Platform sonuçları toplandıktan sonra RAM indeksi güncellenir. Durdurma
        yüzünden başlanamayan veya platform hatasında korunan URL'ler kaynak
        kuyruğa yeniden yazılır.
        """

        stats = {"processed": 0, "skipped": 0, "errors": 0}
        batch_index_queue = []

        if not self.links_file.exists():
            log_ingest_online("WARNING", f"Ingestion skipped: {self.links_file.name} not found")
            return stats

        try:
            log_ingest_online('INFO', f"Reading links from: {self.links_file}")
            links = self._parse_links()

            if not links:
                log_ingest_online("INFO", f"Ingestion finished: {self.links_file.name} is empty")
                return stats


            buckets = self._bucket_links(links)
            log_ingest_online(
                'INFO',
                f"Bucketed {len(links)} links into {len(buckets)} platform queues.",
                platforms=", ".join(sorted(buckets.keys())),
            )


            remaining_urls: List[QueueEntry] = []
            original_count = len(links)

            with ThreadPoolExecutor(max_workers=len(buckets)) as platform_executor:
                futures = {platform_executor.submit(self._manage_platform_queue, plat, urls): plat for plat, urls in buckets.items()}

                for future in as_completed(futures):
                    platform = futures[future]
                    try:
                        worker_stats, worker_remaining, worker_index_data = future.result()

                        stats["processed"] += worker_stats["processed"]
                        stats["skipped"] += worker_stats["skipped"]
                        stats["errors"] += worker_stats["errors"]
                        remaining_urls.extend(worker_remaining)
                        batch_index_queue.extend(worker_index_data)
                    except Exception as e:
                        crashed_urls = buckets.get(platform, [])
                        remaining_urls.extend(crashed_urls)
                        log_ingest_online("ERROR", "Platform manager crash; preserving platform URLs in source queue", platform=platform, preserved=len(crashed_urls), error=str(e))


            if batch_index_queue:
                log_ingest_online('INFO', f"Syncing RAM indexes for {len(batch_index_queue)} new items...")
                try:
                    search_manager.update_indexes_batch(batch_index_queue, ctx=self.ctx)
                except Exception as sync_e:
                    log_ingest_online("ERROR", "RAM Sync failed during batch finalization", error=str(sync_e))


            self._write_back(remaining_urls)
            removed_count = max(0, original_count - len(remaining_urls))
            log_ingest_online(
                "INFO",
                "Source queue rewritten after ingestion",
                original=original_count,
                removed=removed_count,
                remaining=len(remaining_urls),
            )
            log_ingest_audit(
                "INFO",
                "Online ingestion cycle summary",
                ingest_type="online",
                queue=str(self.links_file.name),
                processed=stats["processed"],
                skipped=stats["skipped"],
                errors=stats["errors"],
                original=original_count,
                remaining=len(remaining_urls),
            )
            log_ingest_online('INFO', f"[OK] Ingestion cycle complete. {len(remaining_urls)} deferred/crash-preserved links kept in source queue.")

        except Exception as e:
            log_ingest_online("ERROR", "ExternalIngestor failed", error=str(e))
            log_ingest_audit(
                "ERROR",
                "Online ingestion cycle failed",
                ingest_type="online",
                queue=str(self.links_file.name),
                processed=stats["processed"],
                skipped=stats["skipped"],
                errors=stats["errors"],
                error=str(e),
            )

        return stats

    def _manage_platform_queue(self, platform: str, urls: List[QueueEntry]) -> Tuple[dict, List[QueueEntry], List[dict]]:
        """Platform işçilerini çalıştır; durdurma sinyalinden sonra yeni URL başlatma.

        Sayaçları, ertelenen kuyruk kayıtlarını ve RAM indeksine girecek
        verileri döndürür. Başlamış işler tamamlanır.
        """

        concurrency = self.config.get("ingestion", {}).get("concurrency", {})
        plat_config = concurrency.get("platforms", {}).get(
            platform,
            concurrency.get("platforms", {}).get("default", {}),
        )

        num_workers = plat_config.get("workers", 1)
        jitter = plat_config.get("jitter_range", [2.0, 4.0])

        plat_stats = {"processed": 0, "skipped": 0, "errors": 0}
        plat_remaining = []
        plat_index_data = []

        log_ingest_online("INFO", f"Starting queue with {num_workers} workers.", platform=platform)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            pending_urls = iter(urls)
            in_flight = {}

            for _ in range(num_workers):
                if self.stop_event.is_set():
                    break
                try:
                    next_url = next(pending_urls)
                except StopIteration:
                    break
                in_flight[executor.submit(self._worker_item, platform, next_url, jitter)] = next_url

            while in_flight:
                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    current_entry = in_flight.pop(future, None)
                    current_url = current_entry.url if current_entry else ""
                    try:
                        success, url_out, stats_out, index_list = future.result()
                    except Exception as exc:
                        plat_stats["errors"] += 1
                        if current_url:
                            self._log_failure(current_url, f"Worker crash: {exc}")
                        log_ingest_online("ERROR", "Online ingestion worker crash", platform=platform, url=current_url, error=str(exc))
                        continue

                    plat_stats["processed"] += stats_out["processed"]
                    plat_stats["skipped"] += stats_out["skipped"]
                    plat_stats["errors"] += stats_out["errors"]

                    if index_list:
                        plat_index_data.extend(index_list)

                if self.stop_event.is_set():
                    continue

                for _ in range(len(done)):
                    try:
                        next_url = next(pending_urls)
                    except StopIteration:
                        break
                    in_flight[executor.submit(self._worker_item, platform, next_url, jitter)] = next_url

            if self.stop_event.is_set():
                for url_left in pending_urls:
                    plat_remaining.append(url_left)
                log_ingest_online("INFO", "Stop-after-current acknowledged; deferred URLs preserved in source queue", platform=platform, deferred=len(plat_remaining))

        return plat_stats, plat_remaining, plat_index_data

    def _worker_item(self, platform: str, entry: QueueEntry, jitter_range: list) -> Tuple[bool, str, dict, List[dict]]:
        """Tek URL'yi işle; başarı, URL, sayaçlar ve indeks girdilerini döndür."""

        item_stats = {"processed": 0, "skipped": 0, "errors": 0}
        url = entry.url


        downloader_type = self._get_downloader_type(url)
        if not downloader_type:
            log_ingest_online("WARNING", "URL skipped: Unsupported platform", url=url)
            self._log_failure(url, "Unsupported platform")
            return False, url, item_stats, []

        is_pixiv = self._is_pixiv_url(url)
        is_x = self._is_x_url(url)
        is_instagram = self._is_instagram_url(url)
        is_pinterest = self._is_pinterest_url(url)
        is_youtube_community = self._is_youtube_community_url(url)
        batch_protected = is_pixiv or is_x or is_instagram or is_pinterest or is_youtube_community
        metadata_info = None

        if is_pixiv or is_instagram or is_pinterest or is_youtube_community:
            if is_youtube_community:
                meta_success, metadata_info = inspect_youtube_community(url)
            else:
                meta_success, metadata_info = inspect_gallery(url)
            if not meta_success:
                platform_label = "YouTube community" if is_youtube_community else "Pinterest" if is_pinterest else "Instagram" if is_instagram else "Pixiv"
                error_msg = metadata_info.get('error', f'{platform_label} metadata failed')
                log_ingest_online("ERROR", f"{platform_label} metadata failed", url=url, platform=platform, error=error_msg)
                self._log_failure(url, f"{platform_label} metadata failed: {error_msg}")
                item_stats["errors"] += 1
                return False, url, item_stats, []

            expected_count = metadata_info.get("expected_count", 0)
            if is_instagram:
                shortcode = self._instagram_shortcode(url)
                if self._instagram_complete(url, expected_count):
                    log_ingest_online("INFO", "URL skipped: Complete in database", platform=platform, url=url, shortcode=shortcode, expected_count=expected_count)
                    item_stats["skipped"] += 1
                    return True, url, item_stats, []
            elif is_pinterest:
                if self._url_complete(url):
                    log_ingest_online("INFO", "URL skipped: Complete in database", platform=platform, url=url)
                    item_stats["skipped"] += 1
                    return True, url, item_stats, []
            elif is_youtube_community:
                if self._url_complete(url, expected_count):
                    log_ingest_online("INFO", "URL skipped: Complete in database", platform=platform, url=url, expected_count=expected_count)
                    item_stats["skipped"] += 1
                    return True, url, item_stats, []
            elif self._url_complete(url, expected_count):
                log_ingest_online("INFO", "URL skipped: Complete in database", platform=platform, url=url, expected_count=expected_count)
                item_stats["skipped"] += 1
                return True, url, item_stats, []
        elif self._url_complete(url):
            log_ingest_online("INFO", "URL skipped: Complete in database", platform=platform, url=url)
            item_stats["skipped"] += 1
            return True, url, item_stats, []


        with self.worker_limit:

            wait_time = random.uniform(jitter_range[0], jitter_range[1])
            time.sleep(wait_time)


            max_attempts = 2
            attempt = 0
            success = False
            result = {}

            while attempt < max_attempts and not success:
                attempt += 1
                log_ingest_online("INFO", f"Processing (Attempt {attempt}/{max_attempts})", platform=platform, url=url)

                if downloader_type == 'gallery-dl':
                    success, result = download_gallery(url, metadata_info=metadata_info)
                elif downloader_type == 'yt-dlp':
                    success, result = download_video(url, metadata_info=metadata_info)

                if not success and attempt < max_attempts:
                    log_ingest_online("WARNING", f"Download attempt {attempt} failed, retrying...", url=url, platform=platform)
                    time.sleep(2)

            if success:

                expected_count = result.get("expected_count", 0)
                downloaded_count = result.get("downloaded_count", len(result.get("file_paths", [])))
                extra_data = {"url": url, "platform": platform, "expected_count": expected_count, "downloaded_count": downloaded_count}
                if is_x: extra_data["download_url"] = result.get("download_url", "")
                if is_instagram: extra_data["shortcode"] = self._instagram_shortcode(url)
                log_ingest_online("INFO", "Download verified", **extra_data)

                validation_failed = False
                validation_error = ""

                if not self.skip_validation:
                    expected_size = result.get('expected_size')
                    expected_sizes = result.get('expected_sizes', {})

                    for f_path in result['file_paths']:
                        p = Path(f_path)
                        if not p.exists(): continue

                        actual_size = p.stat().st_size
                        target_expected = None

                        if expected_size:
                            target_expected = expected_size
                        elif expected_sizes:

                            target_expected = expected_sizes.get(p.stem)

                        if target_expected:
                            if downloader_type == 'gallery-dl':

                                if actual_size != target_expected:
                                    validation_failed = True
                                    validation_error = f"Size mismatch for {p.name}: Expected {target_expected}, got {actual_size} (STRICT)"
                                    break
                            else:

                                diff = abs(actual_size - target_expected)
                                if diff > (target_expected * 0.01) and diff > 102400:
                                    validation_failed = True
                                    validation_error = f"Size mismatch for {p.name}: Expected {target_expected}, got {actual_size} (RELAXED)"
                                    break

                if validation_failed:
                    log_ingest_online("ERROR", "Integrity check failed", url=url, platform=platform, error=validation_error)
                    self._log_failure(url, f"Integrity check failed: {validation_error}")


                    if 'session_dir' in result:
                        shutil.rmtree(result['session_dir'], ignore_errors=True)

                    item_stats["errors"] += 1
                    return False, url, item_stats, []


                processed_all = True
                batch_data = []

                for f_path in result['file_paths']:
                    target_file = Path(f_path)

                    process_metadata = _online_process_metadata(result.get('metadata') or {}, self.links_file.stem, entry.metadata())
                    process_success, msg, idx_data = process_file(
                        target_file,
                        self.config,
                        metadata=process_metadata,
                        delete_source=True,
                        sync_index=False,
                        ctx=self.ctx,
                    )

                    if process_success:
                        log_ingest_online("INFO", msg, platform=platform, url=url)
                        item_stats["processed"] += 1
                        if idx_data:
                            batch_data.append(idx_data)
                    else:
                        log_ingest_online("ERROR", f"Pipeline Error: {msg}", platform=platform, url=url)
                        item_stats["errors"] += 1
                        processed_all = False


                if 'session_dir' in result:
                    session_dir_path = Path(result['session_dir'])
                    if session_dir_path.exists():
                        shutil.rmtree(session_dir_path, ignore_errors=True)

                if not processed_all:

                    if batch_data and batch_protected:
                        rolled_back = self._rollback_batch(batch_data)
                        if rolled_back is not None:
                            item_stats["processed"] = max(0, item_stats["processed"] - rolled_back)
                            log_ingest_online("WARNING", "Rolled back partial URL ingest", url=url, platform=platform, rolled_back=rolled_back)
                            batch_data = []
                    self._log_failure(url, "Pipeline Error during file processing")
                    # A failed rollback leaves the previously committed items
                    # in SQLite. Still publish them to the normal batch index.
                    return False, url, item_stats, batch_data

                if is_pixiv:
                    log_ingest_online("INFO", "Pixiv URL processed successfully", url=url, processed=len(batch_data))
                elif is_x:
                    log_ingest_online("INFO", "X URL processed successfully", url=url, processed=len(batch_data))
                elif is_instagram:
                    log_ingest_online("INFO", "Instagram URL processed successfully", url=url, shortcode=self._instagram_shortcode(url), processed=len(batch_data))
                elif is_pinterest:
                    log_ingest_online("INFO", "Pinterest URL processed successfully", url=url, processed=len(batch_data))
                elif is_youtube_community:
                    log_ingest_online("INFO", "YouTube community post processed successfully", url=url, processed=len(batch_data))
                return True, url, item_stats, batch_data
            else:
                error_msg = result.get('error', f"Failed after {max_attempts} attempts")
                log_ingest_online("ERROR", "Download permanently failed", url=url, platform=platform, error=error_msg)

                self._log_failure(url, f"Download failed: {error_msg}")
                item_stats["errors"] += 1
                return False, url, item_stats, []

    def _is_pixiv_url(self, url: str) -> bool:
        return 'pixiv.net' in url.lower()

    def _is_x_url(self, url: str) -> bool:
        u = url.lower()
        return 'twitter.com' in u or 'x.com' in u

    def _is_instagram_url(self, url: str) -> bool:
        return 'instagram.com' in url.lower()

    def _is_pinterest_url(self, url: str) -> bool:
        u = url.lower()
        return 'pinterest' in u or 'pin.it' in u

    def _is_youtube_community_url(self, url: str) -> bool:
        u = url.lower()
        return 'youtube.com/post/' in u or ('youtube.com/' in u and '/community' in u and 'lb=' in u)

    def _instagram_shortcode(self, url: str) -> str:
        match = re.search(r'instagram\.com/(?:p|reel|tv)/([^/?#]+)', url, re.IGNORECASE)
        return match.group(1) if match else ""

    def _instagram_complete(self, url: str, expected_count: int = None) -> bool:
        shortcode = self._instagram_shortcode(url)
        if not shortcode:
            return self._url_complete(url, expected_count)

        conn = connect_database() if self.ctx is None else connect_database(ctx=self.ctx)
        try:
            rows = conn.execute(
                'SELECT hash, file_extension, mime_type, storage_id FROM items WHERE LOWER(source_url) LIKE LOWER(?)',
                (f"%/{shortcode}%",)
            ).fetchall()

            if not rows:
                return False

            missing_assets = []
            for file_hash, file_extension, mime_type, storage_id in rows:
                asset_path = asset_path_for(file_hash, file_extension, mime_type, storage_id=storage_id, ctx=self.ctx)
                if not asset_path.exists():
                    missing_assets.append(file_hash)

            if missing_assets:
                log_ingest_online("WARNING", "Instagram URL has DB rows with missing assets", url=url, shortcode=shortcode, missing_count=len(missing_assets))
                return False

            if expected_count and len(rows) != expected_count:
                log_ingest_online("WARNING", "Instagram URL has incomplete DB row count", url=url, shortcode=shortcode, expected_count=expected_count, db_count=len(rows))
                return False

            return True
        finally:
            conn.close()

    def _url_complete(self, url: str, expected_count: int = None) -> bool:
        """URL'nin tüm kayıtlı dosyalarının diskte ve beklenen sayıda olduğunu doğrula."""
        conn = connect_database() if self.ctx is None else connect_database(ctx=self.ctx)
        try:
            rows = conn.execute(
                'SELECT hash, file_extension, mime_type, storage_id FROM items WHERE source_url_norm = ?',
                (normalize_source_url(url),)
            ).fetchall()

            if not rows:
                return False

            missing_assets = []
            for file_hash, file_extension, mime_type, storage_id in rows:
                asset_path = asset_path_for(file_hash, file_extension, mime_type, storage_id=storage_id, ctx=self.ctx)
                if not asset_path.exists():
                    missing_assets.append(file_hash)

            if missing_assets:
                log_ingest_online("WARNING", "URL has DB rows with missing assets", url=url, missing_count=len(missing_assets))
                return False

            if expected_count is not None and len(rows) != expected_count:
                log_ingest_online("WARNING", "URL has incomplete DB row count", url=url, expected_count=expected_count, db_count=len(rows))
                return False

            return True
        finally:
            conn.close()

    def _rollback_batch(self, batch_data: List[dict]) -> int | None:
        """Yarım kalan bir toplu aktarımı önce SQLite'tan, sonra disk ve RAM'den temizle.

        SQLite silme işlemi tamamlanmazsa None döner ve dosyalar silinmez.
        Başarılı commit sonrasında dönüş değeri silinen satır sayısıdır;
        sonraki dosya temizliği hataları ayrıca loglanır.
        """
        conn = None
        removed_indexes = []
        cleanup_paths = []
        facet_values: set[tuple[str, str]] = set()
        try:
            conn = connect_database() if self.ctx is None else connect_database(ctx=self.ctx)
            for item in batch_data:
                file_hash = item.get("file_hash")
                if not file_hash:
                    continue

                row = conn.execute(
                    'SELECT file_extension, mime_type, storage_id, source_url FROM items WHERE hash = ?',
                    (file_hash,)
                ).fetchone()
                if row is None:
                    continue

                file_extension, mime_type, storage_id, source_url = row
                if not removed_indexes:
                    ensure_metadata_schema(conn)
                facet_values.update(_item_facet_values(conn, file_hash))
                conn.execute('DELETE FROM items WHERE hash = ?', (file_hash,))
                removed_indexes.append({"hash": file_hash, "source_url": source_url})

                paths = []
                if file_extension:
                    paths.append(asset_path_for(file_hash, file_extension, mime_type, storage_id=storage_id, ctx=self.ctx))

                if storage_id:
                    paths.extend((
                        note_path_for(file_hash, storage_id=storage_id, ctx=self.ctx),
                        wd_tag_cache_path_for(file_hash, storage_id=storage_id, ctx=self.ctx),
                    ))
                cleanup_paths.append((storage_id, paths))

            # Files cannot be restored by SQLite rollback. Delete them only
            # after the database confirms that the batch removal is durable.
            if removed_indexes:
                refresh_metadata_facet_counts_for_values(conn, facet_values)
                refresh_metadata_index_counters(conn)
            conn.commit()
        except Exception as e:
            if conn is not None:
                conn.rollback()
            log_ingest_online("ERROR", "Rollback failed", error=str(e))
            return None
        finally:
            if conn is not None:
                conn.close()

        try:
            search_manager.remove_indexes_batch(removed_indexes, ctx=self.ctx)
        except Exception as e:
            log_ingest_online("ERROR", "Batch rollback committed but RAM cleanup failed", error=str(e))

        for storage_id, paths in cleanup_paths:
            # Wait for any thumbnail already being published before unlinking.
            with storage_lifecycle_lock(storage_id, ctx=self.ctx):
                if storage_id:
                    delete_thumbnail(thumbnail_db_path(self.ctx), storage_id)
                for path in paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as e:
                        log_ingest_online("WARNING", "Batch rollback committed but file cleanup failed", path=str(path), error=str(e))
        return len(removed_indexes)

    def _log_failure(self, url: str, reason: str):

        failure_file = queue_path("failed", ctx=self.ctx)
        timestamp = utc_now_str()


        with self.fail_log_lock:
            mode = 'a' if failure_file.exists() else 'w'
            with open(failure_file, mode, encoding='utf-8') as f:
                if mode == 'w':
                    f.write("# LMZ Failed Links Log\n")
                    f.write("# This file tracks URLs that failed to process correctly.\n\n")
                f.write(f"[{timestamp}] {url} | Reason: {reason}\n")


    def _bucket_links(self, links: List[QueueEntry]) -> Dict[str, List[QueueEntry]]:

        buckets = {}
        for entry in links:
            platform = self._get_platform_name(entry.url)
            if platform not in buckets:
                buckets[platform] = []
            buckets[platform].append(entry)
        return buckets

    def _get_platform_name(self, url: str) -> str:

        u = url.lower()
        if 'pixiv.net' in u: return 'pixiv'
        if 'pinterest' in u or 'pin.it' in u: return 'pinterest'
        if 'instagram.com' in u: return 'instagram'
        if 'twitter.com' in u or 'x.com' in u: return 'x'
        if 'youtube.com' in u or 'youtu.be' in u: return 'youtube'
        return 'other'

    def _parse_links(self) -> List[QueueEntry]:

        text = self.links_file.read_text(encoding='utf-8', errors='replace')
        parsed = parse_queue(text)
        for warning in parsed["warnings"]:
            log_ingest_online(
                "WARNING",
                warning.message,
                queue=str(self.links_file.name),
                line=warning.line,
                code=warning.code,
            )
        return parsed["entries"]

    def _get_downloader_type(self, url: str) -> Optional[str]:

        u = url.lower()
        if any(x in u for x in ['pixiv.net', 'pinterest.com', 'pin.it', 'instagram.com', 'twitter.com', 'x.com']):
            return 'gallery-dl'
        if any(x in u for x in ['youtube.com', 'youtu.be']):
            return 'yt-dlp'
        return None

    def _write_back(self, links: List[QueueEntry]):
        """Kalan bağlantıları özgün satır sırası ve grup metadata'sıyla kuyruğa yaz."""

        with open(self.links_file, 'w', encoding='utf-8') as f:
            if links:
                f.write("# Remaining links for LMZ Ingestion\n")
                previous_group = None
                for entry in sorted(links, key=lambda item: (item.line, item.url)):
                    if previous_group is not None and previous_group != entry.group_index:
                        f.write("\n---\n\n")
                    if previous_group != entry.group_index:
                        if entry.artist:
                            f.write(f"@artist: {entry.artist}\n")
                        if entry.platform:
                            f.write(f"@platform: {entry.platform}\n")
                    f.write(f"{entry.url}\n")
                    previous_group = entry.group_index
            else:
                f.write("# All links processed. \n")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        ingestor = ExternalIngestor(sys.argv[1])
        ingestor.run()
