import threading
from pathlib import Path

from runtime_context import WorkspaceContext, get_runtime_context

LOCAL_RESULTS_LIMIT = 500

_events_lock = threading.Lock()
_online_stop_events: dict[Path, threading.Event] = {}
_local_stop_events: dict[Path, threading.Event] = {}
_local_ingest_locks: dict[Path, threading.Lock] = {}
_local_ingest_states: dict[Path, dict] = {}
_local_ingest_state_lock = threading.Lock()


def _ctx_key(ctx: WorkspaceContext | None = None) -> Path:
    return (ctx or get_runtime_context()).active_vault.db_path.resolve()


def _event_for(store: dict[Path, threading.Event], ctx: WorkspaceContext | None = None) -> threading.Event:
    key = _ctx_key(ctx)
    with _events_lock:
        event = store.get(key)
        if event is None:
            event = threading.Event()
            store[key] = event
        return event


def online_stop_event(ctx: WorkspaceContext | None = None) -> threading.Event:
    return _event_for(_online_stop_events, ctx)


def local_stop_event(ctx: WorkspaceContext | None = None) -> threading.Event:
    return _event_for(_local_stop_events, ctx)


def _new_local_ingest_state() -> dict:
    return {
        "running": False,
        "phase": "idle",
        "run_id": None,
        "scanned": 0,
        "staged": 0,
        "queued": 0,
        "processed": 0,
        "summary": {"ingested": 0, "review": 0, "failed": 0, "duplicate": 0},
        "results": [],
        "failed_paths": [],
        "last_defaults": {},
        "last_skip_similarity": False,
        "started_at": None,
        "finished_at": None,
        "stop_requested": False,
    }


def local_ingest_state(ctx: WorkspaceContext | None = None) -> dict:
    """Get mutable ingestion status scoped to the context's active vault."""
    key = _ctx_key(ctx)
    with _local_ingest_state_lock:
        state = _local_ingest_states.get(key)
        if state is None:
            state = _new_local_ingest_state()
            _local_ingest_states[key] = state
        return state


def local_ingest_lock(ctx: WorkspaceContext | None = None) -> threading.Lock:
    key = _ctx_key(ctx)
    with _local_ingest_state_lock:
        lock = _local_ingest_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _local_ingest_locks[key] = lock
        return lock


def reset_local_ingest_state(ctx: WorkspaceContext | None = None) -> None:
    key = _ctx_key(ctx)
    with _local_ingest_state_lock:
        _local_ingest_states[key] = _new_local_ingest_state()
    local_stop_event(ctx).clear()


def clear_stop_flags(ctx: WorkspaceContext | None = None):
    online_stop_event(ctx).clear()
    local_stop_event(ctx).clear()
