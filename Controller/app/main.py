from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from . import activity, auth, pg_admin, registry, snowflake_client as sf
# LOG_DOWNLOAD_LINES, _LOG_JOB_TTL_SECS and _log_jobs aren't referenced by this
# module's own code (only _get_log_job/_start_log_download are); they're
# imported anyway so they stay reachable as main.<name>, which the test suite
# relies on (conftest's per-test job-store reset, log-download TTL/line-cap
# assertions).
from .log_jobs import (
    LOG_DOWNLOAD_LINES,
    _LOG_JOB_TTL_SECS,
    _log_jobs,
    _get_log_job,
    _start_log_download,
)
from .models import (
    AppRecord,
    AppStatusResponse,
    CreateAppRequest,
    HIDDEN_VALUE,
    ResourceTier,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
    UpdateLicenseRequest,
    UpdateRoleMappingRequest,
    UpdateSpecRequest,
)
from .pad_parser import PadConstant, parse_from_zip, parse_user_roles_from_zip
from .spec_builder import _build_spec


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        activity.init_table()
    except Exception:
        logger.exception("Failed to initialise MENDIX_ACTIVITY")
    yield


app = FastAPI(title="Mendix SPCS Deployment Controller", lifespan=lifespan)


@app.middleware("http")
async def log_operator(request: Request, call_next):
    is_mutation = request.method in ("POST", "PUT", "PATCH", "DELETE")
    response = await call_next(request)
    if is_mutation:
        # Identify the operator. The admin UI sets X-Operator; direct API clients
        # don't, so resolve the real Snowflake user from the caller token (a cache
        # hit, since the route dependency already resolved it).
        operator = request.headers.get("X-Operator")
        if not operator:
            try:
                operator = auth.resolve_caller(request).user
            except Exception:
                operator = None
        operator = operator or "<anonymous>"
        action, app_name = activity.derive_action(request.method, request.url.path)
        # Record the real outcome: 2xx accepted the call, anything else was rejected
        # (authorization, validation, or a synchronous error). Background deploy
        # outcomes are tracked separately in the registry's last_deploy_status.
        result = "accepted" if response.status_code < 400 else f"rejected ({response.status_code})"
        logger.info("operator=%s %s %s -> %s", operator, request.method, request.url.path, response.status_code)
        try:
            activity.insert(
                operator=operator,
                action=action,
                app_name=app_name,
                detail={"path": request.url.path, "method": request.method, "status": response.status_code},
                result=result,
            )
        except Exception:
            logger.exception("Failed to record activity row")
    return response

DB_SCHEMA = sf.require_env("DB_SCHEMA")
# DB_SCHEMA is "<db>.APP_PUBLIC"; per-app schemas live next to it in the same
# database. Empty prefix when unqualified (local dev outside SPCS).
_DB_PREFIX = DB_SCHEMA.rsplit(".", 1)[0] + "." if "." in DB_SCHEMA else ""
COMPUTE_POOL = sf.require_env("COMPUTE_POOL")
IMAGE_REPO = sf.require_env("IMAGE_REPO")
# Full image reference for per-app Mendix base services. Dev default is the repo
# path + :latest; the release build (build-and-push.ps1) pins this to an immutable
# @sha256 digest in the controller's service spec, so a frozen app version always
# launches the exact image that passed the security review (not a moving :latest).
MENDIX_BASE_IMAGE = os.environ.get("MENDIX_BASE_IMAGE", f"/{IMAGE_REPO}:latest")
PG_EAI = sf.require_env("PG_EAI")
QUERY_WAREHOUSE = sf.require_env("QUERY_WAREHOUSE")
DEPLOY_STAGE = f"@{DB_SCHEMA}.MENDIX_DEPLOY_STAGE"
DEPLOY_STAGE_MOUNT = "/mnt/deploy-stage"

# Infrastructure services whose own logs are exposed via /system/logs/{target}.
# (service_name, container). Defaults match the names created by the setup scripts;
# override via env if a deployment renames them.
CONTROLLER_SERVICE_NAME = os.environ.get("CONTROLLER_SERVICE_NAME", "MENDIX_DEPLOY_CONTROLLER")
ADMIN_UI_SERVICE_NAME = os.environ.get("ADMIN_UI_SERVICE_NAME", "MENDIX_DEPLOY_ADMIN_UI")
SYSTEM_SERVICES: dict[str, tuple[str, str]] = {
    "controller": (CONTROLLER_SERVICE_NAME, "controller"),
    "admin-ui": (ADMIN_UI_SERVICE_NAME, "streamlit"),
}

# Derived from the bound pg_secret at startup
_PG_HOST: str | None = None
_PG_PASSWORD: str | None = None


