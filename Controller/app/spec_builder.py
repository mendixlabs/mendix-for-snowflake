"""Builds the SPCS service spec YAML for a Mendix app container.

_build_spec needs several pieces of state that live in main.py: the cached PG
host lookup (_pg_host, with its test-visible _PG_HOST/_PG_PASSWORD cache and
monkeypatched _load_pg_credentials), the per-app naming helpers (_secret_fqn,
_filestorage_stage, _pg_username), and a few env-derived constants
(MENDIX_BASE_IMAGE, DEPLOY_STAGE_MOUNT, DEPLOY_STAGE). Those all stay defined
in main.py (create_app, delete_app and friends use them too, and the PG host
cache is reset directly by the test suite via main._PG_HOST), so this module
reaches back into main for them via a deferred, in-function import rather than
a module-level one - a module-level "from .main import ..." here would deadlock
on main.py's own top-level "from .spec_builder import _build_spec" (circular
import while main is only half-initialized). By the time _build_spec is
actually called, main has finished importing, so the deferred import is safe,
and because it's an attribute access (main.X) rather than a bound name, every
existing monkeypatch of those main.py attributes still reaches this function.
"""
from __future__ import annotations

import json

import yaml

from .models import RESOURCE_TIERS, ResourceTier
from .pad_parser import PadConstant


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
    from . import main  # deferred: see module docstring

    res = RESOURCE_TIERS[resource_tier]
    pg_host_port = main._pg_host()
    image_path = main.MENDIX_BASE_IMAGE
    # Falls back to the placeholder name at first registration, before any PAD has
    # been staged/resolved. Every later rebuild must pass the actual resolved path -
    # the container's entrypoint has no fallback logic of its own, so PAD_STAGE_PATH
    # must exactly match whatever filename was really staged (see _resolve_staged_pad).
    pad_path = f"{main.DEPLOY_STAGE_MOUNT}/{pad_relative_path or f'apps/{app_name}/current.zip'}"

    secret_entries = [
        {
            "snowflakeSecret": main._secret_fqn(app_schema, "PG_PASS"),
            "directoryPath": "/secrets/pg_pass",
        },
        {
            "snowflakeSecret": main._secret_fqn(app_schema, "ADMIN_PASS"),
            "directoryPath": "/secrets/admin_pass",
        },
    ]
    for c in constants:
        secret_entries.append({
            "snowflakeSecret": main._secret_fqn(app_schema, c.secret_name),
            "directoryPath": f"/secrets/{c.secret_name.lower()}",
        })

    env = {
        "PAD_STAGE_PATH": pad_path,
        "RUNTIME_PARAMS_DATABASETYPE": "POSTGRESQL",
        "RUNTIME_PARAMS_DATABASEHOST": pg_host_port,
        "RUNTIME_PARAMS_DATABASENAME": pg_database,
        "RUNTIME_PARAMS_DATABASEUSERNAME": main._pg_username(app_name),
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
            "snowflakeSecret": main._secret_fqn(app_schema, "MX_LICENSE_KEY"),
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
                    {"name": "deploy-stage",  "mountPath": main.DEPLOY_STAGE_MOUNT},
                ],
            }],
            "volumes": [
                {
                    "name": "filestorage",
                    "source": "stage",
                    "stageConfig": {"name": f"@{main._filestorage_stage(app_schema)}"},
                    # mendix-base runs as the non-root mendixuser (uid/gid 999, set in its
                    # Dockerfile); without this the stage mount is root-owned and
                    # RUNTIME_PARAMS_UPLOADEDFILESPATH is not writable by the container.
                    "uid": 999,
                    "gid": 999,
                },
                {
                    "name": "deploy-stage",
                    "source": "stage",
                    "stageConfig": {"name": main.DEPLOY_STAGE},
                },
            ],
            "endpoints": [{"name": "mendix-web", "port": 8080, "public": True}],
        }
    }

    if use_caller_rights:
        spec["capabilities"] = {"securityContext": {"executeAsCaller": True}}

    return yaml.dump(spec, default_flow_style=False, sort_keys=False)
