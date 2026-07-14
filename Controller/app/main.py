from __future__ import annotations

import asyncio
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

from . import activity, auth, deploy_history, egress_watch, pg_admin, progress, registry, snowflake_client as sf
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
    EAI_SLOT_KEYS,
    EgressAckRequest,
    EgressAlertConfigRequest,
    HIDDEN_VALUE,
    ResourceTier,
    RollbackRequest,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
    UpdateExternalAccessRequest,
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
    try:
        deploy_history.init_table()
    except Exception:
        logger.exception("Failed to initialise MENDIX_DEPLOY_HISTORY")
    try:
        _refresh_platform_staleness()
    except Exception:
        logger.exception("Failed to refresh platform staleness")
    # Single background task, single-worker controller: runs an iteration
    # immediately, then every 24h (see egress_watch.run_loop's own docstring).
    # Cancelled on shutdown rather than left to die with the process, so a
    # unit test that spins the app up and down repeatedly doesn't accumulate
    # orphaned tasks.
    egress_task = asyncio.create_task(egress_watch.run_loop())
    try:
        yield
    finally:
        egress_task.cancel()
        try:
            await egress_task
        except asyncio.CancelledError:
            pass


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
# Per-app egress slots (manifest.yml's app_eai_1..4 references). setup_script.sql's
# start_controller only emits an APP_EAI_N env line once install_state marks that
# slot bound (reference() on an unbound ref fails DDL outright), so an unset/empty
# env here means "not bound yet" - never a placeholder value. Keyed by slot key
# (e.g. "app_eai_1"), valued by the real bound integration's name, which is what
# _compose_eai_names below actually needs to hand to EXTERNAL_ACCESS_INTEGRATIONS.
BOUND_EAI_SLOTS: dict[str, str] = {
    slot: os.environ[f"APP_EAI_{i}"]
    for i, slot in enumerate(EAI_SLOT_KEYS, start=1)
    if os.environ.get(f"APP_EAI_{i}")
}
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


def _compose_eai_names(slots: list[str]) -> list[str]:
    """The EXTERNAL_ACCESS_INTEGRATIONS list for a per-app service: PG_EAI
    unconditionally first (dropping it kills the app's own Postgres egress),
    then whichever requested slots are currently bound. The single place this
    composition happens - every caller (create_app, update_external_access,
    rollback) goes through here so "PG_EAI is always present" can never
    accidentally regress in one call site but not another.

    A slot in `slots` that isn't in BOUND_EAI_SLOTS is silently dropped rather
    than raising: request-time validation (422 for a requested-but-unbound
    slot) already happened at the endpoint layer for a fresh request, so
    reaching this filter only matters for a slot that WAS bound when it was
    requested/recorded but has since been unbound (e.g. replaying an old
    deploy-history row via rollback, or a consumer unbinding a reference out
    from under a running app) - see the BOUND_EAI_SLOTS comment above.
    """
    return [PG_EAI] + [BOUND_EAI_SLOTS[s] for s in slots if s in BOUND_EAI_SLOTS]


def _poll_status(
    service_name: str, target: str, timeout_secs: int = 300,
    on_tick: Callable[[int], None] | None = None,
) -> bool:
    start = time.time()
    deadline = start + timeout_secs
    while time.time() < deadline:
        status = sf.show_service_status(service_name)
        if status == target:
            return True
        if on_tick:
            on_tick(int(time.time() - start))
        time.sleep(10)
    return False


def _containers_all_ready(containers: list[dict]) -> bool:
    """True only when there is at least one container row and every one
    reports READY. An empty list (no rows, or show_service_containers'
    own swallowed-error path) is never "ready" - shared by GET
    /apps/{name}/health and _poll_ready so "ready" means the same thing
    in both places."""
    return bool(containers) and all((c.get("status") or "").upper() == "READY" for c in containers)


