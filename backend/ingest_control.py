import threading
from pathlib import Path

from runtime_context import WorkspaceContext, get_runtime_context

_events_lock = threading.Lock()
_online_stop_events: dict[Path, threading.Event] = {}
_local_stop_events: dict[Path, threading.Event] = {}


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


def clear_stop_flags(ctx: WorkspaceContext | None = None):
    online_stop_event(ctx).clear()
    local_stop_event(ctx).clear()