def _load_pg_credentials(force_reload: bool = False) -> tuple[str, str]:
    """Read the bound pg_secret (GENERIC_STRING) mounted at /secrets/pg.

    The secret string is JSON: {"host": "<host:port>", "password": "<pw>"}.
    Both values are cached after the first read. Falls back to PG_HOST / PG_PASS
    env vars for local development outside SPCS.

    Pass force_reload=True to bypass the cache and re-read the file: create_app
    does this so a rotated pg_secret password is picked up for newly created apps
    without waiting for a controller restart.
    """
    global _PG_HOST, _PG_PASSWORD
    if force_reload:
        _PG_HOST = None
        _PG_PASSWORD = None
    if _PG_HOST is None or _PG_PASSWORD is None:
        secret_file = "/secrets/pg/secret_string"
        if os.path.exists(secret_file):
            with open(secret_file) as f:
                raw = f.read()
            try:
                data = json.loads(raw)
                _PG_HOST = str(data["host"]).strip()
                _PG_PASSWORD = str(data["password"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                raise RuntimeError(
                    "pg_secret at /secrets/pg/secret_string must be JSON with "
                    '"host" and "password" keys, e.g. '
                    '{"host": "<host:port>", "password": "<pw>"}'
                ) from e
        else:
            _PG_HOST = os.environ.get("PG_HOST", "localhost:5432")
            _PG_PASSWORD = os.environ.get("PG_PASS", "")
    return _PG_HOST, _PG_PASSWORD


def _pg_host() -> str:
    return _load_pg_credentials()[0]


def _service_name(app_name: str) -> str:
    return f"{app_name.upper()}_SERVICE"


def _app_schema_name(app_name: str) -> str:
    # Everything an app owns (secrets, filestorage stage) lives in its own
    # schema, so ownership is containment rather than a naming convention and
    # delete is a single DROP SCHEMA ... CASCADE. Prefix MXAPP_, not APP_: an
    # app named "public" must not resolve to the shared APP_PUBLIC schema.
    return f"MXAPP_{app_name.upper()}"


def _schema_fqn(schema: str) -> str:
    return f"{_DB_PREFIX}{schema}"


def _filestorage_stage(app_schema: str) -> str:
    return f"{_schema_fqn(app_schema)}.FILESTORAGE_STAGE"


def _secret_fqn(app_schema: str, name: str) -> str:
    return f"{_schema_fqn(app_schema)}.{name.upper()}"


def _pg_username(app_name: str) -> str:
    """Per-app Postgres role name - the single source of truth used both when
    provisioning the role (pg_admin.provision_app) and when building the
    service spec (RUNTIME_PARAMS_DATABASEUSERNAME), so the two always match."""
    return f"app_{app_name.lower()}_role"


def _const_secret_name(const_name: str) -> str:
    return "MX_CONST_" + const_name.replace(".", "_").upper()


def _poll_status(service_name: str, target: str, timeout_secs: int = 300) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        status = sf.show_service_status(service_name)
        if status == target:
            return True
        time.sleep(10)
    return False


def _sync_constant_secrets(app_schema: str, constants: list[PadConstant], values: dict[str, str]) -> None:
    for c in constants:
        val = values.get(c.name, c.default)
        if val == HIDDEN_VALUE:
            # The registry stores only the masking sentinel, never real values;
            # seeing it here means "keep the existing secret".
            continue
        sf.create_or_replace_secret(_secret_fqn(app_schema, c.secret_name), val)


def _constants_from_dict(d: dict[str, str]) -> list[PadConstant]:
    return [
        PadConstant(name=k, env_var="", default=v,
                    secret_name=_const_secret_name(k))
        for k, v in d.items()
    ]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def caller_roles(request: Request) -> set[str]:
    """FastAPI dependency: the authoritative role set for the request."""
    return auth.resolve_caller_roles(request)


def _record_for_read(name: str, roles: set[str]) -> AppRecord:
    """Load an app the caller may see, else 404 (unauthorized is indistinguishable
    from missing, so existence is not leaked)."""
    record = registry.get_app(name)
    if not record or not auth.authorize(record.owner_role, roles):
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    return record


# Statuses meaning a background task is already changing this app's service
# state (ALTER SERVICE / suspend / resume). Mirrors the Admin UI's client-side
# _TRANSIENT set (Admin UI/app/pages/1_Apps.py), which disables the
# corresponding buttons for the same reason - kept in sync manually since the
# two packages don't share a module.
_TRANSIENT_STATUSES = {"DEPLOYING", "SUSPENDING", "RESUMING"}


def _record_for_mutation(name: str, roles: set[str], *, block_transient: bool = True) -> AppRecord:
    """Load an app the caller may mutate: 404 if missing, 403 if not authorized,
    409 if block_transient and a prior mutation on this app hasn't finished yet
    (last_deploy_status in _TRANSIENT_STATUSES). Without this guard, two
    near-simultaneous calls launch competing ALTER SERVICE operations and race
    on last_deploy_status; block_transient=False is for callers where that
    race does not apply.
    """
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    if not auth.authorize(record.owner_role, roles):
        raise HTTPException(status_code=403, detail=f"Not authorized for app '{name}'")
    if block_transient and record.last_deploy_status in _TRANSIENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"App '{name}' is currently {record.last_deploy_status} - another change is "
                   "already in progress. Wait for it to finish and retry.",
        )
    return record


def _require_pad_deployed(name: str, record: AppRecord) -> None:
    """Block a spec-rebuild mutation on an app that has never deployed a PAD.

    Such an app has no pad_stage_path, so _build_spec falls back to the
    conventional apps/{name}/current.zip path when rebuilding the spec. That
    could restart the service against whatever PAD happens to be staged there
    without ever running _prepare_deploy's parsing (user_roles, pad_stage_path
    itself). Require an explicit Redeploy first so those fields are always
    populated by the code path that actually parses the PAD. Applies to every
    endpoint that rebuilds and re-applies the service spec (constants, spec,
    license, role mapping), not just constants.
    """
    if record.pad_stage_path is None:
        raise HTTPException(
            status_code=409,
            detail=f"App '{name}' has no PAD deployed yet - Redeploy first. "
                   "Changing its configuration alone would restart the service "
                   "against whatever PAD happens to be staged without recording "
                   "it or detecting its userroles.",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


def _endpoint_is_real(url: str | None) -> bool:
    """A real ingress host has a dot and no spaces; the provisioning placeholder
    ("Endpoints provisioning in progress. ...") has spaces."""
    return bool(url) and " " not in url and "." in url


def _effective_endpoint(record: AppRecord, svc_status: str | None) -> str | None:
    """Return the app's endpoint, healing a stale/empty stored value.

    endpoint_url is captured once at deploy time, but SPCS provisions ingress
    asynchronously after the service reports RUNNING, so that capture is usually
    empty (or, from older builds, a stored provisioning message). When the
    service is RUNNING and we have no real stored endpoint, fetch the live one
    and persist it so later reads stay cheap."""
    if _endpoint_is_real(record.endpoint_url):
        return record.endpoint_url
    if svc_status == "RUNNING":
        live = sf.get_service_endpoint(record.service_name)
        if live:
            registry.update_app(record.name, {"endpoint_url": live})
            return live
    return None


@app.get("/apps")
def list_apps(response: Response, roles: set[str] = Depends(caller_roles)):
    apps = registry.list_apps()
    try:
        statuses = sf.show_all_service_statuses(strict=True)
    except Exception:
        # Distinguishes "the statuses query failed" from "there are genuinely no
        # services" - both otherwise look identical ({}) to every app row below.
        statuses = {}
        response.headers["X-Service-Status-Unavailable"] = "true"
    result = []
    for a in apps:
        if not auth.authorize(a.owner_role, roles):
            continue
        svc_status = statuses.get(a.service_name)
        result.append({
            **a.model_dump(),
            "service_status": svc_status,
            "endpoint_url": _effective_endpoint(a, svc_status),
        })
    return result


def _teardown_app_objects(name: str, service_name: str, app_schema: str,
                          pg_database: str | None = None) -> list[str]:
    """Best-effort drop of every Snowflake object create_app makes, in reverse,
    plus (when pg_database is given) the app's per-app Postgres role/database.

    Every drop is IF EXISTS, so it is safe to call whether or not the object was
    created (partial create_app failure) and safe to retry. Returns the list of
    step names that raised, so the caller can decide whether to keep a registry
    row alive as a retry handle. Shared by delete_app and create_app's rollback.
    """
    cleanup_steps = [
        # Dropping the service auto-drops its service roles (revoking the
        # endpoint grant from app_admin); the per-app application role
        # persists, so drop it separately.
        ("drop service", lambda: sf.drop_service(service_name)),
        ("drop application role", lambda: sf.drop_app_access_role(name)),
        # The app's schema contains everything it owns: credential secrets
        # (PG password, admin password, constants) and the filestorage stage.
        # CASCADE removes them all, including the user's uploaded files; the
        # admin UI warns about this before the delete.
        ("drop schema", lambda: sf.drop_schema_cascade(_schema_fqn(app_schema))),
    ]
    if pg_database is not None:
        # Drop the Snowflake objects first, then release the Postgres role and
        # database last: the Snowflake drops never depend on Postgres being
        # reachable, so a transient Postgres connection failure only blocks
        # this one step rather than derailing the rest of the cleanup.
        def _deprovision_pg() -> None:
            host, bootstrap_pw = _load_pg_credentials()
            pg_admin.deprovision_app(host, bootstrap_pw, pg_database, _pg_username(name))

        cleanup_steps.append(("deprovision postgres", _deprovision_pg))

    failures = []
    for step, run in cleanup_steps:
        try:
            run()
        except Exception as exc:
            logger.warning("teardown %s: %s failed: %s", name, step, exc)
            failures.append(step)
    return failures


@app.post("/apps", status_code=status.HTTP_201_CREATED)
def create_app(req: CreateAppRequest, background_tasks: BackgroundTasks,
               roles: set[str] = Depends(caller_roles)):
    if not auth.authorize(req.owner_role, roles):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot assign owner_role '{req.owner_role}': not one of your roles",
        )
    if registry.get_app(req.name):
        raise HTTPException(status_code=409, detail=f"App '{req.name}' already exists")
    masked = [n for n, v in req.constants.items() if v == HIDDEN_VALUE]
    if masked:
        raise HTTPException(
            status_code=422,
            detail=f"Constants {masked} have the reserved value '{HIDDEN_VALUE}'; "
                   "a new app has no existing secrets to keep - provide real values",
        )

    service_name = _service_name(req.name)
    app_schema = _app_schema_name(req.name)

    # Create Snowflake objects and the registry row as one unit. If any step
    # raises mid-sequence, roll back every object we made so a partial create
    # doesn't orphan a schema/service with no registry row to retry or clean up
    # from (contrast delete_app, which survives partial failure by design).
    try:
        # The app's own schema holds everything it owns (secrets, filestorage
        # stage); delete_app removes it with one DROP SCHEMA ... CASCADE.
        sf.create_schema(_schema_fqn(app_schema))
        sf.create_stage(_filestorage_stage(app_schema))

        # Create PG password and admin password secrets.
        # Read the bootstrap PG password from the controller's bound pg_secret (/secrets/pg).
        # Force a fresh read so a rotated pg_secret isn't served stale to new apps.
        # req.pg_database is the target database name, not the password.
        pg_host, bootstrap_password = _load_pg_credentials(force_reload=True)
        if not bootstrap_password:
            raise HTTPException(status_code=409, detail="Controller PG credentials not mounted at /secrets/pg")
        # Provision a dedicated Postgres role + database scoped to only this app
        # (see pg_admin module docstring): the container is handed this per-app
        # credential, never the shared bootstrap one, so a breached container
        # cannot reach any other app's database.
        app_password = pg_admin.provision_app(pg_host, bootstrap_password, req.pg_database, _pg_username(req.name))
        sf.create_or_replace_secret(_secret_fqn(app_schema, "PG_PASS"), app_password)
        sf.create_or_replace_secret(_secret_fqn(app_schema, "ADMIN_PASS"), req.admin_password)

        # Born-licensed app: the key is a credential (write straight to its secret,
        # never held in a local variable beyond this call); the id is stored on the
        # record below like any other plain field. CreateAppRequest validates both-or-neither.
        if req.license_id and req.license_key:
            sf.create_or_replace_secret(_secret_fqn(app_schema, "MX_LICENSE_KEY"), req.license_key)

        # Create constant secrets from provided values (using defaults for any not supplied)
        constants: list[PadConstant] = []  # no PAD yet at create time
        for const_name, value in req.constants.items():
            secret_name = _const_secret_name(const_name)
            sf.create_or_replace_secret(_secret_fqn(app_schema, secret_name), value)
            constants.append(PadConstant(name=const_name, env_var="", default=value, secret_name=secret_name))

        spec = _build_spec(req.name, app_schema, req.pg_database, req.resource_tier, constants, req.use_caller_rights,
                           req.license_id)

        sf.create_service(service_name, spec, COMPUTE_POOL, PG_EAI, QUERY_WAREHOUSE)

        # Data-plane access control: gate the public endpoint behind a per-app
        # APPLICATION role. End-user membership of app_<name>_user is managed
        # in the IdP via SCIM (GRANT APPLICATION ROLE ... TO USER). Also grant app_admin
        # so any operator can reach the app before the IdP group is populated (owner
        # bootstrap; replaces the old owner_role grant - an application cannot grant its
        # service role to a consumer account role).
        sf.create_app_access_role(req.name)
        sf.grant_endpoint_to_app_role(service_name, sf.app_access_role_name(req.name))
        sf.grant_endpoint_to_app_role(service_name, sf.APP_ADMIN_ROLE)

        # Endpoint URL is not available until the service starts; it's captured by _run_deploy.
        record = AppRecord(
            name=req.name,
            service_name=service_name,
            app_schema=app_schema,
            pg_database=req.pg_database,
            resource_tier=req.resource_tier,
            use_caller_rights=req.use_caller_rights,
            constants=req.constants,
            license_id=req.license_id,
            pad_stage_path=None,
            endpoint_url=None,
            # Non-transient: the app has no PAD yet. A transient status here would
            # disable the Redeploy action that performs the first deploy (deadlock).
            last_deploy_status="NOT_DEPLOYED",
            created_at=None,
            last_deployed_at=None,
            owner_role=req.owner_role,
        )
        registry.create_app(record)
    except Exception as exc:
        # Best-effort rollback of whatever was created before the failure. Every
        # drop is IF EXISTS, so dropping objects that were never created is safe.
        rollback_failures = _teardown_app_objects(req.name, service_name, app_schema,
                                                  pg_database=req.pg_database)
        # Preserve the original status for validation failures (e.g. the 409 for
        # unmounted PG credentials); only unexpected errors become a 502.
        if isinstance(exc, HTTPException):
            raise
        detail = f"Failed to create app '{req.name}': {exc}"
        if rollback_failures:
            detail += f"; rollback also failed ({', '.join(rollback_failures)}) - manual cleanup may be needed"
        else:
            detail += "; created objects were rolled back"
        raise HTTPException(status_code=502, detail=detail) from exc

    # The service was just created against the pre-deploy placeholder spec
    # (PAD_STAGE_PATH = apps/{name}/current.zip), which only exists if the
    # operator's real PAD happens to be named that. If a PAD was already
    # staged under this app's real name before Register was clicked - the
    # normal order once staging uses `snow stage copy` with the operator's
    # own filename - deploy it immediately instead of leaving the service to
    # crash-loop against the placeholder until a separate manual Redeploy.
    # Uses the identical _prepare_deploy/_run_deploy path trigger_deploy uses,
    # so pad_stage_path/user_roles/constants end up populated the same way
    # regardless of whether staging happened before or after registration.
    # Failure here (e.g. the PAD declares constants with no value yet) is not
    # fatal to registration - it leaves the app NOT_DEPLOYED, exactly like the
    # stage-after-register order, and the operator resolves it with a manual
    # Redeploy the same way trigger_deploy's own callers already do.
    staged_pad = _resolve_staged_pad(req.name)
    if staged_pad:
        try:
            record, pad_constants, new_constants, user_roles = _prepare_deploy(req.name, staged_pad)
            registry.update_app(req.name, {"last_deploy_status": "DEPLOYING"})
            background_tasks.add_task(
                _run_deploy, req.name, staged_pad, record, pad_constants, new_constants, user_roles
            )
            return {"service_name": service_name, "status": "DEPLOYING"}
        except Exception:
            logger.exception("Auto-deploy of already-staged PAD failed for %s", req.name)

    return {"service_name": service_name, "status": "NOT_DEPLOYED"}


@app.get("/apps/{name}")
def get_app(name: str, roles: set[str] = Depends(caller_roles)):
    record = _record_for_read(name, roles)
    svc_status = sf.show_service_status(record.service_name)
    record.endpoint_url = _effective_endpoint(record, svc_status)
    return AppStatusResponse(app=record, service_status=svc_status)


@app.get("/apps/{name}/logs")
def get_logs(name: str, lines: int = 200, roles: set[str] = Depends(caller_roles)):
    record = _record_for_read(name, roles)
    try:
        logs = sf.get_service_logs(record.service_name, lines=lines)
    except Exception as e:
        # Surface the underlying reason (e.g. the container is mid-restart) instead of
        # an opaque 500; the Admin UI's auto-refreshing log viewer treats 502 as transient.
        raise HTTPException(status_code=502, detail=f"Could not read logs for {name}: {e}")
    return {"logs": logs}


@app.get("/system/logs/{target}")
def get_system_logs(target: str, lines: int = 200, roles: set[str] = Depends(caller_roles)):
    """Logs for the infrastructure services themselves (controller, admin UI).

    Restricted to privileged roles: these logs span every tenant's operator
    activity, so they sit outside the per-app owner_role isolation.
    """
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="System logs are restricted to privileged roles")
    entry = SYSTEM_SERVICES.get(target)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown system service '{target}'")
    service_name, container = entry
    try:
        logs = sf.get_service_logs(service_name, container=container, lines=lines)
    except Exception as e:
        # Surface the underlying reason (e.g. the controller's role lacks access to
        # another service's logs) instead of an opaque 500.
        raise HTTPException(status_code=502, detail=f"Could not read {target} logs: {e}")
    return {"logs": logs}


