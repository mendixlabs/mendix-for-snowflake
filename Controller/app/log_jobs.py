"""In-memory job store for background log downloads.

SYSTEM$GET_SERVICE_LOGS has no offset/pagination argument - every call returns
only a tail of the most recent lines, hard-capped by Snowflake at 1000
regardless of the requested value. A background download therefore cannot
fetch more history than the live Logs view already could; its value is
dodging the SPCS ingress timeout on a request that may be slow for reasons
unrelated to line count, plus handing back a saveable file.

The controller runs as a single uvicorn process/worker (Controller/Dockerfile
has no --workers flag), so a plain dict guarded by a lock is sufficient - no
external queue needed. Keyed by an unguessable job id; each job also records
the app/system key it belongs to so a caller who only has read access to one
app can't poll another app's job.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import BackgroundTasks, HTTPException

from . import snowflake_client as sf

logger = logging.getLogger(__name__)

LOG_DOWNLOAD_LINES = 1000

_log_jobs: dict[str, dict] = {}
_log_jobs_lock = threading.Lock()
_LOG_JOB_TTL_SECS = 1800  # prune finished jobs this long after they finish


def _prune_log_jobs(now: float) -> None:
    stale = [
        job_id for job_id, job in _log_jobs.items()
        if job["finished_at"] is not None and now - job["finished_at"] > _LOG_JOB_TTL_SECS
    ]
    for job_id in stale:
        del _log_jobs[job_id]


def _run_log_download(job_id: str, service_name: str, container: str) -> None:
    try:
        logs = sf.get_service_logs(service_name, container=container, lines=LOG_DOWNLOAD_LINES)
        with _log_jobs_lock:
            _log_jobs[job_id].update(status="READY", logs=logs, error=None, finished_at=time.time())
    except Exception as e:
        logger.exception("Log download failed for %s", service_name)
        with _log_jobs_lock:
            _log_jobs[job_id].update(status="FAILED", logs=None, error=str(e), finished_at=time.time())


def _start_log_download(job_key: str, service_name: str, container: str,
                        background_tasks: BackgroundTasks) -> str:
    job_id = uuid.uuid4().hex
    with _log_jobs_lock:
        _prune_log_jobs(time.time())
        _log_jobs[job_id] = {
            "job_key": job_key, "status": "PENDING", "logs": None, "error": None, "finished_at": None,
        }
    background_tasks.add_task(_run_log_download, job_id, service_name, container)
    return job_id


def _get_log_job(job_id: str, job_key: str) -> dict:
    """Look up a log-download job, scoped to the caller's app/system key.

    A job's key must match exactly: this is what stops an operator who can only
    read app A from polling app B's job even if they somehow learned its id.
    """
    with _log_jobs_lock:
        job = _log_jobs.get(job_id)
    if not job or job["job_key"] != job_key:
        raise HTTPException(status_code=404, detail="Log download job not found")
    return job
