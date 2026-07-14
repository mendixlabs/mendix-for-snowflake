"""Per-deploy audit trail: snapshots the inputs of each app mutation that
rebuilds and applies a service spec, so a rollback can replay them.

Distinct from activity.py (operator/action/result log of every mutating HTTP
call): this table records what was actually applied to a specific app's
service spec - one row per deploy/constants/spec/license/role_mapping/
platform_update/rollback attempt, success or failure - keyed by app_name so a
rollback can find "the last configuration that actually came up READY".

last_success() does not filter by which operation produced the READY row.
Every operation that reaches _run_lifecycle_task's on_success (deploy,
constants, spec, license, role_mapping, platform_update, rollback) rebuilds
the *entire* spec via _build_spec (main.py's _run_deploy/_run_update_constants/
_run_update_spec/_run_update_license/_run_delete_license/_run_update_role_mapping/
_run_delete_role_mapping/_run_rollback all call it), not just the field the
operation nominally changed. A READY row from a constants-only update is
therefore just as valid a rollback target as one from a deploy - the row
already captures the pad_stage_path/resource_tier/use_caller_rights/
license_id/role_mapping that were live alongside that constants change.

Constant *values* are never snapshotted (they are secrets, living only in
per-app Snowflake secrets); constant_names records which names were mounted,
not their contents, so a rollback restores which secrets mount, never their
point-in-time values.
"""
from __future__ import annotations

import json
import logging
import textwrap
from typing import Optional

from . import snowflake_client as sf
from .models import AppRecord

logger = logging.getLogger(__name__)

_TABLE = f"{sf.require_env('DB_SCHEMA')}.MENDIX_DEPLOY_HISTORY"

# Rows kept per app; older rows beyond this are pruned on every write.
_KEEP_PER_APP = 20


def init_table() -> None:
    """Idempotent: ensure MENDIX_DEPLOY_HISTORY exists.

    In the Native App the table is pre-created in setup_script.sql and owned by the
    app role (the controller's session), so ownership covers SELECT/INSERT and no
    per-table grant to a controller account role is needed. CREATE TABLE IF NOT
    EXISTS stays for local-dev runs outside the app. Column set matches
    setup_script.sql's MENDIX_DEPLOY_HISTORY exactly.
    """
    sf.execute_sql(textwrap.dedent(f"""\
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id                NUMBER    AUTOINCREMENT PRIMARY KEY,
            app_name          VARCHAR   NOT NULL,
            ts                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operation         VARCHAR,
            pad_stage_path    VARCHAR,
            resource_tier     VARCHAR,
            use_caller_rights BOOLEAN,
            constant_names    VARIANT,
            license_id        VARCHAR,
            role_mapping      VARIANT,
            external_access   VARIANT,
            status            VARCHAR,
            detail            VARCHAR
        )
    """))


def record(app_name: str, operation: str, record: AppRecord, status: str,
           detail: Optional[str] = None) -> None:
    """Insert a history row snapshotting `record`'s deployment-relevant fields,
    then prune this app's rows down to the newest _KEEP_PER_APP.

    `record` is the AppRecord reflecting the configuration this operation
    applied (or attempted to apply) - callers pass a record already updated
    with the operation's target values, not necessarily the one fetched before
    the operation started.
    """
    sf.execute_sql(
        f"""
        INSERT INTO {_TABLE}
            (app_name, operation, pad_stage_path, resource_tier, use_caller_rights,
             constant_names, license_id, role_mapping, external_access, status, detail)
        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, %s
        """,
        (
            app_name,
            operation,
            record.pad_stage_path,
            record.resource_tier,
            record.use_caller_rights,
            json.dumps(sorted((record.constants or {}).keys())),
            record.license_id,
            json.dumps(record.role_mapping or {}),
            json.dumps(record.external_access or []),
            status,
            detail,
        ),
    )
    _prune(app_name)


def _prune(app_name: str) -> None:
    """Keep only the newest _KEEP_PER_APP rows for this app."""
    # nosec B608 - _TABLE is a fixed constant and _KEEP_PER_APP an int literal;
    # app_name is parameterized (bound twice, once per subquery reference).
    sf.execute_sql(
        f"""
        DELETE FROM {_TABLE}
        WHERE UPPER(app_name) = UPPER(%s)
          AND id NOT IN (
            SELECT id FROM {_TABLE}
            WHERE UPPER(app_name) = UPPER(%s)
            ORDER BY ts DESC, id DESC
            LIMIT {_KEEP_PER_APP}
          )
        """,
        (app_name, app_name),
    )


def _row_to_dict(row: dict) -> dict:
    role_mapping = row.get("ROLE_MAPPING") or {}
    if isinstance(role_mapping, str):
        role_mapping = json.loads(role_mapping)
    external_access = row.get("EXTERNAL_ACCESS") or []
    if isinstance(external_access, str):
        external_access = json.loads(external_access)
    constant_names = row.get("CONSTANT_NAMES") or []
    if isinstance(constant_names, str):
        constant_names = json.loads(constant_names)
    return {
        "id": row.get("ID"),
        "ts": str(row["TS"]) if row.get("TS") else None,
        "operation": row.get("OPERATION"),
        "pad_stage_path": row.get("PAD_STAGE_PATH"),
        "resource_tier": row.get("RESOURCE_TIER"),
        "use_caller_rights": bool(row.get("USE_CALLER_RIGHTS")),
        "constant_names": constant_names,
        "license_id": row.get("LICENSE_ID"),
        "role_mapping": role_mapping,
        "external_access": external_access,
        "status": row.get("STATUS"),
        "detail": row.get("DETAIL"),
    }


def list_for_app(app_name: str, limit: int = 20) -> list[dict]:
    """Newest-first history rows for `app_name`."""
    rows = sf.execute_sql(
        f"SELECT * FROM {_TABLE} WHERE UPPER(app_name) = UPPER(%s) "  # nosec B608 - _TABLE fixed, app_name parameterized
        f"ORDER BY ts DESC, id DESC LIMIT {int(limit)}",
        (app_name,),
    )
    return [_row_to_dict(r) for r in rows]


def last_success(app_name: str) -> Optional[dict]:
    """The newest READY row for `app_name`, or None if it has never deployed
    successfully. See the module docstring for why every READY row (not just
    ones from a "deploy" operation) is a valid rollback target."""
    rows = sf.execute_sql(
        f"SELECT * FROM {_TABLE} WHERE UPPER(app_name) = UPPER(%s) AND status = 'READY' "  # nosec B608
        f"ORDER BY ts DESC, id DESC LIMIT 1",
        (app_name,),
    )
    return _row_to_dict(rows[0]) if rows else None


def get_entry(app_name: str, entry_id: int) -> Optional[dict]:
    """A single history row by id, scoped to app_name so a row belonging to a
    different app is never usable as this app's rollback target - the caller
    sees the same "not found" outcome as a genuinely nonexistent id."""
    rows = sf.execute_sql(
        f"SELECT * FROM {_TABLE} WHERE id = %s AND UPPER(app_name) = UPPER(%s)",  # nosec B608 - _TABLE fixed, both params bound
        (entry_id, app_name),
    )
    return _row_to_dict(rows[0]) if rows else None


def delete_for_app(app_name: str) -> None:
    """Purge every history row for `app_name`. Called from delete_app's teardown
    so a deleted app doesn't leave orphaned rows that a later re-registration of
    the same name would then see as its own history."""
    sf.execute_sql(
        f"DELETE FROM {_TABLE} WHERE UPPER(app_name) = UPPER(%s)",  # nosec B608 - _TABLE fixed, app_name parameterized
        (app_name,),
    )
