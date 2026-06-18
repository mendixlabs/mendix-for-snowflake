"""Audit log of mutating operations performed via the controller."""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from typing import Optional

from . import snowflake_client as sf

logger = logging.getLogger(__name__)

_TABLE = f"{os.environ['DB_SCHEMA']}.MENDIX_ACTIVITY"

_ACTION_PATTERNS = [
    (re.compile(r"^/apps/([^/]+)/deploy$"), "deploy"),
    (re.compile(r"^/apps/([^/]+)/trigger-deploy$"), "deploy"),
    (re.compile(r"^/apps/([^/]+)/suspend$"), "suspend"),
    (re.compile(r"^/apps/([^/]+)/resume$"), "resume"),
    (re.compile(r"^/apps/([^/]+)/constants$"), "update_constants"),
    (re.compile(r"^/apps/([^/]+)/spec$"), "update_spec"),
    (re.compile(r"^/apps/([^/]+)$"), "delete"),
]


def init_table() -> None:
    """Idempotent: creates MENDIX_ACTIVITY and grants the controller role."""
    sf.execute_sql(textwrap.dedent(f"""\
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id        NUMBER AUTOINCREMENT PRIMARY KEY,
            ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operator  VARCHAR,
            action    VARCHAR,
            app_name  VARCHAR,
            detail    VARIANT,
            result    VARCHAR
        )
    """))
    sf.execute_sql(
        f"GRANT SELECT, INSERT ON TABLE {_TABLE} "
        "TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE"
    )


def derive_action(method: str, path: str) -> tuple[str, Optional[str]]:
    """Return (action_name, app_name) for a mutating request."""
    if method == "POST" and path == "/apps":
        return ("create", None)
    for pattern, action in _ACTION_PATTERNS:
        m = pattern.match(path)
        if m:
            return (action, m.group(1))
    return ("unknown", None)


def insert(operator: str, action: str, app_name: Optional[str],
           detail: dict, result: str = "accepted") -> None:
    sf.execute_sql(
        f"INSERT INTO {_TABLE} (operator, action, app_name, detail, result) "
        "SELECT %s, %s, %s, PARSE_JSON(%s), %s",
        (operator, action, app_name, json.dumps(detail), result),
    )


def query(app: Optional[str] = None, operator: Optional[str] = None,
          limit: int = 100) -> list[dict]:
    where: list[str] = []
    params: list = []
    if app:
        where.append("app_name = %s")
        params.append(app)
    if operator:
        where.append("operator = %s")
        params.append(operator)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT id, ts, operator, action, app_name, detail, result "
        f"FROM {_TABLE} {where_clause} "
        f"ORDER BY ts DESC LIMIT {int(limit)}"
    )
    rows = sf.execute_sql(sql, tuple(params))
    out: list[dict] = []
    for r in rows:
        detail = r.get("DETAIL")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                pass
        out.append({
            "id": r.get("ID"),
            "ts": str(r["TS"]) if r.get("TS") else None,
            "operator": r.get("OPERATOR"),
            "action": r.get("ACTION"),
            "app_name": r.get("APP_NAME"),
            "detail": detail,
            "result": r.get("RESULT"),
        })
    return out
