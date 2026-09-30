import asyncio
import copy
import os
import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Literal

import runtime_transitions
from config_repository import ConfigReadError
from db.sqlite_operator import connect_database
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from logger import configure_terminal_logging, log_system
from metadata_index import metadata_index_status, start_metadata_repair_worker
from path_policy import workspace_relative_path
from pydantic import BaseModel
from runtime_activation import activate_runtime_context, active_vault_is_usable
from runtime_context import (
    build_runtime_context,
    get_runtime_context,
    has_runtime_context,
    try_get_runtime_context,
)
from workspace_db import prune_unused_workspace_metadata, rebuild_workspace_metadata

from api.common import _api_key, _validate_origin
from api.guards import (
    require_usable_target_vault_context,
    require_usable_vault_context,
    require_workspace_context,
)

router = APIRouter()


@router.get("/api/session-key")
async def get_session_key(request: Request):
    _validate_origin(request.headers.get("origin"))
    return {"key": _api_key()}


@router.get("/api/runtime/session")
async def get_runtime_session():
    ctx = try_get_runtime_context()
    if ctx is None:
        return {"loaded": False}
    vault = ctx.active_vault
    return {
        "loaded": True,
        "workspace": {
            "root": str(ctx.root),
            "topics_root": str(ctx.topics_dir),
        },
        "vault": {
            "id": vault.id,
            "name": vault.name,
            "root": str(vault.root),
            "database": str(vault.db_path),
        },
        "env_override": bool(os.environ.get("LMZ_CONFIG_PATH")),
    }


@router.get("/api/metadata-index/status")
async def get_metadata_index_status():
    require_usable_vault_context()
    return await asyncio.to_thread(_get_metadata_index_status_sync)

def _get_metadata_index_status_sync():
    conn = connect_database()
    try:
        return metadata_index_status(conn)
    finally:
        conn.close()

@router.post("/api/metadata-index/rebuild")
async def rebuild_metadata_index():
    require_usable_vault_context()
    return await asyncio.to_thread(start_metadata_repair_worker, True, True)

@router.post("/api/workspace-metadata/rebuild")
async def rebuild_workspace_metadata_route():
    require_workspace_context()
    return await asyncio.to_thread(rebuild_workspace_metadata)

@router.post("/api/workspace-metadata/prune")
async def prune_workspace_metadata_route():
    require_workspace_context()
    return await asyncio.to_thread(prune_unused_workspace_metadata)


@router.get("/api/system/memory")
async def get_system_memory():
    require_workspace_context()
    return await asyncio.to_thread(_get_system_memory_sync)

