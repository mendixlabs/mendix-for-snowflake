from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

import yaml
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from . import activity, registry, snowflake_client as sf
from .models import (
    AppRecord,
    AppStatusResponse,
    CreateAppRequest,
    DeployResponse,
    MissingConstantsError,
    RESOURCE_TIERS,
    ResourceTier,
    UpdateConstantsRequest,
    UpdateSpecRequest,
)
from .pad_parser import PadConstant, parse_from_zip


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
    if request.method in ("POST", "PUT", "DELETE"):
        operator = request.headers.get("X-Operator", "<anonymous>")
        action, app_name = activity.derive_action(request.method, request.url.path)
        logger.info("operator=%s %s %s", operator, request.method, request.url.path)
        try:
            activity.insert(
                operator=operator,
                action=action,
                app_name=app_name,
                detail={"path": request.url.path, "method": request.method},
            )
        except Exception:
            logger.exception("Failed to record activity row")
    return await call_next(request)

DB_SCHEMA = os.environ["DB_SCHEMA"]
COMPUTE_POOL = os.environ["COMPUTE_POOL"]
IMAGE_REPO = os.environ["IMAGE_REPO"]
PG_EAI = os.environ["PG_EAI"]
QUERY_WAREHOUSE = os.environ["QUERY_WAREHOUSE"]
DEPLOY_STAGE = f"@{DB_SCHEMA}.MENDIX_DEPLOY_STAGE"
DEPLOY_STAGE_MOUNT = "/mnt/deploy-stage"

# Derived from controller secrets at startup
_PG_HOST: str | None = None


def _pg_host() -> str:
    global _PG_HOST
    if _PG_HOST is None:
        secret_file = "/secrets/pg_host/secret_string"
        if os.path.exists(secret_file):
            with open(secret_file) as f:
                _PG_HOST = f.read().strip()
        else:
            _PG_HOST = os.environ.get("PG_HOST", "localhost:5432")
    return _PG_HOST


def _service_name(app_name: str) -> str:
    return f"{app_name.upper()}_SERVICE"


