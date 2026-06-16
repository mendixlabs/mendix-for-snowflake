from __future__ import annotations

import logging
import os
import textwrap
import threading
from typing import Any

import snowflake.connector

logger = logging.getLogger(__name__)

# RLock so execute_sql can hold the lock while calling get_connection (which also acquires it).
_lock = threading.RLock()
_conn: snowflake.connector.SnowflakeConnection | None = None

_DB_SCHEMA = os.environ["DB_SCHEMA"]


def _read_token() -> str:
    with open("/snowflake/session/token") as f:
        return f.read().strip()


def _db_and_schema() -> tuple[str, str]:
    # DB_SCHEMA is set in the controller service spec, e.g. "YOUR_DB.PUBLIC"
    db_schema = os.environ["DB_SCHEMA"]
    parts = db_schema.split(".", 1)
    return parts[0], parts[1]


def get_connection() -> snowflake.connector.SnowflakeConnection:
    global _conn
    with _lock:
        if _conn is None or _conn.is_closed():
            database, schema = _db_and_schema()
            _conn = snowflake.connector.connect(
                host=os.environ["SNOWFLAKE_HOST"],
                account=os.environ["SNOWFLAKE_ACCOUNT"],
                token=_read_token(),
                authenticator="oauth",
                database=database,
                schema=schema,
            )
    return _conn


def execute_sql(sql: str, params: tuple = ()) -> list[dict]:
    # Hold the lock for the entire operation: the Snowflake connector is not
    # thread-safe on a single connection, so concurrent cursor use would interleave.
    with _lock:
        conn = get_connection()
        try:
            cur = conn.cursor(snowflake.connector.DictCursor)
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            global _conn
            _conn = None
            raise


def create_or_replace_secret(fqn: str, value: str) -> None:
    escaped = value.replace("'", "''")
    execute_sql(f"CREATE OR REPLACE SECRET {fqn} TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'")


def create_stage(fqn: str) -> None:
    execute_sql(f"CREATE STAGE IF NOT EXISTS {fqn} ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')")


def create_service(name: str, spec: str, compute_pool: str, eai: str, warehouse: str) -> None:
    execute_sql(textwrap.dedent(f"""\
        CREATE SERVICE {_DB_SCHEMA}.{name}
            IN COMPUTE POOL {compute_pool}
            FROM SPECIFICATION $${spec}$$
            MIN_INSTANCES = 1
            MAX_INSTANCES = 1
            EXTERNAL_ACCESS_INTEGRATIONS = ({eai})
            QUERY_WAREHOUSE = {warehouse}
    """))


def alter_service_spec(name: str, spec: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} FROM SPECIFICATION $${spec}$$")


def suspend_service(name: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} SUSPEND")


def resume_service(name: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} RESUME")


def drop_service(name: str) -> None:
    execute_sql(f"DROP SERVICE IF EXISTS {_DB_SCHEMA}.{name}")


def show_service_status(name: str) -> str | None:
    try:
        rows = execute_sql(f"DESCRIBE SERVICE {_DB_SCHEMA}.{name}")
        if rows:
            return rows[0].get("status")
    except Exception:
        return None
    return None


def get_service_endpoint(name: str) -> str | None:
    rows = execute_sql(f"SHOW ENDPOINTS IN SERVICE {_DB_SCHEMA}.{name}")
    for row in rows:
        url = row.get("ingress_url")
        if url:
            return f"https://{url}" if not url.startswith("https://") else url
    return None


def set_caller_token_validity(name: str, secs: int = 1800) -> None:
    try:
        execute_sql(
            f"ALTER SERVICE {_DB_SCHEMA}.{name} "
            f"SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = {secs}"
        )
    except Exception as e:
        # Schema-level fallback intentionally omitted: it would affect all services in the schema.
        logger.warning("Could not set SERVICE_CALLER_TOKEN_VALIDITY_SECS on %s: %s", name, e)


def get_service_logs(name: str, container: str = "mendix-app", lines: int = 100) -> str:
    rows = execute_sql(
        f"SELECT SYSTEM$GET_SERVICE_LOGS('{_DB_SCHEMA}.{name}', 0, '{container}', {lines}) AS logs"
    )
    if rows:
        return rows[0].get("LOGS", "")
    return ""


def put_file(local_path: str, stage_path: str) -> None:
    """Upload a local file to an internal stage via PUT."""
    execute_sql(f"PUT file://{local_path} {stage_path} AUTO_COMPRESS=FALSE OVERWRITE=TRUE")
