from __future__ import annotations

import json
import os
from typing import Any, Optional

from . import snowflake_client as sf
from .models import AppRecord

_TABLE = f"{os.environ['DB_SCHEMA']}.MENDIX_APPS"


def _row_to_record(row: dict) -> AppRecord:
    constants = row.get("CONSTANTS") or {}
    if isinstance(constants, str):
        constants = json.loads(constants)
    return AppRecord(
        name=row["NAME"],
        service_name=row["SERVICE_NAME"],
        pg_database=row["PG_DATABASE"],
        resource_tier=row.get("RESOURCE_TIER") or "medium",
        use_caller_rights=bool(row.get("USE_CALLER_RIGHTS")),
        constants=constants,
        pad_stage_path=row.get("PAD_STAGE_PATH"),
        endpoint_url=row.get("ENDPOINT_URL"),
        last_deploy_status=row.get("LAST_DEPLOY_STATUS"),
        created_at=str(row["CREATED_AT"]) if row.get("CREATED_AT") else None,
        last_deployed_at=str(row["LAST_DEPLOYED_AT"]) if row.get("LAST_DEPLOYED_AT") else None,
    )


def create_app(record: AppRecord) -> None:
    constants_json = json.dumps(record.constants)
    sf.execute_sql(
        f"""
        INSERT INTO {_TABLE}
            (name, service_name, pg_database, resource_tier, use_caller_rights,
             constants, pad_stage_path, endpoint_url, last_deploy_status)
        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s
        """,
        (
            record.name,
            record.service_name,
            record.pg_database,
            record.resource_tier,
            record.use_caller_rights,
            constants_json,
            record.pad_stage_path,
            record.endpoint_url,
            record.last_deploy_status,
        ),
    )


def get_app(name: str) -> Optional[AppRecord]:
    rows = sf.execute_sql(f"SELECT * FROM {_TABLE} WHERE name = %s", (name,))
    if not rows:
        return None
    return _row_to_record(rows[0])


def list_apps() -> list[AppRecord]:
    rows = sf.execute_sql(f"SELECT * FROM {_TABLE} ORDER BY created_at")
    return [_row_to_record(r) for r in rows]


_ALLOWED_UPDATE_COLUMNS = frozenset({
    "constants", "pad_stage_path", "endpoint_url",
    "last_deploy_status", "last_deployed_at",
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
            values.append(json.dumps(val))
        else:
            set_clauses.append(f"{key} = %s")
            values.append(val)
    values.append(name)
    sf.execute_sql(
        f"UPDATE {_TABLE} SET {', '.join(set_clauses)} WHERE name = %s",
        tuple(values),
    )


def delete_app(name: str) -> None:
    sf.execute_sql(f"DELETE FROM {_TABLE} WHERE name = %s", (name,))
