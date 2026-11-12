"""Hazırlanmış çalışma alanı bağlamını log, arama ve metadata servislerine bağlar."""

from pathlib import Path

from path_policy import vault_root_is_usable
from runtime_context import WorkspaceContext, set_runtime_context


def active_vault_is_usable(ctx: WorkspaceContext) -> bool:
    """Kasa kökünün mevcut bağlamla dosya işlemlerine uygun olup olmadığını denetle."""
    return bool(ctx and ctx.active_vault and vault_root_is_usable(Path(ctx.active_vault.root), ctx.root))


def activate_runtime_context(ctx: WorkspaceContext, *, hydrate: bool = True) -> dict:
    """Bağlamı yayınla ve log hedeflerini yeni kasaya bağla.

    Kasa kullanılamıyorsa arama indeksini yüklemeden recovery sonucu döner.
    hydrate etkinse RAM indeksi SQLite'tan doldurulur, metadata izleyicisi
    yeniden başlatılır ve bir onarım çalışması başlatılmaya çalışılır.
    """
    set_runtime_context(ctx)

    from logger import log_system, reconfigure_logging

    reconfigure_logging(ctx)
    usable = active_vault_is_usable(ctx)
    if not usable:
        log_system("WARNING", "Workspace loaded but active vault root is missing", vault=ctx.active_vault.id, root=str(ctx.active_vault.root))
        return {"status": "recovery", "hydrated": False, "active_vault": ctx.active_vault.id}

    if not hydrate:
        return {"status": "success", "hydrated": False, "active_vault": ctx.active_vault.id}

    from db.search_manager import search_manager
    from db.sqlite_operator import init_database
    from metadata_index import restart_metadata_watchdog, start_metadata_repair_worker

    search_manager.reset_all()
    conn = init_database(ctx=ctx)
    try:
        search_manager.hydrate(conn)
    finally:
        conn.close()

    restart_metadata_watchdog(ctx)
    try:
        start_metadata_repair_worker(full=False)
    except Exception as exc:
        log_system("WARNING", "Metadata index repair startup failed during runtime activation", error=str(exc))

    log_system("INFO", "Runtime context activated", vault=ctx.active_vault.id, root=str(ctx.active_vault.root))
    return {"status": "success", "hydrated": True, "active_vault": ctx.active_vault.id}