@app.post("/apps/{name}/logs/download", status_code=202)
def start_log_download(name: str, background_tasks: BackgroundTasks,
                       roles: set[str] = Depends(caller_roles)):
    """Fetch up to LOG_DOWNLOAD_LINES lines in the background and return a job id.

    Sidesteps the SPCS ingress timeout on a slow log fetch (see O12); it does not
    retrieve more history than /apps/{name}/logs already can, since
    SYSTEM$GET_SERVICE_LOGS has no pagination and is hard-capped at that many lines.
    """
    record = _record_for_read(name, roles)
    job_id = _start_log_download(name.upper(), record.service_name, "mendix-app", background_tasks)
    return {"job_id": job_id, "status": "PENDING"}


@app.get("/apps/{name}/logs/download/{job_id}")
def get_log_download(name: str, job_id: str, roles: set[str] = Depends(caller_roles)):
    _record_for_read(name, roles)
    job = _get_log_job(job_id, name.upper())
    return {"status": job["status"], "logs": job["logs"], "error": job["error"]}


@app.post("/system/logs/{target}/download", status_code=202)
def start_system_log_download(target: str, background_tasks: BackgroundTasks,
                              roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="System logs are restricted to privileged roles")
    entry = SYSTEM_SERVICES.get(target)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown system service '{target}'")
    service_name, container = entry
    job_id = _start_log_download(f"system:{target}", service_name, container, background_tasks)
    return {"job_id": job_id, "status": "PENDING"}


