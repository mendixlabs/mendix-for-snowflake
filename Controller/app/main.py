from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import activity, auth, registry, snowflake_client as sf
from .models import (
    AppRecord,
    AppStatusResponse,
    CreateAppRequest,
    HIDDEN_VALUE,
    RESOURCE_TIERS,
    ResourceTier,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
    UpdateLicenseRequest,
    UpdateRoleMappingRequest,
    UpdateSpecRequest,
)
from .pad_parser import PadConstant, parse_from_zip, parse_user_roles_from_zip


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


def _load_pg_credentials() -> tuple[str, str]:
    """Read the bound pg_secret (GENERIC_STRING) mounted at /secrets/pg.

    The secret string is JSON: {"host": "<host:port>", "password": "<pw>"}.
    Both values are cached after the first read. Falls back to PG_HOST / PG_PASS
    env vars for local development outside SPCS.
    """
    global _PG_HOST, _PG_PASSWORD
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


def _const_secret_name(const_name: str) -> str:
    return "MX_CONST_" + const_name.replace(".", "_").upper()


def _build_spec(
    app_name: str,
    app_schema: str,
    pg_database: str,
    resource_tier: ResourceTier,
    constants: list[PadConstant],
    use_caller_rights: bool,
    license_id: str | None = None,
    role_mapping: dict[str, str] | None = None,
    pad_relative_path: str | None = None,
) -> str:
    res = RESOURCE_TIERS[resource_tier]
    pg_host_port = _pg_host()
    image_path = MENDIX_BASE_IMAGE
    # Falls back to the placeholder name at first registration, before any PAD has
    # been staged/resolved. Every later rebuild must pass the actual resolved path -
    # the container's entrypoint has no fallback logic of its own, so PAD_STAGE_PATH
    # must exactly match whatever filename was really staged (see _resolve_staged_pad).
    pad_path = f"{DEPLOY_STAGE_MOUNT}/{pad_relative_path or f'apps/{app_name}/current.zip'}"

    secret_entries = [
        {
            "snowflakeSecret": _secret_fqn(app_schema, "PG_PASS"),
            "directoryPath": "/secrets/pg_pass",
        },
        {
            "snowflakeSecret": _secret_fqn(app_schema, "ADMIN_PASS"),
            "directoryPath": "/secrets/admin_pass",
        },
    ]
    for c in constants:
        secret_entries.append({
            "snowflakeSecret": _secret_fqn(app_schema, c.secret_name),
            "directoryPath": f"/secrets/{c.secret_name.lower()}",
        })

    env = {
        "PAD_STAGE_PATH": pad_path,
        "RUNTIME_PARAMS_DATABASETYPE": "POSTGRESQL",
        "RUNTIME_PARAMS_DATABASEHOST": pg_host_port,
        "RUNTIME_PARAMS_DATABASENAME": pg_database,
        "RUNTIME_PARAMS_DATABASEUSERNAME": "application",
        "RUNTIME_PARAMS_DATABASEUSESSL": "true",
        "RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE": "com.mendix.storage.localfilesystem",
        "RUNTIME_PARAMS_UPLOADEDFILESPATH": "/mnt/filestorage",
    }
    if license_id:
        # The License ID is an identifier, not a credential - it goes in as a plain,
        # operator-visible env var. The License Key is a credential and never appears
        # here; it reaches the container only via the MX_LICENSE_KEY secret mount below.
        env["RUNTIME_LICENSE_ID"] = license_id
        secret_entries.append({
            "snowflakeSecret": _secret_fqn(app_schema, "MX_LICENSE_KEY"),
            "directoryPath": "/secrets/mx_license_key",
        })
    if role_mapping:
        # Not a secret: operator-visible mapping of Snowflake account roles to Mendix
        # userroles, consumed by the SnowflakeSSO module at login. Compact and sorted
        # so the spec is deterministic.
        env["MX_ROLE_MAPPING"] = json.dumps(role_mapping, separators=(",", ":"), sort_keys=True)

    spec: dict = {
        "spec": {
            "containers": [{
                "name": "mendix-app",
                "image": image_path,
                "env": env,
                "secrets": secret_entries,
                "readinessProbe": {"port": 8080, "path": "/"},
                "resources": {
                    "requests": {"memory": res["mem_request"], "cpu": res["cpu_request"]},
                    "limits":   {"memory": res["mem_limit"],   "cpu": res["cpu_limit"]},
                },
                "volumeMounts": [
                    {"name": "filestorage",   "mountPath": "/mnt/filestorage"},
                    {"name": "deploy-stage",  "mountPath": DEPLOY_STAGE_MOUNT},
                ],
            }],
            "volumes": [
                {
                    "name": "filestorage",
                    "source": "stage",
                    "stageConfig": {"name": f"@{_filestorage_stage(app_schema)}"},
                    # mendix-base runs as the non-root mendixuser (uid/gid 999, set in its
                    # Dockerfile); without this the stage mount is root-owned and
                    # RUNTIME_PARAMS_UPLOADEDFILESPATH is not writable by the container.
                    "uid": 999,
                    "gid": 999,
                },
                {
                    "name": "deploy-stage",
                    "source": "stage",
                    "stageConfig": {"name": DEPLOY_STAGE},
                },
            ],
            "endpoints": [{"name": "mendix-web", "port": 8080, "public": True}],
        }
    }

    if use_caller_rights:
        spec["capabilities"] = {"securityContext": {"executeAsCaller": True}}

    return yaml.dump(spec, default_flow_style=False, sort_keys=False)


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


