"""Shared, framework-independent coordination for runtime transitions."""

import os
import threading
from contextlib import contextmanager

from ingest_control import local_ingest_lock, local_ingest_state
from metadata_index import metadata_repair_running
from queue_service import INGESTION_LOCK
from runtime_context import WorkspaceContext, get_runtime_context, has_runtime_context

# Workspace switches run in worker threads; vault switches must use the same
# process-wide lock so their staging, activation, commit, and rollback serialize.
_WORKSPACE_SWITCH_LOCK = threading.Lock()

# A unique sentinel distinguishes an absent override from an empty value.
MISSING_ENV = object()


class RuntimeSwitchBlockedError(RuntimeError):
    """Backend-level rejection with the blockers needed by the HTTP adapter."""

    def __init__(self, blockers: list[str]):
        self.blockers = list(blockers)
        super().__init__(", ".join(self.blockers))


@contextmanager
def runtime_transition_lock():
    """Serialize workspace and vault transitions within this backend process."""
    with _WORKSPACE_SWITCH_LOCK:
        yield


def runtime_switch_preflight(ctx: WorkspaceContext | None = None) -> dict:
    """Return active operations that prevent switching the runtime context."""
    runtime = ctx or get_runtime_context()
    blockers = []
    with local_ingest_lock(runtime):
        if local_ingest_state(runtime).get("running"):
            blockers.append("local_ingest_running")
    if INGESTION_LOCK.locked():
        blockers.append("online_ingest_running")
    if metadata_repair_running(runtime):
        blockers.append("metadata_repair_running")
    return {"allowed": not blockers, "blockers": blockers}


def ensure_runtime_switch_allowed(ctx: WorkspaceContext | None = None) -> None:
    """Raise a framework-neutral error when active work blocks a transition."""
    if ctx is None and not has_runtime_context():
        return
    preflight = runtime_switch_preflight(ctx)
    if not preflight["allowed"]:
        raise RuntimeSwitchBlockedError(preflight["blockers"])


def _restore_workspace_registry_snapshot(registry: dict) -> None:
    """Restore registry state without calling the normal, potentially failing save."""
    import workspaces
    from config_repository import WorkspaceRegistryRepository
    from config_schema import WorkspaceRegistry

    repository = WorkspaceRegistryRepository(workspaces.REGISTRY_PATH)
    value = WorkspaceRegistry.model_validate(registry)
    if not workspaces.REGISTRY_PATH.exists():
        repository.create(value)
        return
    current = repository.read()
    repository.replace(value, expected_etag=current.etag)


def restore_workspace_switch_state(
    previous_registry: dict,
    previous_env: object,
    previous_ctx,
) -> list[str]:
    """Best-effort full rollback; return errors so the API can report them."""
    errors: list[str] = []

    try:
        _restore_workspace_registry_snapshot(previous_registry)
    except Exception as exc:  # noqa: BLE001 - continue rollback even when a subsystem restore fails.
        errors.append(f"registry rollback failed: {exc}")

    try:
        if previous_env is MISSING_ENV:
            os.environ.pop("LMZ_CONFIG_PATH", None)
        else:
            os.environ["LMZ_CONFIG_PATH"] = str(previous_env)
    except Exception as exc:  # noqa: BLE001 - report this restore failure and continue rollback.
        errors.append(f"environment rollback failed: {exc}")

    try:
        if previous_ctx is None:
            from logger import reconfigure_logging
            from runtime_context import clear_runtime_context

            clear_runtime_context()
            reconfigure_logging(None)
        else:
            # Restore service state too; changing only the active-context pointer
            # would leave search and metadata services bound to the failed target.
            from runtime_activation import activate_runtime_context

            activate_runtime_context(previous_ctx, hydrate=True)
        from logger import configure_terminal_logging

        configure_terminal_logging()
    except Exception as exc:  # noqa: BLE001 - preserve the primary failure and report rollback diagnostics.
        errors.append(f"runtime-service rollback failed: {exc}")

    return errors