@app.get("/system/logs/{target}/download/{job_id}")
def get_system_log_download(target: str, job_id: str, roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="System logs are restricted to privileged roles")
    if target not in SYSTEM_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown system service '{target}'")
    job = _get_log_job(job_id, f"system:{target}")
    return {"status": job["status"], "logs": job["logs"], "error": job["error"]}


@app.get("/system/compute-pool")
def get_compute_pool(roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    pool = sf.get_compute_pool(COMPUTE_POOL)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Compute pool '{COMPUTE_POOL}' not found")
    return pool


@app.get("/system/pg-info")
def get_pg_info(roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    host_port = _pg_host()
    host, _, port = host_port.rpartition(":")
    return {"host": host or host_port, "port": port or None}


@app.patch("/system/compute-pool")
def update_compute_pool(req: UpdateComputePoolRequest, roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    if req.min_nodes is None and req.max_nodes is None and req.auto_suspend_secs is None:
        raise HTTPException(status_code=400, detail="At least one field must be provided")
    # A Snowflake failure here (invalid sizing, pool busy, transient error) must
    # surface as a 502, not a raw 500, mirroring the logs routes.
    try:
        sf.alter_compute_pool(
            COMPUTE_POOL,
            min_nodes=req.min_nodes,
            max_nodes=req.max_nodes,
            auto_suspend_secs=req.auto_suspend_secs,
        )
        pool = sf.get_compute_pool(COMPUTE_POOL)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to update compute pool: {exc}") from exc
    return pool or {}


def _prepare_deploy(
    name: str, pad_path: str
) -> tuple[AppRecord, list[PadConstant], dict, list[str]]:
    """Parse and validate a PAD. Returns (record, pad_constants, new_constants,
    user_roles). Raises HTTPException on error."""
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    pad_constants = parse_from_zip(pad_path)
    stored = record.constants or {}
    missing = [c.name for c in pad_constants if c.name not in stored and not c.default]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"detail": "New constants with no value", "missing": missing},
        )

    new_constants = {**stored}
    for c in pad_constants:
        if c.name not in new_constants:
            new_constants[c.name] = c.default

    user_roles = parse_user_roles_from_zip(pad_path)
    if record.role_mapping and user_roles:
        orphaned = sorted(set(record.role_mapping.values()) - set(user_roles))
        if orphaned:
            logger.warning(
                "App %s: role_mapping targets userroles not in the redeployed PAD: %s",
                name, orphaned,
            )

    return record, pad_constants, new_constants, user_roles


def _stamp_deploy_success(name: str, service_name: str, extra: dict | None = None) -> None:
    """Record a successful deploy/restart: capture the live endpoint, stamp the deploy
    time, and mark the app READY. Shared by every background task that restarts a
    service so they all populate endpoint_url + last_deployed_at (a constants-only
    deploy used to leave both empty)."""
    update = {
        "endpoint_url": sf.get_service_endpoint(service_name),
        "last_deploy_status": "READY",
        "last_deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        update.update(extra)
    registry.update_app(name, update)


def _run_lifecycle_task(
    name: str,
    service_name: str,
    action_fn: Callable[[], None],
    *,
    target_status: str,
    timeout_secs: int,
    on_success: Callable[[], None],
    error_message: str,
) -> None:
    """Shared skeleton for every background service-restart task (deploy,
    constants/spec/license/role-mapping update, suspend, resume): run
    action_fn (rebuild+apply a spec, or suspend/resume the service), poll
    until the service reaches target_status, then on_success. A poll timeout
    marks the app FAILED (silently, matching the original per-route
    behavior). Any exception - including one raised by action_fn or
    on_success - marks the app FAILED and logs via
    logger.exception(error_message, name)."""
    try:
        action_fn()
        if not _poll_status(service_name, target_status, timeout_secs=timeout_secs):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        on_success()
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception(error_message, name)


def _run_deploy(name: str, pad_path: str, record: AppRecord,
                pad_constants: list[PadConstant], new_constants: dict,
                user_roles: list[str]) -> None:
    """Background deploy task. registry status must be set to DEPLOYING before calling."""
    holder: dict = {}

    def action() -> None:
        stored = record.constants or {}
        constants_changed = any(
            new_constants.get(c.name) != stored.get(c.name)
            for c in pad_constants
        )

        if constants_changed:
            _sync_constant_secrets(record.app_schema, pad_constants, new_constants)
            # Persist constants alongside the secret sync so a failed restart cannot
            # leave the registry and the per-app secrets out of step (see the same
            # fix in _run_update_constants).
            registry.update_app(name, {"constants": new_constants})

        # Always rebuild the spec, even when constants didn't change: PAD_STAGE_PATH
        # must track whatever file was actually resolved for this deploy (it may
        # differ from the previous deploy's filename even with identical constants).
        # Normalized to forward slashes: the path lands in a Linux container's env
        # var regardless of the OS the Controller (or its test suite) runs on.
        pad_relative_path = os.path.relpath(pad_path, DEPLOY_STAGE_MOUNT).replace(os.sep, "/")
        holder["pad_relative_path"] = pad_relative_path
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           _constants_from_dict(new_constants), record.use_caller_rights, record.license_id,
                           record.role_mapping, pad_relative_path)
        sf.alter_service_spec(record.service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, record.service_name, {
            "constants": new_constants,
            "pad_stage_path": holder["pad_relative_path"],
            "user_roles": user_roles,
        })

    _run_lifecycle_task(name, record.service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Deploy failed for %s")


def _resolve_staged_pad(name: str) -> str | None:
    """Find the PAD a consumer staged under apps/<name>/.

    Always the newest .zip in the directory by mtime - no special-casing of
    any filename (in particular, no current.zip preference). That means the
    documented `snow stage copy <yourpad>.zip @.../apps/<name>/` one-liner
    works with the operator's own filename, and staging a fresh build under a
    new name always wins over an older file left behind under any name,
    including a current.zip from before this preference was removed.
    """
    app_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
    if not os.path.isdir(app_dir):
        return None
    zips = [
        os.path.join(app_dir, f)
        for f in os.listdir(app_dir)
        if f.lower().endswith(".zip") and os.path.isfile(os.path.join(app_dir, f))
    ]
    if not zips:
        return None
    # Break mtime ties by filename so the choice is deterministic even when two
    # files share a timestamp (coarse stage-mount mtime resolution, or two stage
    # copies within the same second). os.listdir order alone is not stable.
    return max(zips, key=lambda z: (os.path.getmtime(z), z))


@app.post("/apps/{name}/trigger-deploy", status_code=202)
def trigger_deploy(name: str, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    """Trigger deploy from whichever .zip is newest under apps/{name}/ (any filename)."""
    _record_for_mutation(name, roles)
    pad_path = _resolve_staged_pad(name)
    if pad_path is None:
        raise HTTPException(
            status_code=400,
            detail=f"No PAD (.zip) found at stage path apps/{name}/ — upload it first.",
        )
    record, pad_constants, new_constants, user_roles = _prepare_deploy(name, pad_path)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_deploy, name, pad_path, record, pad_constants, new_constants, user_roles)
    return {"status": "DEPLOYING"}


def _run_update_constants(name: str, service_name: str, merged: dict,
                          record: AppRecord, constants: list[PadConstant]) -> None:
    """Background task for constants update."""
    # Persist constants up front, independent of the restart outcome: the per-app
    # secrets are already written by the endpoint handler, so a failed restart must
    # not discard the registry copy (otherwise the UI shows constants as {}).
    registry.update_app(name, {"constants": merged})

    def action() -> None:
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants, record.use_caller_rights, record.license_id, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)

    _run_lifecycle_task(name, service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Constants update failed for %s")


@app.put("/apps/{name}/constants", status_code=202)
def update_constants(name: str, req: UpdateConstantsRequest, background_tasks: BackgroundTasks,
                     roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    stored = record.constants or {}

    # HIDDEN_VALUE means "keep the existing secret" - valid only for constants
    # that already have one. For a new name it would leave the rebuilt spec
    # mounting a secret that was never created.
    unknown_masked = [n for n, v in req.constants.items()
                      if v == HIDDEN_VALUE and n not in stored]
    if unknown_masked:
        raise HTTPException(
            status_code=422,
            detail=f"Constants {unknown_masked} are new but have the reserved "
                   f"value '{HIDDEN_VALUE}' - provide real values",
        )

    changed = {n: v for n, v in req.constants.items() if v != HIDDEN_VALUE}
    if not changed:
        return {"status": "UNCHANGED"}

    _require_pad_deployed(name, record)

    for const_name, value in changed.items():
        sf.create_or_replace_secret(_secret_fqn(record.app_schema, _const_secret_name(const_name)), value)

    merged = {**stored, **req.constants}
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_constants, name, record.service_name, merged, record, _constants_from_dict(merged))
    return {"status": "DEPLOYING"}


def _run_update_spec(name: str, record: AppRecord, new_tier: ResourceTier,
                     new_caller: bool) -> None:
    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, new_tier, constants_list, new_caller,
                           record.license_id, record.role_mapping, record.pad_stage_path)
        sf.alter_service_spec(record.service_name, spec)

    def on_success() -> None:
        registry.update_app(name, {
            "resource_tier": str(new_tier.value) if hasattr(new_tier, "value") else str(new_tier),
            "use_caller_rights": new_caller,
            "last_deploy_status": "READY",
        })

    _run_lifecycle_task(name, record.service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Spec update failed for %s")


@app.put("/apps/{name}/spec", status_code=202)
def update_spec(name: str, req: UpdateSpecRequest, background_tasks: BackgroundTasks,
                roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    _require_pad_deployed(name, record)
    if req.resource_tier is None and req.use_caller_rights is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of resource_tier or use_caller_rights must be provided",
        )

    new_tier = req.resource_tier if req.resource_tier is not None else ResourceTier(record.resource_tier)
    new_caller = req.use_caller_rights if req.use_caller_rights is not None else bool(record.use_caller_rights)

    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_spec, name, record, new_tier, new_caller)
    return {"status": "DEPLOYING"}


def _run_update_license(name: str, service_name: str, record: AppRecord, license_id: str) -> None:
    """Background task for a license set/replace. The secret is already written by
    the endpoint handler; this restarts the service so the runtime picks it up (it
    only checks the license at startup) and persists the id."""
    registry.update_app(name, {"license_id": license_id})

    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants_list, record.use_caller_rights, license_id, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)

    _run_lifecycle_task(name, service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="License update failed for %s")


