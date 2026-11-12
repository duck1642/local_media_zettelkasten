
"""API kullanmadan bağlantı kuyruklarını ve yerel girdi dizinini işleyen CLI."""

from logger import log_ingest_local, log_ingest_online
from utils import setup_directories, get_app_settings
from runtime_context import reload_runtime_context
from processor import process_file
from external_ingestion import ExternalIngestor
from db.sqlite_operator import init_database
from db.search_manager import search_manager
from queue_service import queue_path
from app_paths import get_app_paths
from config_repository import bootstrap_data_home

def main():
    """Kasayı hazırla; normal ve force kuyruklarından sonra yerel dosyaları işle."""

    bootstrap_data_home(get_app_paths())
    ctx = reload_runtime_context()
    setup_directories()
    config = get_app_settings()


    conn = init_database(ctx=ctx)
    try:
        search_manager.hydrate(conn)
    finally:
        conn.close()

    log_ingest_online('INFO', "\nLMZ Unified System - Starting")
    log_ingest_online('INFO', f"Input: {ctx.active_vault.input_dir}")
    log_ingest_online('INFO', f"Vault: {ctx.active_vault.vault_dir}")
    log_ingest_online('INFO', f"DB: {ctx.active_vault.db_path}\n")

    stats = {"processed": 0, "skipped": 0, "errors": 0}


    ingestion_targets = [
        ("normal_pending_links.md", False),
        ("force_pending_links.md", True)
    ]

    for filename, skip_val in ingestion_targets:
        queue_name = "force" if skip_val else "normal"
        links_file = queue_path(queue_name, ctx=ctx)
        if links_file.exists():
            mode_str = "FORCE (No Size Check)" if skip_val else "NORMAL"
            log_ingest_online('INFO', f"Found {filename} [{mode_str}]. Starting Ingestion...")
            ingestor = ExternalIngestor(str(links_file), skip_validation=skip_val, ctx=ctx)
            ext_stats = ingestor.run()
            stats["processed"] += ext_stats.get("processed", 0)
            stats["skipped"] += ext_stats.get("skipped", 0)
            stats["errors"] += ext_stats.get("errors", 0)
        else:
            log_ingest_online('INFO', f"  No {filename} found. Skipping.")

    log_ingest_local('INFO', "\nScanning local input folder for remaining files...")

    input_dir = ctx.active_vault.input_dir
    review_dir = ctx.active_vault.review_dir
    if not input_dir.exists():
        input_dir.mkdir(parents=True, exist_ok=True)

    local_index_queue = []
    for filepath in sorted(input_dir.rglob('*')):
        if not filepath.is_file() or filepath.suffix.lower() in ['.md', '.json', '.txt']:
            continue

        if review_dir in filepath.parents or filepath.parent == review_dir:
            continue


        success, message, idx_data = process_file(filepath, config, delete_source=True, sync_index=False, ctx=ctx)

        if success:
            log_ingest_local('INFO', f"{message}")
            stats["processed"] += 1
            if idx_data:
                local_index_queue.append(idx_data)

            parent = filepath.parent
            if parent != input_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        elif "Duplicate ignored" in message:
            log_ingest_local('INFO', f"{message}")
            stats["skipped"] += 1
        else:
            log_ingest_local('INFO', f"{message}")
            stats["errors"] += 1


    if local_index_queue:
        log_ingest_local('INFO', f"Syncing RAM indexes for {len(local_index_queue)} local items...")
        search_manager.update_indexes_batch(local_index_queue, ctx=ctx)

    log_ingest_local('INFO', f"\nFINAL SUMMARY: {stats['processed']} Added | {stats['skipped']} Skipped/Duplicates | {stats['errors']} Errors")