def _record_for_mutation(name: str, roles: set[str]) -> AppRecord:
    """Load an app the caller may mutate: 404 if missing, 403 if not authorized."""
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    if not auth.authorize(record.owner_role, roles):
        raise HTTPException(status_code=403, detail=f"Not authorized for app '{name}'")
    return record


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
def list_apps(roles: set[str] = Depends(caller_roles)):
    apps = registry.list_apps()
    statuses = sf.show_all_service_statuses()
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


@app.post("/apps", status_code=status.HTTP_201_CREATED)
def create_app(req: CreateAppRequest, roles: set[str] = Depends(caller_roles)):
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

    # The app's own schema holds everything it owns (secrets, filestorage
    # stage); delete_app removes it with one DROP SCHEMA ... CASCADE.
    sf.create_schema(_schema_fqn(app_schema))
    sf.create_stage(_filestorage_stage(app_schema))

    # Create PG password and admin password secrets.
    # Read the bootstrap PG password from the controller's bound pg_secret (/secrets/pg).
    # req.pg_database is the target database name, not the password.
    _, pg_password = _load_pg_credentials()
    if not pg_password:
        raise HTTPException(status_code=409, detail="Controller PG credentials not mounted at /secrets/pg")
    sf.create_or_replace_secret(_secret_fqn(app_schema, "PG_PASS"), pg_password)
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


@app.get("/system/compute-pool")
def get_compute_pool(roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    pool = sf.get_compute_pool(COMPUTE_POOL)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Compute pool '{COMPUTE_POOL}' not found")
    return pool


@app.patch("/system/compute-pool")
def update_compute_pool(req: UpdateComputePoolRequest, roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    if req.min_nodes is None and req.max_nodes is None and req.auto_suspend_secs is None:
        raise HTTPException(status_code=400, detail="At least one field must be provided")
    sf.alter_compute_pool(
        COMPUTE_POOL,
        min_nodes=req.min_nodes,
        max_nodes=req.max_nodes,
        auto_suspend_secs=req.auto_suspend_secs,
    )
    pool = sf.get_compute_pool(COMPUTE_POOL)
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


def _run_deploy(name: str, pad_path: str, record: AppRecord,
                pad_constants: list[PadConstant], new_constants: dict,
                user_roles: list[str]) -> None:
    """Background deploy task. registry status must be set to DEPLOYING before calling."""
    try:
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
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           _constants_from_dict(new_constants), record.use_caller_rights, record.license_id,
                           record.role_mapping, pad_relative_path)
        sf.alter_service_spec(record.service_name, spec)

        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return

        _stamp_deploy_success(name, record.service_name, {
            "constants": new_constants,
            "pad_stage_path": pad_relative_path,
            "user_roles": user_roles,
        })
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Deploy failed for %s", name)


def _resolve_staged_pad(name: str) -> str | None:
    """Find the PAD a consumer staged under apps/<name>/.

    Prefer current.zip (the canonical name). Otherwise accept the newest .zip in
    the directory, so the documented `snow stage copy <yourpad>.zip @.../apps/<name>/`
    one-liner works without forcing the consumer to rename the file first.
    """
    app_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
    canonical = os.path.join(app_dir, "current.zip")
    if os.path.isfile(canonical):
        return canonical
    if not os.path.isdir(app_dir):
        return None
    zips = [
        os.path.join(app_dir, f)
        for f in os.listdir(app_dir)
        if f.lower().endswith(".zip") and os.path.isfile(os.path.join(app_dir, f))
    ]
    if not zips:
        return None
    return max(zips, key=os.path.getmtime)


@app.post("/apps/{name}/trigger-deploy", status_code=202)
def trigger_deploy(name: str, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    """Trigger deploy from a PAD already staged under apps/{name}/ (current.zip or newest .zip)."""
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
    try:
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants, record.use_caller_rights, record.license_id, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Constants update failed for %s", name)


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

    for const_name, value in changed.items():
        sf.create_or_replace_secret(_secret_fqn(record.app_schema, _const_secret_name(const_name)), value)

    merged = {**stored, **req.constants}
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_constants, name, record.service_name, merged, record, _constants_from_dict(merged))
    return {"status": "DEPLOYING"}


def _run_update_spec(name: str, record: AppRecord, new_tier: ResourceTier,
                     new_caller: bool) -> None:
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, new_tier, constants_list, new_caller,
                           record.license_id, record.role_mapping, record.pad_stage_path)
        sf.alter_service_spec(record.service_name, spec)
        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {
            "resource_tier": str(new_tier.value) if hasattr(new_tier, "value") else str(new_tier),
            "use_caller_rights": new_caller,
            "last_deploy_status": "READY",
        })
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Spec update failed for %s", name)