def _filestorage_stage(app_name: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_FILESTORAGE_STAGE"


def _secret_fqn(app_name: str, suffix: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_{suffix.upper()}"


def _const_secret_fqn(app_name: str, secret_name: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_{secret_name}"


def _build_spec(
    app_name: str,
    pg_database: str,
    resource_tier: ResourceTier,
    constants: list[PadConstant],
    use_caller_rights: bool,
) -> str:
    res = RESOURCE_TIERS[resource_tier]
    pg_host_port = _pg_host()
    image_path = f"/{IMAGE_REPO}:latest"
    pad_path = f"{DEPLOY_STAGE_MOUNT}/apps/{app_name}/current.zip"

    secret_entries = [
        {
            "snowflakeSecret": _secret_fqn(app_name, "PG_PASS"),
            "directoryPath": "/secrets/pg_pass",
        },
        {
            "snowflakeSecret": _secret_fqn(app_name, "ADMIN_PASS"),
            "directoryPath": "/secrets/admin_pass",
        },
    ]
    for c in constants:
        secret_entries.append({
            "snowflakeSecret": _const_secret_fqn(app_name, c.secret_name),
            "directoryPath": f"/secrets/{c.secret_name.lower()}",
        })

    spec: dict = {
        "spec": {
            "containers": [{
                "name": "mendix-app",
                "image": image_path,
                "env": {
                    "PAD_STAGE_PATH": pad_path,
                    "RUNTIME_PARAMS_DATABASETYPE": "POSTGRESQL",
                    "RUNTIME_PARAMS_DATABASEHOST": pg_host_port,
                    "RUNTIME_PARAMS_DATABASENAME": pg_database,
                    "RUNTIME_PARAMS_DATABASEUSERNAME": "application",
                    "RUNTIME_PARAMS_DATABASEUSESSL": "true",
                    "RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE": "com.mendix.storage.localfilesystem",
                    "RUNTIME_PARAMS_UPLOADEDFILESPATH": "/mnt/filestorage",
                },
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
                    "stageConfig": {"name": f"@{_filestorage_stage(app_name)}"},
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
        time.sleep(5)
    return False


def _sync_constant_secrets(app_name: str, constants: list[PadConstant], values: dict[str, str]) -> None:
    for c in constants:
        val = values.get(c.name, c.default)
        sf.create_or_replace_secret(_const_secret_fqn(app_name, c.secret_name), val)


def _constants_from_dict(d: dict[str, str]) -> list[PadConstant]:
    return [
        PadConstant(name=k, env_var="", default=v,
                    secret_name="MX_CONST_" + k.replace(".", "_").upper())
        for k, v in d.items()
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/apps")
def list_apps():
    apps = registry.list_apps()
    result = []
    for a in apps:
        svc_status = sf.show_service_status(a.service_name)
        result.append({**a.model_dump(), "service_status": svc_status})
    return result


@app.post("/apps", status_code=status.HTTP_201_CREATED)
def create_app(req: CreateAppRequest):
    if registry.get_app(req.name):
        raise HTTPException(status_code=409, detail=f"App '{req.name}' already exists")

    service_name = _service_name(req.name)
    filestorage_fqn = _filestorage_stage(req.name)

    # Create filestorage stage
    sf.create_stage(filestorage_fqn)

    # Create PG password and admin password secrets.
    # Read the actual PG password from the controller's own mounted secret (CTRL_PG_PASS).
    # req.pg_database is the target database name, not the password.
    _pg_pass_file = "/secrets/pg_pass/secret_string"
    if os.path.exists(_pg_pass_file):
        with open(_pg_pass_file) as f:
            pg_password = f.read().strip()
    else:
        pg_password = ""
    if not pg_password:
        raise HTTPException(status_code=500, detail="Controller PG password secret not mounted at /secrets/pg_pass")
    sf.create_or_replace_secret(_secret_fqn(req.name, "PG_PASS"), pg_password)
    sf.create_or_replace_secret(_secret_fqn(req.name, "ADMIN_PASS"), req.admin_password)

    # Create constant secrets from provided values (using defaults for any not supplied)
    constants: list[PadConstant] = []  # no PAD yet at create time
    for const_name, value in req.constants.items():
        secret_name = "MX_CONST_" + const_name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(req.name, secret_name), value)
        constants.append(PadConstant(name=const_name, env_var="", default=value, secret_name=secret_name))

    spec = _build_spec(req.name, req.pg_database, req.resource_tier, constants, req.use_caller_rights)

    sf.create_service(service_name, spec, COMPUTE_POOL, PG_EAI, QUERY_WAREHOUSE)

    if req.use_caller_rights:
        sf.set_caller_token_validity(service_name, 1800)

    # Endpoint URL is not available until the service starts; it's captured by _run_deploy.
    record = AppRecord(
        name=req.name,
        service_name=service_name,
        pg_database=req.pg_database,
        resource_tier=req.resource_tier,
        use_caller_rights=req.use_caller_rights,
        constants=req.constants,
        pad_stage_path=None,
        endpoint_url=None,
        last_deploy_status="STARTING",
        created_at=None,
        last_deployed_at=None,
    )
    registry.create_app(record)

    return {"service_name": service_name, "status": "STARTING"}


@app.get("/apps/{name}")
def get_app(name: str):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    svc_status = sf.show_service_status(record.service_name)
    return AppStatusResponse(app=record, service_status=svc_status)


@app.get("/apps/{name}/logs")
def get_logs(name: str, lines: int = 200):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    logs = sf.get_service_logs(record.service_name, lines=lines)
    return {"logs": logs}


def _prepare_deploy(
    name: str, pad_path: str
) -> tuple[AppRecord, list[PadConstant], dict]:
    """Parse and validate a PAD. Returns (record, pad_constants, new_constants). Raises HTTPException on error."""
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

    return record, pad_constants, new_constants


def _run_deploy(name: str, pad_path: str, record: AppRecord,
                pad_constants: list[PadConstant], new_constants: dict) -> None:
    """Background deploy task. registry status must be set to DEPLOYING before calling."""
    try:
        stored = record.constants or {}
        constants_changed = any(
            new_constants.get(c.name) != stored.get(c.name)
            for c in pad_constants
        )

        if constants_changed:
            _sync_constant_secrets(name, pad_constants, new_constants)
            spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                               _constants_from_dict(new_constants), record.use_caller_rights)
            sf.alter_service_spec(record.service_name, spec)
        else:
            sf.suspend_service(record.service_name)
            if not _poll_status(record.service_name, "SUSPENDED", timeout_secs=120):
                raise RuntimeError(f"Service {record.service_name} did not suspend within 120s")
            sf.resume_service(record.service_name)

        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return

        endpoint_url = sf.get_service_endpoint(record.service_name)
        registry.update_app(name, {
            "constants": new_constants,
            "pad_stage_path": f"apps/{name}/current.zip",
            "endpoint_url": endpoint_url,
            "last_deploy_status": "READY",
            "last_deployed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Deploy failed for %s", name)


@app.post("/apps/{name}/deploy", status_code=202)
def deploy_pad(name: str, pad_file: UploadFile = File(...),
               background_tasks: BackgroundTasks = None):
    """Upload a PAD zip. For large PADs (>50 MB) use snow stage copy + /trigger-deploy instead."""
    dest_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "current.zip")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(pad_file.file, tmp)
        tmp_path = tmp.name
    shutil.copy2(tmp_path, dest_path)
    os.unlink(tmp_path)

    record, pad_constants, new_constants = _prepare_deploy(name, dest_path)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_deploy, name, dest_path, record, pad_constants, new_constants)
    return {"status": "DEPLOYING"}


@app.post("/apps/{name}/trigger-deploy", status_code=202)
def trigger_deploy(name: str, background_tasks: BackgroundTasks):
    """Trigger deploy from a PAD already at stage path apps/{name}/current.zip."""
    pad_path = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name, "current.zip")
    if not os.path.exists(pad_path):
        raise HTTPException(
            status_code=400,
            detail=f"PAD not found at stage path apps/{name}/current.zip — upload it first.",
        )
    record, pad_constants, new_constants = _prepare_deploy(name, pad_path)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_deploy, name, pad_path, record, pad_constants, new_constants)
    return {"status": "DEPLOYING"}


def _run_update_constants(name: str, service_name: str, merged: dict,
                          record: AppRecord, constants: list[PadConstant]) -> None:
    """Background task for constants update."""
    try:
        spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                           constants, record.use_caller_rights)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {"constants": merged, "last_deploy_status": "READY"})
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Constants update failed for %s", name)


@app.put("/apps/{name}/constants", status_code=202)
def update_constants(name: str, req: UpdateConstantsRequest, background_tasks: BackgroundTasks):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    for const_name, value in req.constants.items():
        secret_name = "MX_CONST_" + const_name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(name, secret_name), value)

    merged = {**(record.constants or {}), **req.constants}
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_constants, name, record.service_name, merged, record, _constants_from_dict(merged))
    return {"status": "DEPLOYING"}


