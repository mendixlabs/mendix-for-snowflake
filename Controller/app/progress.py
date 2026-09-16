"""In-memory per-app progress text for in-flight background lifecycle tasks.

Same rationale as log_jobs.py: the controller runs as a single uvicorn
process/worker, so a plain dict guarded by a lock is sufficient - no external
queue or persistence needed. Progress is coarse, human-readable phase text
("applying changes", "waiting for RUNNING (45s)"), not a structured status;
_run_lifecycle_task (main.py) is the only writer, GET /apps/{name}/progress
the only external reader. Never written to the registry: it is disposable
UI sugar, not state that needs to survive a controller restart.

Keyed by uppercased app name, matching every other case-insensitive app
lookup in this codebase (registry.get_app, _service_name, etc.) - callers here
pass whatever case the request URL or a background task happened to use.
"""
from __future__ import annotations

import threading

_progress: dict[str, str] = {}
_lock = threading.Lock()


def set_progress(name: str, text: str) -> None:
    with _lock:
        _progress[name.upper()] = text


def get_progress(name: str) -> str | None:
    with _lock:
        return _progress.get(name.upper())


def clear_progress(name: str) -> None:
    with _lock:
        _progress.pop(name.upper(), None)