@app.put("/apps/{name}/spec", status_code=202)
def update_spec(name: str, req: UpdateSpecRequest, background_tasks: BackgroundTasks,
                roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
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
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants_list, record.use_caller_rights, license_id, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("License update failed for %s", name)


@app.put("/apps/{name}/license", status_code=202)
def update_license(name: str, req: UpdateLicenseRequest, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
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
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database, ResourceTier(record.resource_tier),
                           constants_list, record.use_caller_rights, None, record.role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
        sf.drop_secret(_secret_fqn(record.app_schema, "MX_LICENSE_KEY"))
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("License removal failed for %s", name)


@app.delete("/apps/{name}/license", status_code=202)
def delete_license(name: str, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_delete_license, name, record.service_name, record)
    return {"status": "DEPLOYING"}


def _run_update_role_mapping(name: str, service_name: str, record: AppRecord,
                             role_mapping: dict[str, str]) -> None:
    """Persist up front (same ordering as license_id), rebuild the spec with
    MX_ROLE_MAPPING, restart so the SSO handler sees it at next login."""
    registry.update_app(name, {"role_mapping": role_mapping})
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database,
                           ResourceTier(record.resource_tier), constants_list,
                           record.use_caller_rights, record.license_id, role_mapping,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Role mapping update failed for %s", name)


@app.put("/apps/{name}/role-mapping", status_code=202)
def update_role_mapping(name: str, req: UpdateRoleMappingRequest,
                        background_tasks: BackgroundTasks,
                        roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
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
        warnings.append("No userroles detected yet (no PAD deployed); mapping values are unvalidated.")
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
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.app_schema, record.pg_database,
                           ResourceTier(record.resource_tier), constants_list,
                           record.use_caller_rights, record.license_id, None,
                           record.pad_stage_path)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Role mapping removal failed for %s", name)


@app.delete("/apps/{name}/role-mapping", status_code=202)
def delete_role_mapping(name: str, background_tasks: BackgroundTasks,
                        roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
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
    try:
        sf.suspend_service(service_name)
        if not _poll_status(service_name, "SUSPENDED", timeout_secs=120):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {"last_deploy_status": "SUSPENDED"})
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Suspend failed for %s", name)


def _run_resume(name: str, service_name: str) -> None:
    try:
        sf.resume_service(service_name)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {"last_deploy_status": "READY"})
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Resume failed for %s", name)


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
    record = _record_for_mutation(name, roles)

    try:
        sf.suspend_service(record.service_name)
        _poll_status(record.service_name, "SUSPENDED", timeout_secs=60)
    except Exception:
        pass

    # Every drop below is IF EXISTS, so a partially failed delete is safe to
    # retry. Attempt each step even when an earlier one fails, then keep the
    # registry row alive if anything failed: the row is the operator's only
    # handle for retrying, and deleting it while a service or schema survives
    # would leak that object with nothing left pointing at it.
    cleanup_steps = [
        # Dropping the service auto-drops its service roles (revoking the
        # endpoint grant from app_admin); the per-app application role
        # persists, so drop it separately.
        ("drop service", lambda: sf.drop_service(record.service_name)),
        ("drop application role", lambda: sf.drop_app_access_role(name)),
        # The app's schema contains everything it owns: credential secrets
        # (PG password, admin password, constants) and the filestorage stage.
        # CASCADE removes them all, including the user's uploaded files; the
        # admin UI warns about this before the delete.
        ("drop schema", lambda: sf.drop_schema_cascade(_schema_fqn(record.app_schema))),
    ]
    failures = []
    for step, run in cleanup_steps:
        try:
            run()
        except Exception as exc:
            logger.warning("delete %s: %s failed: %s", name, step, exc)
            failures.append(step)
    if failures:
        raise HTTPException(
            status_code=502,
            detail=f"Cleanup failed ({', '.join(failures)}); retry the delete",
        )

    registry.delete_app(name)