def _get_system_memory_sync():
    try:
        try:
            import psutil
            payload = _get_psutil_app_memory(psutil)
        except ModuleNotFoundError:
            backend_mb = _get_process_memory_mb_fallback()
            payload = {
                "backend_mb": round(backend_mb, 2),
                "app_mb": round(backend_mb, 2),
                "runtime_mb": round(backend_mb, 2),
                "roles": _empty_memory_roles(backend_mb=backend_mb),
                "process_count": 1,
                "mode": "fallback",
                "warnings": ["psutil unavailable; reporting backend process only"],
                "processes": [],
            }
        return payload
    except Exception as exc:
        log_system("ERROR", "Failed to read backend memory", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to read backend memory") from exc

def _empty_memory_roles(backend_mb: float = 0.0) -> dict:
    return {
        "backend_mb": round(float(backend_mb or 0.0), 2),
        "tauri_mb": 0.0,
        "webview_mb": 0.0,
        "subprocess_mb": 0.0,
        "dev_tool_mb": 0.0,
        "other_mb": 0.0,
    }

def _process_name(proc) -> str:
    try:
        return str(proc.name() or "")
    except Exception:
        return ""

def _process_exe(proc) -> str:
    try:
        return str(proc.exe() or "")
    except Exception:
        return ""

def _process_cmdline(proc) -> list[str]:
    try:
        return [str(part or "") for part in (proc.cmdline() or [])]
    except Exception:
        return []

def _process_rss_mb(proc) -> float | None:
    try:
        return float(proc.memory_info().rss) / 1024 / 1024
    except Exception:
        return None

def _process_children(proc, recursive: bool = True) -> list:
    try:
        return list(proc.children(recursive=recursive))
    except Exception:
        return []

def _process_parent(proc):
    try:
        return proc.parent()
    except Exception:
        return None

def _looks_like_tauri_host(proc) -> bool:
    name = _process_name(proc).casefold()
    exe = _process_exe(proc).casefold()
    haystack = " ".join([name, exe])
    return any(token in haystack for token in ("lmz", "local_media_zettelkasten", "tauri"))

def _looks_like_dev_launcher(proc) -> bool:
    cmdline = " ".join(_process_cmdline(proc)).casefold()
    return "dev.py" in cmdline and "local_media_zettelkasten" in cmdline

def _project_path_token() -> str:
    try:
        return str(get_runtime_context().root).casefold()
    except Exception:
        try:
            return str(Path(__file__).resolve().parents[2]).casefold()
        except Exception:
            return "local_media_zettelkasten"

def _process_matches_project(proc, project_token: str) -> bool:
    if not project_token:
        return False
    haystack = " ".join([_process_exe(proc), *_process_cmdline(proc)]).casefold()
    return project_token in haystack or "local_media_zettelkasten" in haystack

def _scan_project_processes(psutil_module, project_token: str) -> list:
    matches = []
    try:
        iterator = psutil_module.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception:
        return matches
    for proc in iterator:
        if _process_matches_project(proc, project_token):
            matches.append(proc)
            matches.extend(_process_children(proc, recursive=True))
    return matches

def _role_for_process(proc, backend_pid: int) -> str:
    try:
        if int(proc.pid) == int(backend_pid):
            return "backend"
    except Exception:
        pass
    name = _process_name(proc).casefold()
    exe = _process_exe(proc).casefold()
    cmdline = " ".join(_process_cmdline(proc)).casefold()
    haystack = " ".join([name, exe, cmdline])
    if "msedgewebview2" in haystack:
        return "webview"
    if any(token in haystack for token in ("ffmpeg", "gallery-dl", "gallery_dl", "yt-dlp", "yt_dlp")):
        return "subprocess"
    if _looks_like_tauri_host(proc):
        return "tauri"
    if any(token in haystack for token in ("node", "npm", "cargo", "vite", "tauri-cli")):
        return "dev_tool"
    return "other"

def _collect_process_group(backend_proc, psutil_module=None) -> tuple[list, str, list[str]]:
    warnings: list[str] = []
    backend_children = _process_children(backend_proc, recursive=True)
    parent = _process_parent(backend_proc)
    processes = [backend_proc, *backend_children]
    mode = "backend_tree"
    if parent and _looks_like_tauri_host(parent):
        processes = [parent, *_process_children(parent, recursive=True)]
        mode = "packaged_sidecar"
    elif parent and _looks_like_dev_launcher(parent):
        processes = [parent, *_process_children(parent, recursive=True)]
        mode = "dev_launcher"
    elif psutil_module is not None:
        project_matches = _scan_project_processes(psutil_module, _project_path_token())
        if project_matches:
            processes = [backend_proc, *backend_children, *project_matches]
            mode = "dev_scan"
            warnings.append("app root not detected; included readable project-matched process trees")
        else:
            warnings.append("app root not detected; reporting backend process tree only")
    else:
        warnings.append("app root not detected; reporting backend process tree only")

    by_pid = {}
    for proc in processes:
        try:
            by_pid[int(proc.pid)] = proc
        except Exception:
            continue
    return list(by_pid.values()), mode, warnings

def _aggregate_memory_processes(processes: list, backend_pid: int, mode: str, warnings: list[str]) -> dict:
    roles = _empty_memory_roles()
    process_rows = []
    app_mb = 0.0
    for proc in processes:
        rss_mb = _process_rss_mb(proc)
        if rss_mb is None:
            warnings.append(f"process {getattr(proc, 'pid', '?')} memory unavailable")
            continue
        role = _role_for_process(proc, backend_pid)
        key = f"{role}_mb"
        if key not in roles:
            key = "other_mb"
        rounded = round(rss_mb, 2)
        roles[key] = round(float(roles.get(key, 0.0)) + rounded, 2)
        app_mb += rss_mb
        process_rows.append({
            "pid": int(getattr(proc, "pid", 0) or 0),
            "name": _process_name(proc),
            "role": role,
            "rss_mb": rounded,
        })
    runtime_mb = (
        roles["backend_mb"]
        + roles["tauri_mb"]
        + roles["webview_mb"]
        + roles["subprocess_mb"]
    )
    return {
        "backend_mb": roles["backend_mb"],
        "app_mb": round(app_mb, 2),
        "runtime_mb": round(runtime_mb, 2),
        "roles": roles,
        "process_count": len(process_rows),
        "mode": mode,
        "warnings": warnings,
        "processes": sorted(process_rows, key=lambda row: (row["role"], row["pid"])),
    }

def _get_psutil_app_memory(psutil_module) -> dict:
    backend_proc = psutil_module.Process()
    processes, mode, warnings = _collect_process_group(backend_proc, psutil_module)
    return _aggregate_memory_processes(processes, int(backend_proc.pid), mode, warnings)

def _get_process_memory_mb_fallback():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize / 1024 / 1024

    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return value / divisor


@router.get("/api/workspaces")
async def get_workspaces():
    return await asyncio.to_thread(_get_workspaces_sync)


def _get_workspaces_sync():
    from workspaces import load_workspace_registry, workspace_list

    return {"active": load_workspace_registry()["active_workspace"], "items": workspace_list()}


@router.post("/api/workspaces/active")
async def set_workspace_active(body: dict):
    return await asyncio.to_thread(_set_workspace_active_sync, body)


def _runtime_switch_blocker():
    if not has_runtime_context():
        return None
    preflight = runtime_transitions.runtime_switch_preflight()
    if preflight.get("allowed"):
        return None
    return JSONResponse(
        status_code=409,
        content={"detail": "Runtime switch blocked", "blockers": list(preflight.get("blockers") or [])},
    )


def _ensure_runtime_switch_allowed():
    try:
        runtime_transitions.ensure_runtime_switch_allowed()
    except runtime_transitions.RuntimeSwitchBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"detail": "Runtime switch blocked", "blockers": exc.blockers},
        ) from exc