def _container_failure_detail(containers: list[dict]) -> str | None:
    """A status_detail fragment built from non-READY container rows (status
    plus message when SPCS gave one), for a RUNNING-but-never-ready poll
    timeout. None when there is nothing to report - e.g. the container list
    came back empty on every tick - so the caller can fall back to its own
    plain timeout text instead."""
    non_ready = [c for c in containers if (c.get("status") or "").upper() != "READY"]
    if not non_ready:
        return None
    pieces = []
    for c in non_ready:
        piece = f"{c.get('container_name') or 'container'}: {c.get('status') or 'UNKNOWN'}"
        if c.get("message"):
            piece += f" - {c['message']}"
        pieces.append(piece)
    return "; ".join(pieces)


def _poll_ready(
    service_name: str, target: str, timeout_secs: int = 300,
    on_tick: Callable[[int], None] | None = None,
    on_tick_ready: Callable[[int], None] | None = None,
) -> tuple[bool, str | None]:
    """Poll until the service reaches `target`, then - only when target is
    "RUNNING" - keep polling within the SAME timeout_secs deadline until every
    container SHOW SERVICE CONTAINERS reports READY. A service can report
    RUNNING as soon as a container instance starts, before its readinessProbe
    passes (see sf.show_service_containers's docstring), so service-level
    RUNNING alone is not enough to call an operation that ends in a running
    service successful. Any other target (e.g. suspend's "SUSPENDED") has no
    readiness concept, so it is returned as-is with no container calls at all.

    Returns (ok, detail). detail is always None on success. On failure it is
    None for a plain service-status timeout, or for a container list that
    never came back with any rows (the caller falls back to its own generic
    timeout text); it carries a container status/message summary when
    containers were seen but never all reached READY.
    """
    start = time.time()
    deadline = start + timeout_secs
    if not _poll_status(service_name, target, timeout_secs=timeout_secs, on_tick=on_tick):
        return False, None
    if target != "RUNNING":
        return True, None
    containers: list[dict] = []
    while time.time() < deadline:
        containers = sf.show_service_containers(service_name)
        if _containers_all_ready(containers):
            return True, None
        if on_tick_ready:
            on_tick_ready(int(time.time() - start))
        time.sleep(10)
    detail = _container_failure_detail(containers)
    return False, (f"Containers not ready after {timeout_secs}s: {detail}" if detail else None)