@app.put("/apps/{name}/license", status_code=202)
def update_license(name: str, req: UpdateLicenseRequest, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    _require_pad_deployed(name, record)
    # No HIDDEN_VALUE semantics: the key is write-only, so every PUT carries a real
    # key. Written before the registry/spec update, same ordering as constants.
    sf.create_or_replace_secret(_secret_fqn(record.app_schema, "MX_LICENSE_KEY"), req.license_key)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_license, name, record.service_name, record, req.license_id)
    return {"status": "DEPLOYING"}


def _run_delete_license(name: str, service_name: str, record: AppRecord) -> None:
    """Background task for license removal: revert to trial and restart, then drop
    the now-unused secret once the restart has actually happened."""
    registry.update_app(name, {"license_id": None})

    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants_list, record.use_caller_rights, None, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)
        sf.drop_secret(_secret_fqn(record.app_schema, "MX_LICENSE_KEY"))

    _run_lifecycle_task(name, service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="License removal failed for %s")


@app.delete("/apps/{name}/license", status_code=202)
def delete_license(name: str, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    _require_pad_deployed(name, record)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_delete_license, name, record.service_name, record)
    return {"status": "DEPLOYING"}


def _run_update_role_mapping(name: str, service_name: str, record: AppRecord,
                             role_mapping: dict[str, str]) -> None:
    """Persist up front (same ordering as license_id), rebuild the spec with
    MX_ROLE_MAPPING, restart so the SSO handler sees it at next login."""
    registry.update_app(name, {"role_mapping": role_mapping})

    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database,
                           ResourceTier(record.resource_tier), constants_list,
                           record.use_caller_rights, record.license_id, role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)

    _run_lifecycle_task(name, service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Role mapping update failed for %s")


@app.put("/apps/{name}/role-mapping", status_code=202)
def update_role_mapping(name: str, req: UpdateRoleMappingRequest,
                        background_tasks: BackgroundTasks,
                        roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    _require_pad_deployed(name, record)
    if record.user_roles:
        unknown = sorted(set(req.role_mapping.values()) - set(record.user_roles))
        if unknown:
            raise HTTPException(status_code=422, detail={
                "detail": "Mapping targets userroles not present in the deployed PAD",
                "unknown_userroles": unknown,
                "detected_userroles": record.user_roles,
            })
    warnings = []
    if not record.user_roles:
        warnings.append("No userroles detected in the deployed PAD; mapping values are unvalidated.")
    if not record.use_caller_rights:
        warnings.append("use_caller_rights is off: no caller token reaches the app, so the "
                        "mapping is inert and all users get the default role until it is enabled.")
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_role_mapping, name, record.service_name,
                              record, req.role_mapping)
    return {"status": "DEPLOYING", "warnings": warnings}


def _run_delete_role_mapping(name: str, service_name: str, record: AppRecord) -> None:
    """Background task for role-mapping removal: no secret to drop, so this is simpler
    than _run_delete_license."""
    registry.update_app(name, {"role_mapping": None})

    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database,
                           ResourceTier(record.resource_tier), constants_list,
                           record.use_caller_rights, record.license_id, None,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)

    _run_lifecycle_task(name, service_name, action, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Role mapping removal failed for %s")


@app.delete("/apps/{name}/role-mapping", status_code=202)
def delete_role_mapping(name: str, background_tasks: BackgroundTasks,
                        roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    _require_pad_deployed(name, record)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_delete_role_mapping, name, record.service_name, record)
    return {"status": "DEPLOYING"}


@app.get("/activity")
def list_activity(app: Optional[str] = None, operator: Optional[str] = None, limit: int = 100,
                  roles: set[str] = Depends(caller_roles)):
    rows = activity.query(app=app, operator=operator, limit=limit)
    if roles & auth.PRIVILEGED_ROLES:
        return rows
    # Non-privileged operators see only activity for apps they own. Rows with no
    # app (e.g. create attempts) are visible only to privileged roles.
    visible = {a.name for a in registry.list_apps() if auth.authorize(a.owner_role, roles)}
    return [r for r in rows if r.get("app_name") in visible]


def _run_suspend(name: str, service_name: str) -> None:
    def on_success() -> None:
        registry.update_app(name, {"last_deploy_status": "SUSPENDED"})

    _run_lifecycle_task(name, service_name, lambda: sf.suspend_service(service_name),
                        target_status="SUSPENDED", timeout_secs=120,
                        on_success=on_success, error_message="Suspend failed for %s")


def _run_resume(name: str, service_name: str) -> None:
    def on_success() -> None:
        registry.update_app(name, {"last_deploy_status": "READY"})

    _run_lifecycle_task(name, service_name, lambda: sf.resume_service(service_name),
                        target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Resume failed for %s")


@app.post("/apps/{name}/suspend", status_code=202)
def suspend_app(name: str, background_tasks: BackgroundTasks,
                roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    registry.update_app(name, {"last_deploy_status": "SUSPENDING"})
    background_tasks.add_task(_run_suspend, name, record.service_name)
    return {"status": "SUSPENDING"}


@app.post("/apps/{name}/resume", status_code=202)
def resume_app(name: str, background_tasks: BackgroundTasks,
               roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    registry.update_app(name, {"last_deploy_status": "RESUMING"})
    background_tasks.add_task(_run_resume, name, record.service_name)
    return {"status": "RESUMING"}


@app.delete("/apps/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(name: str, roles: set[str] = Depends(caller_roles)):
    # Delete runs its suspend + cleanup synchronously in this request rather than
    # as a background task, so it doesn't race a concurrent ALTER SERVICE the way
    # the other mutations do; blocking it on a transient status would also remove
    # the only way to clear an app stuck mid-transition.
    record = _record_for_mutation(name, roles, block_transient=False)

    try:
        sf.suspend_service(record.service_name)
        _poll_status(record.service_name, "SUSPENDED", timeout_secs=60)
    except Exception:
        pass

    # Attempt every drop even when an earlier one fails, then keep the registry
    # row alive if anything failed: the row is the operator's only handle for
    # retrying, and deleting it while a service or schema survives would leak
    # that object with nothing left pointing at it. Deleting the app also drops
    # its Postgres database and role - intentional, since delete is already
    # destructive and there is no install base to protect.
    failures = _teardown_app_objects(name, record.service_name, record.app_schema,
                                     pg_database=record.pg_database)
    if failures:
        raise HTTPException(
            status_code=502,
            detail=f"Cleanup failed ({', '.join(failures)}); retry the delete",
        )

    registry.delete_app(name)