def _set_workspace_active_sync(body: dict):
    workspace_id = str((body or {}).get("id") or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=400, detail="workspace id is required")
    payload = _load_workspace_sync(workspace_id)
    if payload.get("status") != "success":
        return payload
    from workspaces import load_workspace_registry, workspace_list

    return {
        "status": "success",
        "active": load_workspace_registry()["active_workspace"],
        "restart_required": False,
        "items": workspace_list(),
    }


@router.post("/api/workspaces")
async def create_workspace(body: dict):
    return await asyncio.to_thread(_create_workspace_sync, body)


def _create_workspace_sync(body: dict):
    from workspace_setup import setup_lmz_workspace
    from workspaces import register_workspace, workspace_list

    parent_path = str((body or {}).get("path") or "").strip()
    name = str((body or {}).get("name") or "LMZ Workspace").strip() or "LMZ Workspace"
    set_active = bool((body or {}).get("set_active"))
    if not parent_path:
        raise HTTPException(status_code=400, detail="Workspace parent folder is required")
    try:
        payload = setup_lmz_workspace(parent_path)
        registry = register_workspace(name, payload["config_path"], set_active=set_active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "success",
        "workspace": payload,
        "active": registry["active_workspace"],
        "restart_required": set_active,
        "items": workspace_list(),
    }




@router.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    mode: Literal["unregister", "generated", "all"] = Query("unregister"),
):
    blocked = await asyncio.to_thread(_runtime_switch_blocker)
    if blocked:
        return blocked
    return await asyncio.to_thread(_delete_workspace_sync, workspace_id, mode)


def _delete_workspace_sync(workspace_id: str, mode: str = "unregister"):
    from workspaces import WorkspaceDeletionError, delete_workspace, workspace_list

    try:
        result = delete_workspace(workspace_id, mode=mode)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except WorkspaceDeletionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {
        "status": "success",
        "active": result["active_workspace"],
        "mode": result["mode"],
        "cleanup_status": result["cleanup_status"],
        "cleanup_path": result["cleanup_path"],
        "items": workspace_list(),
    }