def _truncate(text: str, limit: int = 500) -> str:
    """Cap `text` at `limit` characters, appending a marker so a truncated
    status_detail (e.g. a long exception message) is distinguishable from one
    that happens to end exactly at the limit."""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


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
        # Purge this app's deploy-history rows too, so a name freed up by delete
        # is never re-registered onto a stranger's history. Harmless (a no-op
        # DELETE) when called from create_app's rollback, where nothing has been
        # recorded for this app yet.
        ("delete deploy history", lambda: deploy_history.delete_for_app(name)),
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
    # Reject rather than silently drop: a requested-but-unbound slot here would
    # otherwise create an app the operator believes has egress it doesn't - same
    # 422 posture as PUT /apps/{name}/external-access.
    unbound_eai = sorted(s for s in req.external_access if s not in BOUND_EAI_SLOTS)
    if unbound_eai:
        raise HTTPException(
            status_code=422,
            detail=f"external_access slot(s) not currently bound: {unbound_eai}",
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

        sf.create_service(service_name, spec, COMPUTE_POOL, _compose_eai_names(req.external_access), QUERY_WAREHOUSE)

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
            external_access=req.external_access,
            pad_stage_path=None,
            endpoint_url=None,
            # Non-transient: the app has no PAD yet. A transient status here would
            # disable the Redeploy action that performs the first deploy (deadlock).
            last_deploy_status="NOT_DEPLOYED",
            created_at=None,
            last_deployed_at=None,
            owner_role=req.owner_role,
            # The service was just created against a spec built with the current
            # image (see _build_spec / sf.create_service above), so it starts
            # current, not stale.
            platform_image=MENDIX_BASE_IMAGE,
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


@app.get("/apps/{name}/progress")
def get_app_progress(name: str, roles: set[str] = Depends(caller_roles)):
    """Live phase text for an in-flight background task on this app, or null when
    none is running. Same cheap authorization check as /logs (a registry lookup,
    no service-status/warehouse query) - the progress value itself comes straight
    out of the in-memory progress module."""
    _record_for_read(name, roles)
    return {"progress": progress.get_progress(name)}


@app.get("/apps/{name}/health")
def get_app_health(name: str, roles: set[str] = Depends(caller_roles)):
    """Container-level readiness for this app's service, distinct from the
    aggregate service_status GET /apps/{name} returns: a service can be
    RUNNING while its container hasn't yet passed the readinessProbe (see
    sf.show_service_containers). Both show_service_status and
    show_service_containers already swallow their own Snowflake errors and
    return None/[] respectively, so a suspended-or-missing service degrades to
    service_status=None, containers=[], ready=False here with no special-casing."""
    record = _record_for_read(name, roles)
    svc_status = sf.show_service_status(record.service_name)
    containers = sf.show_service_containers(record.service_name)
    ready = _containers_all_ready(containers)
    return {"service_status": svc_status, "containers": containers, "ready": ready}


@app.get("/apps/{name}/history")
def get_app_history(name: str, limit: int = 20, roles: set[str] = Depends(caller_roles)):
    """Newest-first deploy-history rows for this app, guarded the same way as
    GET /apps/{name} (a registry lookup, no service-status/warehouse query)."""
    _record_for_read(name, roles)
    return {"history": deploy_history.list_for_app(name, limit=limit)}


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


@app.get("/system/external-access")
def get_external_access_slots(roles: set[str] = Depends(caller_roles)):
    """Slot picker data for the four app_eai_N reference slots.

    Deliberately NOT gated to PRIVILEGED_ROLES, unlike every other /system/*
    endpoint above - deviation from the plan, noted here on purpose. Every
    operator registering a new app (2_Register.py) or editing an existing
    one's external access (1_Apps.py) needs this to render the slot
    checkboxes, not just privileged roles; there is also no owner_role to
    authorize against for a system-wide list like this one (contrast GET
    /apps, which resolves caller_roles the same way but has no upfront gate
    either - filtering there happens per-row instead). Any caller whose
    identity resolves at all (even to zero roles) can therefore read this.

    integration_name is withheld (null) from non-privileged callers: no other
    endpoint in this codebase surfaces an EAI's real Snowflake object name
    (get_pg_info returns the PG host/port but never the pg_eai integration
    name itself), so this endpoint doesn't start doing that either - the
    picker only needs key/bound/label to render its checkboxes.
    """
    privileged = bool(roles & auth.PRIVILEGED_ROLES)
    slots = []
    for i, key in enumerate(EAI_SLOT_KEYS, start=1):
        integration_name = BOUND_EAI_SLOTS.get(key)
        slots.append({
            "key": key,
            "bound": integration_name is not None,
            "integration_name": integration_name if privileged else None,
            "label": f"App egress integration {i} (optional)",
        })
    return {"slots": slots}


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


@app.get("/system/egress-status")
def get_egress_status(roles: set[str] = Depends(caller_roles)):
    """Full egress-whitelist detail for the Infrastructure page: privileged
    only, since it exposes the raw CIDR ranges and alert configuration (unlike
    GET /system/egress-warning below, the cheap unprivileged signal). Null-safe
    when egress_watch's background loop hasn't completed an iteration yet."""
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    min_expiry = sf.get_config(egress_watch.CONFIG_MIN_EXPIRY)
    ranges_raw = sf.get_config(egress_watch.CONFIG_RANGES)
    try:
        ranges = json.loads(ranges_raw) if ranges_raw else []
    except (json.JSONDecodeError, TypeError):
        ranges = []
    recipients_raw = sf.get_config(egress_watch.CONFIG_ALERT_RECIPIENTS)
    try:
        recipients = json.loads(recipients_raw) if recipients_raw else []
    except (json.JSONDecodeError, TypeError):
        recipients = []
    return {
        "min_expiry": min_expiry,
        "days_remaining": egress_watch.days_remaining(min_expiry),
        "ranges": ranges,
        "acknowledged_through": sf.get_config(egress_watch.CONFIG_ACK_THROUGH),
        "alert_integration": sf.get_config(egress_watch.CONFIG_ALERT_INTEGRATION),
        "alert_recipients": recipients,
    }


@app.post("/system/egress-ack")
def acknowledge_egress(req: EgressAckRequest, roles: set[str] = Depends(caller_roles)):
    """Record that the operator has seen and handled the current egress
    rotation through `through_date`; the banner/email both stay silent while
    the recorded min_expiry falls on or before this date (egress_watch.
    is_acknowledged), and reappear automatically once Snowflake rotates to a
    later expiry."""
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    through = req.through_date.isoformat()
    sf.set_config(egress_watch.CONFIG_ACK_THROUGH, through)
    return {"acknowledged_through": through}


@app.post("/system/egress-alert-config")
def set_egress_alert_config(req: EgressAlertConfigRequest, roles: set[str] = Depends(caller_roles)):
    """Save (or clear, with empty fields) the notification integration +
    recipient list egress_watch's daily loop uses to email an expiry warning.
    Storing a half-configured pair (one set, the other empty) is accepted -
    egress_watch simply never sends until both are present."""
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    integration = req.integration_name.strip() or None
    recipients = req.recipients or None
    sf.set_config(egress_watch.CONFIG_ALERT_INTEGRATION, integration)
    sf.set_config(egress_watch.CONFIG_ALERT_RECIPIENTS, json.dumps(recipients) if recipients else None)
    return {"alert_integration": integration, "alert_recipients": recipients or []}


@app.get("/system/egress-warning")
def get_egress_warning(roles: set[str] = Depends(caller_roles)):
    """Cheap unprivileged signal for the Apps-page banner: boolean + days
    remaining only - never the raw ranges or alert configuration (same
    posture as GET /system/external-access above: any caller whose identity
    resolves at all, even to zero roles, can read this). warn is true only
    within WARNING_THRESHOLD_DAYS and not already acknowledged, so a settled
    fleet with a fresh rotation shows nothing."""
    min_expiry = sf.get_config(egress_watch.CONFIG_MIN_EXPIRY)
    remaining = egress_watch.days_remaining(min_expiry)
    acked = egress_watch.is_acknowledged(min_expiry, sf.get_config(egress_watch.CONFIG_ACK_THROUGH))
    warn = remaining is not None and remaining < egress_watch.WARNING_THRESHOLD_DAYS and not acked
    return {"warn": warn, "days_remaining": remaining}


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
    deploy used to leave both empty). Also clears any stale FAILED status_detail /
    failed_operation left over from a prior attempt on this app, and (since every
    caller of this helper rebuilds and applies a fresh spec) stamps platform_image
    to the image this controller is currently running with - platform_update_available
    is cleared alongside it since a freshly-applied spec is by definition current."""
    update = {
        "endpoint_url": sf.get_service_endpoint(service_name),
        "last_deploy_status": "READY",
        "last_deployed_at": datetime.now(timezone.utc).isoformat(),
        "status_detail": None,
        "failed_operation": None,
        "platform_image": MENDIX_BASE_IMAGE,
        "platform_update_available": False,
    }
    if extra:
        update.update(extra)
    registry.update_app(name, update)


def _is_platform_stale(record: AppRecord) -> bool:
    """True if this app's last-applied service spec was built against a different
    MENDIX_BASE_IMAGE than the one this controller is currently running with. A
    record with no platform_image recorded is stale only if it has actually
    deployed at least once (last_deployed_at set) - an app that was only
    registered and never deployed has no running spec to be stale."""
    if record.platform_image is None:
        return record.last_deployed_at is not None
    return record.platform_image != MENDIX_BASE_IMAGE


def _refresh_platform_staleness() -> None:
    """Recompute platform_update_available for every registered app against the
    image this controller instance is running with. Runs once at startup rather
    than on a timer: a respec that happens later in this controller's lifetime
    already stamps platform_image/platform_update_available itself (see
    _stamp_deploy_success, _run_update_spec), so this only needs to catch up
    apps that went stale across a controller restart (a new image was rolled
    out). Writes only rows whose computed value actually changed, so a settled
    fleet costs zero registry writes."""
    for record in registry.list_apps():
        stale = _is_platform_stale(record)
        if stale != record.platform_update_available:
            registry.update_app(record.name, {"platform_update_available": stale})


def _record_history(name: str, op: str, record: AppRecord | None, status: str,
                    detail: str | None) -> None:
    """Best-effort deploy-history write: a failure here must never affect the
    lifecycle task's own outcome, so every error is swallowed and logged.
    `record` is None for operations that don't mutate the service spec/deployment
    (suspend, resume) - those are simply never recorded (see deploy_history's
    module docstring)."""
    if record is None:
        return
    try:
        deploy_history.record(name, op, record, status, detail)
    except Exception:
        logger.exception("Failed to record deploy history for %s (op=%s)", name, op)


def _run_lifecycle_task(
    name: str,
    service_name: str,
    action_fn: Callable[[], None],
    *,
    op: str,
    target_status: str,
    timeout_secs: int,
    on_success: Callable[[], None],
    error_message: str,
    record: AppRecord | None = None,
) -> None:
    """Shared skeleton for every background service-restart task (deploy,
    constants/spec/license/role-mapping update, suspend, resume): run
    action_fn (rebuild+apply a spec, or suspend/resume the service), poll
    until the service reaches target_status, then on_success. A poll timeout
    marks the app FAILED with a status_detail explaining the timeout. Any
    exception - including one raised by action_fn or on_success - marks the
    app FAILED with a truncated str(exc) and logs via
    logger.exception(error_message, name). `op` (e.g. "deploy", "constants",
    "suspend") is recorded as failed_operation so the UI can say which action
    failed; it is not otherwise interpreted here.

    Also drives the in-memory progress module (progress.py) so the Admin UI
    can show a live phase caption without a warehouse query: "applying
    changes" while action_fn runs, then "waiting for {target_status} ({N}s)"
    on each poll tick, then (once the service itself is RUNNING) "waiting for
    containers ready ({N}s)" until every container passes readiness, cleared
    in the finally block regardless of outcome.

    `record`, when given, is an AppRecord already reflecting the values this
    operation is applying (callers build it via record.model_copy(update=...)
    so no extra registry read is needed here); it drives a deploy_history row
    on both the success and failure paths. Passed only by callers whose
    operation mutates the service spec/deployment - suspend/resume never pass
    it, so they are simply never recorded.
    """
    try:
        progress.set_progress(name, "applying changes")
        action_fn()
        ok, ready_detail = _poll_ready(
            service_name, target_status, timeout_secs=timeout_secs,
            on_tick=lambda elapsed: progress.set_progress(
                name, f"waiting for {target_status} ({elapsed}s)"
            ),
            on_tick_ready=lambda elapsed: progress.set_progress(
                name, f"waiting for containers ready ({elapsed}s)"
            ),
        )
        if not ok:
            detail = ready_detail or f"Timed out waiting for {target_status} after {timeout_secs}s"
            registry.update_app(name, {
                "last_deploy_status": "FAILED",
                "status_detail": detail,
                "failed_operation": op,
            })
            _record_history(name, op, record, "FAILED", detail)
            return
        on_success()
        _record_history(name, op, record, "READY", None)
    except Exception as exc:
        detail = _truncate(str(exc))
        try:
            registry.update_app(name, {
                "last_deploy_status": "FAILED",
                "status_detail": detail,
                "failed_operation": op,
            })
        except Exception:
            pass
        _record_history(name, op, record, "FAILED", detail)
        logger.exception(error_message, name)
    finally:
        progress.clear_progress(name)


def _run_deploy(name: str, pad_path: str, record: AppRecord,
                pad_constants: list[PadConstant], new_constants: dict,
                user_roles: list[str]) -> None:
    """Background deploy task. registry status must be set to DEPLOYING before calling."""
    # Computed upfront (both inputs are already known before the background task
    # runs) rather than inside action(): it's needed both to build the spec and to
    # snapshot the deploy-history row, and computing it once keeps both in sync.
    # Normalized to forward slashes: the path lands in a Linux container's env var
    # regardless of the OS the Controller (or its test suite) runs on.
    pad_relative_path = os.path.relpath(pad_path, DEPLOY_STAGE_MOUNT).replace(os.sep, "/")

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
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           _constants_from_dict(new_constants), record.use_caller_rights, record.license_id,
                           record.role_mapping, pad_relative_path)
        sf.alter_service_spec(record.service_name, spec)

    def on_success() -> None:
        _stamp_deploy_success(name, record.service_name, {
            "constants": new_constants,
            "pad_stage_path": pad_relative_path,
            "user_roles": user_roles,
        })

    history_record = record.model_copy(update={"constants": new_constants, "pad_stage_path": pad_relative_path})
    _run_lifecycle_task(name, record.service_name, action, op="deploy", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Deploy failed for %s", record=history_record)


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


def _rollback_config(fields: dict) -> dict:
    """The subset of an AppRecord (or a deploy_history row - both use the same
    key names) that identifies a deployment configuration, normalized for a
    cheap dict-equality comparison. external_access is included in the
    identity check (see _run_rollback, which now re-applies it too), so a
    stored history row that differs only in external_access is still treated
    as a distinct configuration rather than silently matching the live state."""
    return {
        "pad_stage_path": fields.get("pad_stage_path"),
        "resource_tier": fields.get("resource_tier"),
        "use_caller_rights": bool(fields.get("use_caller_rights")),
        "license_id": fields.get("license_id"),
        "role_mapping": fields.get("role_mapping") or {},
        "external_access": fields.get("external_access") or [],
    }


def _run_rollback(name: str, record: AppRecord, target: dict) -> None:
    """Background task for a configuration rollback: rebuild the spec from a
    deploy-history row's resource_tier/use_caller_rights/license_id/
    role_mapping/pad_stage_path, but the CURRENT environment (image, PG host -
    same as every other _build_spec caller) and the app's CURRENT constants.
    Constant values are never restored: only names are ever snapshotted in
    history (values live only in secrets), so "rollback" here means restoring
    which deployment configuration is live, not replaying point-in-time
    constant values. The recorded external_access slot set IS re-applied, via
    set_service_external_access after the spec is applied - through
    _compose_eai_names like every other caller, so a slot that was bound at
    snapshot time but has since been unbound is silently dropped rather than
    failing the rollback.
    """
    new_tier = ResourceTier(target["resource_tier"])
    new_caller = bool(target["use_caller_rights"])
    new_license_id = target["license_id"]
    new_role_mapping = target["role_mapping"] or None
    pad_relative_path = target["pad_stage_path"]
    new_external_access = target["external_access"] or []

    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, new_tier, constants_list,
                           new_caller, new_license_id, new_role_mapping, pad_relative_path)
        sf.alter_service_spec(record.service_name, spec)
        sf.set_service_external_access(record.service_name, _compose_eai_names(new_external_access))

    def on_success() -> None:
        _stamp_deploy_success(name, record.service_name, {
            "resource_tier": str(new_tier.value),
            "use_caller_rights": new_caller,
            "license_id": new_license_id,
            "role_mapping": new_role_mapping,
            "pad_stage_path": pad_relative_path,
            "external_access": new_external_access,
        })

    history_record = record.model_copy(update={
        "resource_tier": str(new_tier.value),
        "use_caller_rights": new_caller,
        "license_id": new_license_id,
        "role_mapping": new_role_mapping or {},
        "pad_stage_path": pad_relative_path,
        "external_access": new_external_access,
    })
    _run_lifecycle_task(name, record.service_name, action, op="rollback", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Rollback failed for %s", record=history_record)


@app.post("/apps/{name}/rollback", status_code=202)
def rollback_app(name: str, background_tasks: BackgroundTasks,
                req: RollbackRequest | None = None,
                roles: set[str] = Depends(caller_roles)):
    """Roll back to a deploy-history configuration: PAD, resource tier, caller's
    rights, license, role mapping, AND external_access are restored from that
    history row. Constant VALUES are never snapshotted (they are secrets, see
    deploy_history's module docstring), so this always redeploys with the
    app's CURRENT constants from the registry - it restores which deployment
    configuration is live, not point-in-time constant values.

    With no body (or entry_id omitted/null), targets the last successful
    deploy (deploy_history.last_success). With `{"entry_id": N}`, targets that
    specific history row instead - it must belong to this app and be READY
    (404 if it doesn't exist for this app, 409 if it exists but isn't READY);
    either way the same identical-config and PAD-still-staged checks below
    apply to whichever row was chosen.
    """
    record = _record_for_mutation(name, roles)
    entry_id = req.entry_id if req else None
    if entry_id is not None:
        target = deploy_history.get_entry(name, entry_id)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"History entry {entry_id} not found for app '{name}'",
            )
        if target["status"] != "READY":
            raise HTTPException(
                status_code=409,
                detail=f"History entry {entry_id} is {target['status']}, not READY - cannot roll back to it",
            )
    else:
        target = deploy_history.last_success(name)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"App '{name}' has no successful deployment recorded to roll back to",
            )

    # Only a no-op when the app is actually healthy on that configuration right
    # now. A FAILED app's registry row still reflects its last-good config (the
    # fields a failed deploy/spec change would have overwritten are only written
    # in on_success - see _run_deploy/_run_update_spec), not what the service is
    # currently (mis)running, so re-applying it is precisely the recovery this
    # endpoint exists for - it must not 409 just because the recorded fields
    # happen to match.
    current = _rollback_config(record.model_dump())
    if record.last_deploy_status == "READY" and current == _rollback_config(target):
        raise HTTPException(
            status_code=409,
            detail=f"App '{name}' is already running this configuration",
        )

    pad_relative_path = target["pad_stage_path"]
    pad_abs_path = os.path.join(DEPLOY_STAGE_MOUNT, *pad_relative_path.split("/")) if pad_relative_path else None
    if not pad_relative_path or not os.path.isfile(pad_abs_path):
        raise HTTPException(
            status_code=409,
            detail=f"PAD file '{pad_relative_path}' is no longer on the stage; re-upload it.",
        )

    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_rollback, name, record, target)
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

    history_record = record.model_copy(update={"constants": merged})
    _run_lifecycle_task(name, service_name, action, op="constants", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Constants update failed for %s",
                        record=history_record)


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
                     new_caller: bool, *, op: str = "spec") -> None:
    """Rebuild and apply the spec for a tier/caller-rights change - or, with
    op="platform_update", for no config change at all, purely to pick up a new
    MENDIX_BASE_IMAGE (see apply_platform_update). Either way the rebuilt spec
    always bakes in the image this controller is currently running with, so
    on_success stamps platform_image/clears platform_update_available same as
    _stamp_deploy_success (not reused directly: this path also persists the new
    resource_tier/use_caller_rights, which _stamp_deploy_success's callers don't)."""
    def action() -> None:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, new_tier, constants_list, new_caller,
                           record.license_id, record.role_mapping, record.pad_stage_path)
        sf.alter_service_spec(record.service_name, spec)

    new_tier_str = str(new_tier.value) if hasattr(new_tier, "value") else str(new_tier)

    def on_success() -> None:
        registry.update_app(name, {
            "resource_tier": new_tier_str,
            "use_caller_rights": new_caller,
            "last_deploy_status": "READY",
            "status_detail": None,
            "failed_operation": None,
            "platform_image": MENDIX_BASE_IMAGE,
            "platform_update_available": False,
        })

    history_record = record.model_copy(update={"resource_tier": new_tier_str, "use_caller_rights": new_caller})
    _run_lifecycle_task(name, record.service_name, action, op=op, target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Spec update failed for %s",
                        record=history_record)


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


@app.post("/apps/{name}/platform-update", status_code=202)
def apply_platform_update(name: str, background_tasks: BackgroundTasks,
                          roles: set[str] = Depends(caller_roles)):
    """Respec the app onto the current MENDIX_BASE_IMAGE with no other config
    change - the same rebuild-and-restart _run_update_spec already does for a
    tier/caller-rights edit, just with both left unchanged."""
    record = _record_for_mutation(name, roles)
    if not record.platform_update_available:
        raise HTTPException(status_code=409, detail=f"App '{name}' has no platform update available")
    _require_pad_deployed(name, record)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(
        _run_update_spec, name, record, ResourceTier(record.resource_tier), bool(record.use_caller_rights),
        op="platform_update",
    )
    return {"status": "DEPLOYING"}


def _run_update_external_access(name: str, service_name: str, record: AppRecord, slots: list[str]) -> None:
    """Background task for an external-access change. Unlike every other
    mutation in this file, this one never calls _build_spec/alter_service_spec:
    set_service_external_access is a standalone ALTER SERVICE ... SET
    EXTERNAL_ACCESS_INTEGRATIONS clause that doesn't touch the rest of the
    spec, so there's no PAD/constants/tier/license/role-mapping dependency here
    at all - only the slot list, composed through _compose_eai_names like
    every other caller of it."""
    registry.update_app(name, {"external_access": slots})

    def action() -> None:
        sf.set_service_external_access(service_name, _compose_eai_names(slots))

    def on_success() -> None:
        _stamp_deploy_success(name, service_name)

    history_record = record.model_copy(update={"external_access": slots})
    _run_lifecycle_task(name, service_name, action, op="external_access", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="External access update failed for %s",
                        record=history_record)


@app.put("/apps/{name}/external-access", status_code=202)
def update_external_access(name: str, req: UpdateExternalAccessRequest, background_tasks: BackgroundTasks,
                          roles: set[str] = Depends(caller_roles)):
    """Attach/detach this app's service to/from the given app_eai_N slots (the
    full desired set, not a delta). No _require_pad_deployed guard: unlike
    every other spec-touching mutation, this doesn't rebuild the spec at all,
    so an app that has never deployed a PAD can still have its external access
    configured ahead of its first deploy.
    """
    record = _record_for_mutation(name, roles)
    unbound = sorted(s for s in req.slots if s not in BOUND_EAI_SLOTS)
    if unbound:
        raise HTTPException(
            status_code=422,
            detail=f"Slot(s) not currently bound, cannot attach: {unbound}",
        )
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_external_access, name, record.service_name, record, req.slots)
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

    history_record = record.model_copy(update={"license_id": license_id})
    _run_lifecycle_task(name, service_name, action, op="license", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="License update failed for %s",
                        record=history_record)


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

    history_record = record.model_copy(update={"license_id": None})
    _run_lifecycle_task(name, service_name, action, op="license", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="License removal failed for %s",
                        record=history_record)


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

    history_record = record.model_copy(update={"role_mapping": role_mapping})
    _run_lifecycle_task(name, service_name, action, op="role_mapping", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Role mapping update failed for %s",
                        record=history_record)


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

    history_record = record.model_copy(update={"role_mapping": None})
    _run_lifecycle_task(name, service_name, action, op="role_mapping", target_status="RUNNING", timeout_secs=300,
                        on_success=on_success, error_message="Role mapping removal failed for %s",
                        record=history_record)


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
        registry.update_app(name, {
            "last_deploy_status": "SUSPENDED",
            "status_detail": None,
            "failed_operation": None,
        })

    _run_lifecycle_task(name, service_name, lambda: sf.suspend_service(service_name),
                        op="suspend", target_status="SUSPENDED", timeout_secs=120,
                        on_success=on_success, error_message="Suspend failed for %s")


def _run_resume(name: str, service_name: str) -> None:
    def on_success() -> None:
        registry.update_app(name, {
            "last_deploy_status": "READY",
            "status_detail": None,
            "failed_operation": None,
        })

    _run_lifecycle_task(name, service_name, lambda: sf.resume_service(service_name),
                        op="resume", target_status="RUNNING", timeout_secs=300,
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