def _run_update_spec(name: str, record: AppRecord, new_tier: ResourceTier,
                     new_caller: bool, caller_flipping_on: bool) -> None:
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.pg_database, new_tier, constants_list, new_caller)
        sf.alter_service_spec(record.service_name, spec)
        if caller_flipping_on:
            sf.set_caller_token_validity(record.service_name, 1800)
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
def update_spec(name: str, req: UpdateSpecRequest, background_tasks: BackgroundTasks):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    if req.resource_tier is None and req.use_caller_rights is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of resource_tier or use_caller_rights must be provided",
        )

    new_tier = req.resource_tier if req.resource_tier is not None else ResourceTier(record.resource_tier)
    new_caller = req.use_caller_rights if req.use_caller_rights is not None else bool(record.use_caller_rights)
    caller_flipping_on = (not record.use_caller_rights) and new_caller

    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_spec, name, record, new_tier, new_caller, caller_flipping_on)
    return {"status": "DEPLOYING"}


@app.get("/activity")
def list_activity(app: Optional[str] = None, operator: Optional[str] = None, limit: int = 100):
    return activity.query(app=app, operator=operator, limit=limit)


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
def suspend_app(name: str, background_tasks: BackgroundTasks):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    registry.update_app(name, {"last_deploy_status": "SUSPENDING"})
    background_tasks.add_task(_run_suspend, name, record.service_name)
    return {"status": "SUSPENDING"}


@app.post("/apps/{name}/resume", status_code=202)
def resume_app(name: str, background_tasks: BackgroundTasks):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    registry.update_app(name, {"last_deploy_status": "RESUMING"})
    background_tasks.add_task(_run_resume, name, record.service_name)
    return {"status": "RESUMING"}


@app.delete("/apps/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(name: str):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    try:
        sf.suspend_service(record.service_name)
        _poll_status(record.service_name, "SUSPENDED", timeout_secs=60)
    except Exception:
        pass

    sf.drop_service(record.service_name)
    registry.delete_app(name)