@router.get("/api/vaults")
async def get_vaults():
    require_workspace_context()
    return await asyncio.to_thread(_get_vaults_sync)


def _get_vaults_sync():
    from vaults import active_vault_id, vault_list

    return {"active": active_vault_id(), "items": vault_list()}


@router.post("/api/vaults")
async def create_vault(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_create_vault_sync, body)


def _create_vault_sync(body: dict):
    from vaults import create_vault

    name = str((body or {}).get("name") or "").strip()
    vault_id = str((body or {}).get("id") or "").strip() or None
    if not name:
        raise HTTPException(status_code=400, detail="vault name is required")
    try:
        payload = create_vault(name, vault_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload["restart_required"] = False
    return payload


@router.patch("/api/vaults/{vault_id}")
async def rename_vault(vault_id: str, body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_rename_vault_sync, vault_id, body)


def _rename_vault_sync(vault_id: str, body: dict):
    from vaults import rename_vault

    name = str((body or {}).get("name") or "").strip()
    try:
        return rename_vault(vault_id, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/vaults/active")
async def set_vault_active(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_set_vault_active_sync, body)


def _set_vault_active_sync(body: dict):
    from vaults import set_active_vault

    vault_id = str((body or {}).get("id") or "").strip()
    if not vault_id:
        raise HTTPException(status_code=400, detail="vault id is required")
    try:
        return set_active_vault(vault_id)
    except runtime_transitions.RuntimeSwitchBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"detail": "Runtime switch blocked", "blockers": exc.blockers},
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/vaults/{vault_id}")
async def delete_vault(vault_id: str, confirm: bool = Query(False)):
    require_workspace_context()
    return await asyncio.to_thread(_delete_vault_sync, vault_id, confirm)


def _delete_vault_sync(vault_id: str, confirm: bool = False):
    from vaults import delete_vault

    try:
        return delete_vault(vault_id, confirm=confirm)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/vaults/merge-preview")
async def preview_merged_vault(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_preview_merged_vault_sync, body)


def _preview_merged_vault_sync(body: dict):
    from vaults import preview_merged_vault

    name = str((body or {}).get("name") or "").strip()
    sources = list((body or {}).get("source_vault_ids") or [])
    try:
        return preview_merged_vault(name, sources)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/vaults/merge")
async def create_merged_vault(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_create_merged_vault_sync, body)


def _create_merged_vault_sync(body: dict):
    from vaults import merge_vaults_to_new

    name = str((body or {}).get("name") or "").strip()
    sources = list((body or {}).get("source_vault_ids") or [])
    try:
        payload = merge_vaults_to_new(name, sources)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload["restart_required"] = False
    return payload


@router.get("/api/vaults/{vault_id}/health")
async def get_vault_health(vault_id: str):
    require_usable_target_vault_context(vault_id)
    return await asyncio.to_thread(_get_vault_health_sync, vault_id)


def _get_vault_health_sync(vault_id: str):
    from vaults import audit_vault_health

    try:
        return audit_vault_health(vault_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/vaults/{vault_id}/repair")
async def repair_vault(vault_id: str, body: dict):
    require_usable_target_vault_context(vault_id)
    return await asyncio.to_thread(_repair_vault_sync, vault_id, body)


def _repair_vault_sync(vault_id: str, body: dict):
    from vaults import repair_vault

    try:
        return repair_vault(
            vault_id,
            actions=list((body or {}).get("actions") or []),
            confirm_destructive=(body or {}).get("confirm_destructive") is True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/vaults/{vault_id}/backup")
async def backup_vault(vault_id: str, body: dict | None = None):
    require_usable_target_vault_context(vault_id)
    return await asyncio.to_thread(_backup_vault_sync, vault_id, body or {})


def _backup_vault_sync(vault_id: str, body: dict | None = None):
    from vaults import backup_vault

    try:
        return backup_vault(vault_id, confirm=(body or {}).get("confirm") is True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/vaults/{vault_id}/export")
async def export_vault(vault_id: str, body: dict | None = None):
    require_usable_target_vault_context(vault_id)
    return await asyncio.to_thread(_export_vault_sync, vault_id, body or {})


def _export_vault_sync(vault_id: str, body: dict | None = None):
    from vaults import export_vault

    try:
        return export_vault(
            vault_id,
            confirm=(body or {}).get("confirm") is True,
            include_review=(body or {}).get("include_review") is True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class RelocateWorkspaceRequest(BaseModel):
    workspace_id: str
    new_config_path: str

class RelocateVaultRequest(BaseModel):
    vault_id: str
    new_vault_root: str

@router.post("/api/workspaces/{workspace_id}/load")
async def load_workspace(workspace_id: str):
    return await asyncio.to_thread(_load_workspace_sync, workspace_id)


def _load_workspace_sync(workspace_id: str):
    from workspaces import (
        DEFAULT_WORKSPACE_ID,
        _resolve,
        load_workspace_registry,
        save_workspace_registry,
    )

    with runtime_transitions.runtime_transition_lock():
        # One shared path is used by both workspace APIs. Preflight happens while
        # holding the same lock as the candidate load and commit.
        _ensure_runtime_switch_allowed()
        registry = load_workspace_registry()
        if workspace_id not in registry["workspaces"]:
            raise HTTPException(status_code=404, detail="Workspace not found")

        entry = registry["workspaces"][workspace_id]
        config_path = _resolve(entry.get("config_path") or "")
        if not config_path.exists():
            return {
                "status": "relocate_workspace",
                "message": f"Workspace configuration file not found at {config_path}",
                "config_path": str(config_path),
            }

        previous_registry = copy.deepcopy(registry)
        previous_ctx = try_get_runtime_context()
        previous_env = os.environ.get("LMZ_CONFIG_PATH", runtime_transitions.MISSING_ENV)
        activation_started = False

        try:
            # Stage and validate the candidate without mutating global runtime
            # state. The explicit path prevents the old environment override from
            # influencing candidate construction.
            new_ctx = build_runtime_context(config_path)
            vaults_root = new_ctx.root / "data" / "vaults"
            if workspace_id == DEFAULT_WORKSPACE_ID and not new_ctx.active_vault.root.exists() and not vaults_root.exists():
                from utils import setup_directories

                setup_directories(new_ctx)

            active_vault = new_ctx.active_vault
            recovery = not active_vault_is_usable(new_ctx)

            # Commit order:
            # 1) activate the fully staged candidate (context, logging, search,
            #    metadata services); 2) persist the active registry; 3) clear the
            #    one-shot LMZ_CONFIG_PATH override. Any later failure rolls back
            #    all three plus a full service rehydration of the old context.
            activation_started = True
            activate_runtime_context(new_ctx, hydrate=not recovery)
            configure_terminal_logging()

            candidate_registry = copy.deepcopy(registry)
            candidate_registry["active_workspace"] = workspace_id
            save_workspace_registry(candidate_registry)
            os.environ.pop("LMZ_CONFIG_PATH", None)

            if recovery:
                vault_root = Path(active_vault.root)
                return {
                    "status": "relocate_vault",
                    "message": f"Vault directory is missing or outside the workspace at {vault_root}",
                    "vault_id": active_vault.id,
                    "vault_name": active_vault.name,
                    "vault_root": str(vault_root),
                }

            return {
                "status": "success",
                "active_workspace": workspace_id,
                "active_vault": active_vault.id if active_vault else None,
            }
        except ConfigReadError as exc:
            rollback_errors = runtime_transitions.restore_workspace_switch_state(previous_registry, previous_env, previous_ctx) if activation_started else []
            detail = {"code": "unsupported_workspace_config", "message": str(exc)}
            if rollback_errors:
                detail["rollback_errors"] = rollback_errors
            raise HTTPException(status_code=422, detail=detail) from exc
        except ValueError as exc:
            rollback_errors = runtime_transitions.restore_workspace_switch_state(previous_registry, previous_env, previous_ctx) if activation_started else []
            detail: object = str(exc)
            if rollback_errors:
                detail = {"message": str(exc), "rollback_errors": rollback_errors}
            raise HTTPException(status_code=400, detail=detail) from exc
        except Exception as exc:
            rollback_errors = runtime_transitions.restore_workspace_switch_state(previous_registry, previous_env, previous_ctx) if activation_started else []
            log_system(
                "ERROR",
                "Failed to load workspace",
                workspace_id=workspace_id,
                error=str(exc),
                rollback_errors=rollback_errors,
            )
            detail = f"Failed to load workspace: {exc}"
            if rollback_errors:
                detail = {"message": detail, "rollback_errors": rollback_errors}
            raise HTTPException(status_code=500, detail=detail) from exc

@router.post("/api/workspaces/relocate")
async def relocate_workspace(body: RelocateWorkspaceRequest):
    return await asyncio.to_thread(_relocate_workspace_sync, body.workspace_id, body.new_config_path)

def _relocate_workspace_sync(workspace_id: str, new_config_path: str):
    from workspaces import _resolve, load_workspace_registry, save_workspace_registry
    registry = load_workspace_registry()
    if workspace_id not in registry["workspaces"]:
        raise HTTPException(status_code=404, detail="Workspace not found")
        
    resolved = _resolve(new_config_path)
    if not resolved.exists():
        raise HTTPException(status_code=400, detail=f"File does not exist: {resolved}")
        
    stored_path = str(resolved)
    try:
        resolved_abs = resolved.resolve()
        from app_paths import get_app_paths
        data_root = get_app_paths().data_root
        if resolved_abs.is_relative_to(data_root):
            stored_path = str(resolved_abs.relative_to(data_root)).replace("\\", "/")
    except Exception:
        pass
        
    registry["workspaces"][workspace_id]["config_path"] = stored_path
    save_workspace_registry(registry)
    return {"status": "success", "config_path": str(resolved)}

@router.post("/api/vaults/relocate")
async def relocate_vault(body: RelocateVaultRequest):
    require_workspace_context()
    return await asyncio.to_thread(_relocate_vault_sync, body.vault_id, body.new_vault_root)

def _relocate_vault_sync(vault_id: str, new_vault_root: str):
    from config_schema import WorkspaceConfig
    from vaults import (
        _capture_filesystem_paths,
        _capture_transition_snapshot,
        _cleanup_staged_config,
        _read_config,
        _record_rollback_errors,
        _remove_new_files,
        _restore_transition_snapshot,
        _stage_workspace_config,
        _write_config,
        vault_id_slug,
    )
    from workspaces import _resolve, load_workspace_registry, save_workspace_registry

    with runtime_transitions.runtime_transition_lock():
        _ensure_runtime_switch_allowed()
        ctx = get_runtime_context()
        snapshot = _capture_transition_snapshot(ctx)
        staged_config: Path | None = None
        target_root: Path | None = None
        target_paths_before: set[Path] | None = None
        target_mutation_started = False
        target_db_backup: Path | None = None
        try:
            config = _read_config(ctx)
            clean_id = vault_id_slug(vault_id)
            if "vaults" not in config or clean_id not in config["vaults"]:
                raise HTTPException(status_code=404, detail="Vault not found")

            resolved_root = Path(new_vault_root).expanduser().resolve()
            target_root = resolved_root
            if not target_root.exists():
                raise HTTPException(status_code=400, detail=f"Directory does not exist: {target_root}")

            try:
                stored_root = workspace_relative_path(target_root, ctx.root, label="vault root")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            candidate = copy.deepcopy(config)
            candidate["vaults"][clean_id]["root"] = stored_root
            WorkspaceConfig.model_validate(candidate)
            target_db = target_root / "db" / "lmz_main.db"
            if target_db.exists():
                target_db_backup = target_root.parent / f".lmz-relocate-db-{uuid.uuid4().hex}.bak"
                shutil.copy2(target_db, target_db_backup)
            staged_config = _stage_workspace_config(ctx.config_path, candidate)

            # Validate and hydrate the candidate while the durable config still
            # points at the old path. The old path remains untouched until the
            # candidate and all services are ready.
            staged_ctx = build_runtime_context(staged_config)
            candidate_ctx = replace(staged_ctx, config_path=ctx.config_path)
            target_paths_before = _capture_filesystem_paths(target_root)
            # Before activation, target files must be preserved if preparation fails.
            # After this point activation may create the database and vault folders.
            target_mutation_started = True
            activate_runtime_context(candidate_ctx, hydrate=True)
            configure_terminal_logging()

            _write_config(candidate, ctx)
            registry = load_workspace_registry()
            candidate_registry = copy.deepcopy(registry)
            matched_workspace = None
            for candidate_id, entry in registry.get("workspaces", {}).items():
                if _resolve(entry.get("config_path") or "") == ctx.config_path:
                    matched_workspace = candidate_id
                    break
            if matched_workspace is not None:
                candidate_registry["active_workspace"] = matched_workspace
                save_workspace_registry(candidate_registry)
            os.environ.pop("LMZ_CONFIG_PATH", None)
            return {"status": "success", "vault_root": str(target_root)}
        except HTTPException as exc:
            rollback_errors = _restore_transition_snapshot(snapshot)
            if target_mutation_started and target_root is not None and target_paths_before is not None:
                rollback_errors.extend(_remove_new_files(target_root, target_paths_before))
            if target_mutation_started and target_db_backup is not None and target_db_backup.exists() and target_root is not None:
                try:
                    shutil.copy2(target_db_backup, target_root / "db" / "lmz_main.db")
                except OSError as restore_exc:
                    rollback_errors.append(f"target database rollback failed: {restore_exc}")
            _record_rollback_errors(exc, rollback_errors)
            raise
        except Exception as exc:
            rollback_errors = _restore_transition_snapshot(snapshot)
            if target_mutation_started and target_root is not None and target_paths_before is not None:
                rollback_errors.extend(_remove_new_files(target_root, target_paths_before))
            if target_mutation_started and target_db_backup is not None and target_db_backup.exists() and target_root is not None:
                try:
                    shutil.copy2(target_db_backup, target_root / "db" / "lmz_main.db")
                except OSError as restore_exc:
                    rollback_errors.append(f"target database rollback failed: {restore_exc}")
            _record_rollback_errors(exc, rollback_errors)
            raise HTTPException(status_code=500, detail=f"Vault relocation failed: {exc}") from exc
        finally:
            _cleanup_staged_config(staged_config)
            if target_db_backup is not None:
                target_db_backup.unlink(missing_ok=True)


@router.post("/api/vaults/import")
async def import_vault(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_import_vault_sync, body)


@router.post("/api/vaults/import-preview")
async def import_vault_preview(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_import_vault_preview_sync, body)


@router.post("/api/vaults/restore-preview")
async def restore_backup_preview(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_restore_backup_preview_sync, body)


@router.post("/api/vaults/restore")
async def restore_backup(body: dict):
    require_workspace_context()
    return await asyncio.to_thread(_restore_backup_sync, body)


def _import_vault_preview_sync(body: dict):
    from vaults import preview_import_vault_package

    try:
        return preview_import_vault_package(
            str((body or {}).get("package_path") or "").strip(),
            target_name=str((body or {}).get("target_name") or (body or {}).get("name") or "").strip() or None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _restore_backup_preview_sync(body: dict):
    from vaults import preview_restore_backup_package

    try:
        return preview_restore_backup_package(
            str((body or {}).get("package_path") or "").strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _restore_backup_sync(body: dict):
    from vaults import restore_backup_package

    try:
        return restore_backup_package(
            str((body or {}).get("package_path") or "").strip(),
            package_fingerprint_value=str((body or {}).get("package_fingerprint") or "").strip(),
            confirm=(body or {}).get("confirm") is True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _import_vault_sync(body: dict):
    from vaults import import_vault_package

    try:
        return import_vault_package(
            str((body or {}).get("package_path") or "").strip(),
            target_name=str((body or {}).get("target_name") or (body or {}).get("name") or "").strip() or None,
            package_fingerprint_value=str((body or {}).get("package_fingerprint") or "").strip(),
            confirm=(body or {}).get("confirm") is True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
