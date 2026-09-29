import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from db.sqlite_operator import init_database
from db.search_manager import search_manager
from logger import log_system, reconfigure_logging, shutdown_logging
from metadata_index import start_metadata_repair_worker, start_metadata_watchdog, stop_metadata_watchdog
from runtime_activation import activate_runtime_context
from runtime_context import RuntimeNotLoadedError, build_runtime_context, has_runtime_context
from app_paths import get_app_paths
from config_repository import bootstrap_data_home

from api.common import (
    ALLOWED_ORIGINS,
    MUTATING_METHODS,
    _assets_dir,
    _file_response_under,
    _require_api_key,
    _review_dir,
    _scan_auth_status_sync,
    _validate_origin,
    configure_terminal_logging,
    restore_terminal_logging,
)
from api import app_settings, capture, ingestion, library, logs, review, runtime

# These paths must work before workspace/vault runtime exists. Vault/data
# routes stay blocked here and use api.guards for route-specific validation.
PRE_RUNTIME_PUBLIC_PATHS = {
    "/",
    "/api/session-key",
    "/api/app/settings",
    "/api/runtime/session",
    "/api/runtime/health",
}
PRE_RUNTIME_LOG_PATHS = {
    "/api/logs",
    "/api/logs/location",
    "/api/logs/open",
    "/api/logs/ui",
}
PRE_RUNTIME_WORKSPACE_PATHS = {
    "/api/workspaces",
    "/api/workspaces/active",
    "/api/workspaces/relocate",
}
PRE_RUNTIME_WORKSPACE_LOAD_PREFIXES = (
    "/api/workspaces/",
)


# Baştaki `_`, adın iç kullanım için olduğunu belirtir; Python gizliliği zorunlu kılmaz.
def _is_pre_runtime_path(path: str) -> bool:
    pre_runtime_paths = PRE_RUNTIME_PUBLIC_PATHS | PRE_RUNTIME_LOG_PATHS | PRE_RUNTIME_WORKSPACE_PATHS
    if path in pre_runtime_paths:
        return True
    return any(path.startswith(prefix) and path.endswith("/load") for prefix in PRE_RUNTIME_WORKSPACE_LOAD_PREFIXES)


def _load_env_workspace_if_requested():
    env_path = os.environ.get("LMZ_CONFIG_PATH")
    if not env_path or has_runtime_context():
        return
    try:
        ctx = build_runtime_context(env_path)
        activate_runtime_context(ctx)
        configure_terminal_logging()
    except Exception as exc:
        log_system("WARNING", "LMZ_CONFIG_PATH workspace load failed; staying in launcher mode", error=str(exc))


async def startup_auth_scan():
    if not has_runtime_context():
        return
    await asyncio.to_thread(_scan_auth_status_sync, "startup")


async def startup_env_workspace():
    await asyncio.to_thread(_load_env_workspace_if_requested)


async def startup_metadata_index():
    if not has_runtime_context():
        return
    def start_services():
        try:
            start_metadata_watchdog()
        except Exception as exc:
            log_system("WARNING", "Metadata watchdog startup failed", error=str(exc))
        try:
            start_metadata_repair_worker(full=False)
        except Exception as exc:
            log_system("WARNING", "Metadata index repair startup failed", error=str(exc))
    await asyncio.to_thread(start_services)


async def startup_search_index():
    if not has_runtime_context():
        return
    def hydrate_search_index():
        conn = init_database()
        try:
            search_manager.hydrate(conn)
        finally:
            conn.close()
    await asyncio.to_thread(hydrate_search_index)


# async fonksiyon await noktasında beklerken event loop başka işleri sürdürebilir;
# to_thread bloklayan işi ayrı iş parçacığına taşır.
@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(bootstrap_data_home, get_app_paths())
    reconfigure_logging()
    configure_terminal_logging()
    await startup_env_workspace()
    await startup_auth_scan()
    try:
        yield
    finally:
        await asyncio.to_thread(stop_metadata_watchdog)
        restore_terminal_logging()
        shutdown_logging()


app = FastAPI(title="LMZ API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)


@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    configure_terminal_logging()
    if not has_runtime_context() and os.environ.get("LMZ_CONFIG_PATH"):
        await asyncio.to_thread(_load_env_workspace_if_requested)
    if request.method in MUTATING_METHODS:
        try:
            _validate_origin(request.headers.get("origin"))
            _require_api_key(request)
        except Exception as exc:
            status_code = getattr(exc, "status_code", 500)
            detail = getattr(exc, "detail", str(exc))
            return JSONResponse(status_code=status_code, content={"detail": detail})
    if request.method != "OPTIONS" and not has_runtime_context() and not _is_pre_runtime_path(request.url.path):
        return JSONResponse(status_code=503, content={"detail": "Workspace not loaded"})
    try:
        return await call_next(request)
    except RuntimeNotLoadedError:
        return JSONResponse(status_code=503, content={"detail": "Workspace not loaded"})


@app.get("/vault/{asset_path:path}")
async def serve_vault_asset(asset_path: str):
    return await asyncio.to_thread(_file_response_under, _assets_dir(), asset_path)


@app.get("/review-assets/{asset_path:path}")
async def serve_review_asset(asset_path: str):
    return await asyncio.to_thread(_file_response_under, _review_dir(), asset_path)


app.include_router(runtime.router)
app.include_router(app_settings.router)
app.include_router(library.router)
app.include_router(logs.router)
app.include_router(ingestion.router)
app.include_router(review.router)
app.include_router(capture.router)


@app.get("/api/runtime/health")
async def runtime_health():
    """Return the local sidecar identity used by the desktop startup handshake."""
    nonce = os.environ.get("LMZ_STARTUP_NONCE", "").strip()
    return {
        "service": "lmz-api",
        "ready": bool(nonce),
        "protocol_version": 1,
        "nonce": nonce,
    }


@app.get("/")
async def root():
    return {"status": "LMZ API Running"}

