from __future__ import annotations

import json
from typing import Any, Optional

from . import snowflake_client as sf
from .models import AppRecord, HIDDEN_VALUE

_TABLE = f"{sf.require_env('DB_SCHEMA')}.MENDIX_APPS"


def _mask_constants(constants: dict) -> dict:
    """Keep the keys, drop the values. Constant values live only in the per-app
    Snowflake secrets (MX_CONST_*); the registry row must never hold a plaintext
    copy readable via SELECT on MENDIX_APPS."""
    return {name: HIDDEN_VALUE for name in constants}


def _row_to_record(row: dict) -> AppRecord:
    constants = row.get("CONSTANTS") or {}
    if isinstance(constants, str):
        constants = json.loads(constants)
    user_roles = row.get("USER_ROLES") or []
    if isinstance(user_roles, str):
        user_roles = json.loads(user_roles)
    role_mapping = row.get("ROLE_MAPPING") or {}
    if isinstance(role_mapping, str):
        role_mapping = json.loads(role_mapping)
    return AppRecord(
        name=row["NAME"],
        service_name=row["SERVICE_NAME"],
        app_schema=row["APP_SCHEMA"],
        pg_database=row["PG_DATABASE"],
        resource_tier=row.get("RESOURCE_TIER") or "medium",
        use_caller_rights=bool(row.get("USE_CALLER_RIGHTS")),
        constants=constants,
        license_id=row.get("LICENSE_ID"),
        user_roles=user_roles,
        role_mapping=role_mapping,
        pad_stage_path=row.get("PAD_STAGE_PATH"),
        endpoint_url=row.get("ENDPOINT_URL"),
        last_deploy_status=row.get("LAST_DEPLOY_STATUS"),
        created_at=str(row["CREATED_AT"]) if row.get("CREATED_AT") else None,
        last_deployed_at=str(row["LAST_DEPLOYED_AT"]) if row.get("LAST_DEPLOYED_AT") else None,
        owner_role=row.get("OWNER_ROLE") or "MENDIX_ADMIN_OPERATOR_ROLE",
    )


def create_app(record: AppRecord) -> None:
    constants_json = json.dumps(_mask_constants(record.constants))
    sf.execute_sql(
        f"""
        INSERT INTO {_TABLE}
            (name, service_name, app_schema, pg_database, resource_tier, use_caller_rights,
             constants, pad_stage_path, endpoint_url, last_deploy_status, owner_role, license_id)
        SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s, %s, %s
        """,
        (
            record.name,
            record.service_name,
            record.app_schema,
            record.pg_database,
            record.resource_tier,
            record.use_caller_rights,
            constants_json,
            record.pad_stage_path,
            record.endpoint_url,
            record.last_deploy_status,
            record.owner_role,
            record.license_id,
        ),
    )


def get_app(name: str) -> Optional[AppRecord]:
    # Case-insensitive: the name feeds case-insensitive Snowflake identifiers
    # (MXAPP_<NAME> schema, <NAME>_SERVICE), so "myapp" and "MyApp" are the
    # same app. Registration relies on this for its duplicate check.
    rows = sf.execute_sql(f"SELECT * FROM {_TABLE} WHERE UPPER(name) = UPPER(%s)", (name,))
    if not rows:
        return None
    return _row_to_record(rows[0])


def list_apps() -> list[AppRecord]:
    rows = sf.execute_sql(f"SELECT * FROM {_TABLE} ORDER BY created_at")
    return [_row_to_record(r) for r in rows]


_ALLOWED_UPDATE_COLUMNS = frozenset({
    "constants", "pad_stage_path", "endpoint_url",
    "last_deploy_status", "last_deployed_at",
    "resource_tier", "use_caller_rights", "owner_role", "license_id",
    "user_roles", "role_mapping",
})


def update_app(name: str, fields: dict[str, Any]) -> None:
    if not fields:
        return
    invalid = set(fields.keys()) - _ALLOWED_UPDATE_COLUMNS
    if invalid:
        raise ValueError(f"Invalid column(s) in update_app: {invalid}")
    set_clauses = []
    values = []
    for key, val in fields.items():
        if key == "constants":
            set_clauses.append(f"{key} = PARSE_JSON(%s)")
            values.append(json.dumps(_mask_constants(val)))
        elif key in ("user_roles", "role_mapping"):
            if val is None:
                set_clauses.append(f"{key} = %s")
                values.append(None)          # DELETE clears to SQL NULL
            else:
                set_clauses.append(f"{key} = PARSE_JSON(%s)")
                values.append(json.dumps(val))
        else:
            set_clauses.append(f"{key} = %s")
            values.append(val)
    values.append(name)
    sf.execute_sql(
        f"UPDATE {_TABLE} SET {', '.join(set_clauses)} WHERE UPPER(name) = UPPER(%s)",
        tuple(values),
    )


def delete_app(name: str) -> None:
    sf.execute_sql(f"DELETE FROM {_TABLE} WHERE UPPER(name) = UPPER(%s)", (name,))
