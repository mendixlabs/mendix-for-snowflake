from __future__ import annotations

import logging
import os
import re
import textwrap
import threading
from typing import Any

import snowflake.connector
from snowflake.connector import errors as _sf_errors

logger = logging.getLogger(__name__)

# Errnos that mean "re-authenticate": the SPCS service session token rotates, and
# a long-lived cached connection eventually presents an expired one. 390114 is
# "Authentication token has expired."
_REAUTH_ERRNOS = {390114, 390111, 390195}


def _is_recoverable(exc: Exception) -> bool:
    """True if the error is an auth/connection failure where reconnecting with a
    freshly-read session token is worth one retry (the statement did not run)."""
    if getattr(exc, "errno", None) in _REAUTH_ERRNOS:
        return True
    if isinstance(exc, (_sf_errors.OperationalError, _sf_errors.InterfaceError)):
        return True
    return "token has expired" in str(exc).lower()

# RLock so execute_sql can hold the lock while calling get_connection (which also acquires it).
_lock = threading.RLock()
_conn: snowflake.connector.SnowflakeConnection | None = None

# Bound how long a hung connect() or in-flight statement can block the single
# shared connection, since a wedged call would otherwise stall every app the
# single-worker controller manages.
_LOGIN_TIMEOUT_SECS = 30
_NETWORK_TIMEOUT_SECS = 60
_STATEMENT_TIMEOUT_SECS = 60

def require_env(name: str) -> str:
    """Read a required env var, raising a clear RuntimeError instead of a bare
    KeyError if a future service-spec edit ever drops it."""
    try:
        return os.environ[name]
    except KeyError:
        raise RuntimeError(f"Missing required env var: {name}") from None


_DB_SCHEMA = require_env("DB_SCHEMA")


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
                login_timeout=_LOGIN_TIMEOUT_SECS,
                network_timeout=_NETWORK_TIMEOUT_SECS,
            )
    return _conn


def execute_sql(sql: str, params: tuple = ()) -> list[dict]:
    # Hold the lock for the entire operation: the Snowflake connector is not
    # thread-safe on a single connection, so concurrent cursor use would interleave.
    global _conn
    with _lock:
        try:
            conn = get_connection()
            cur = conn.cursor(snowflake.connector.DictCursor)
            cur.execute(sql, params, timeout=_STATEMENT_TIMEOUT_SECS)
            return cur.fetchall()
        except Exception as e:
            # Drop the connection so the next get_connection() reconnects and
            # re-reads the (rotated) session token from /snowflake/session/token.
            _conn = None
            if not _is_recoverable(e):
                raise
            logger.warning("Snowflake call failed (%s); reconnecting and retrying once", e)
        # Single retry on a fresh connection. The original statement did not run
        # (auth/connection failed before execution), so this is safe to repeat.
        conn = get_connection()
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute(sql, params)
        return cur.fetchall()


# Schema-qualified plain identifier: letters, digits, underscore, $, dot
# separators. No quotes, spaces, or semicolons, so a value interpolated here
# cannot break out of the identifier position in the DDL below.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")


def _assert_identifier(fqn: str) -> None:
    if not _IDENTIFIER_RE.match(fqn):
        raise ValueError(f"unsafe SQL identifier: {fqn!r}")


def create_or_replace_secret(fqn: str, value: str) -> None:
    # Backstop for the secret name: callers derive it from constant names that may
    # originate in an uploaded PAD (untrusted), not only from validated API input.
    _assert_identifier(fqn)
    escaped = value.replace("'", "''")
    execute_sql(f"CREATE OR REPLACE SECRET {fqn} TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'")


def drop_secret(fqn: str) -> None:
    _assert_identifier(fqn)
    execute_sql(f"DROP SECRET IF EXISTS {fqn}")


def create_schema(fqn: str) -> None:
    _assert_identifier(fqn)
    execute_sql(f"CREATE SCHEMA IF NOT EXISTS {fqn}")


def drop_schema_cascade(fqn: str) -> None:
    _assert_identifier(fqn)
    # Safety interlock: the controller only ever drops per-app schemas. A bug
    # that routed APP_PUBLIC (registry, deploy stage, services) here would
    # destroy the installation, so refuse anything else outright.
    if not fqn.split(".")[-1].upper().startswith("MXAPP_"):
        raise ValueError(f"refusing to drop non-per-app schema: {fqn!r}")
    execute_sql(f"DROP SCHEMA IF EXISTS {fqn} CASCADE")


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
    # Without this, get_service_logs() 403s for every caller (including
    # ACCOUNTADMIN holding app_admin): a freshly created service is owned by
    # the application itself, and OWNERSHIP does not cascade MONITOR to
    # application roles that merely have CREATE SERVICE on the schema.
    execute_sql(f"GRANT MONITOR ON SERVICE {_DB_SCHEMA}.{name} TO APPLICATION ROLE {APP_ADMIN_ROLE}")


def alter_service_spec(name: str, spec: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} FROM SPECIFICATION $${spec}$$")


def suspend_service(name: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} SUSPEND")


def resume_service(name: str) -> None:
    execute_sql(f"ALTER SERVICE {_DB_SCHEMA}.{name} RESUME")


def drop_service(name: str) -> None:
    execute_sql(f"DROP SERVICE IF EXISTS {_DB_SCHEMA}.{name}")


# ---------------------------------------------------------------------------
# Endpoint access control (data plane)
#
# Gates which authenticated end-users may open an app's public endpoint, via the
# service's auto-created ALL_ENDPOINTS_USAGE service role granted to a per-app
# APPLICATION role. Distinct from owner_role (management plane).
#
# Inside the Native App the controller session is the app role, which owns the
# services and every application role, so it can both create the per-app
# application role at runtime and grant the service role to it (no account-level
# CREATE ROLE / MANAGE GRANTS needed). Runtime CREATE/GRANT/DROP APPLICATION ROLE
# is validated (O1). End-user membership is managed by the consumer (SCIM/IdP):
#   GRANT APPLICATION ROLE <app>.app_<name>_user TO USER <u>;
# ---------------------------------------------------------------------------

# The management application role operators hold (created in setup_script.sql).
# Endpoint access is granted to it for owner bootstrap (an operator can reach any
# app before the per-app IdP group is populated), mirroring the old owner_role grant.
APP_ADMIN_ROLE = "app_admin"


def app_access_role_name(app_name: str) -> str:
    """Application role that gates browser access to an app's endpoint."""
    return f"app_{app_name.lower()}_user"


def create_app_access_role(app_name: str) -> None:
    execute_sql(
        f"CREATE APPLICATION ROLE IF NOT EXISTS {app_access_role_name(app_name)}"
    )


def grant_endpoint_to_app_role(service_name: str, app_role: str) -> None:
    """Authorize an application role to reach the service's public endpoint."""
    execute_sql(
        f"GRANT SERVICE ROLE {_DB_SCHEMA}.{service_name}!ALL_ENDPOINTS_USAGE "
        f"TO APPLICATION ROLE {app_role}"
    )


def drop_app_access_role(app_name: str) -> None:
    execute_sql(
        f"DROP APPLICATION ROLE IF EXISTS {app_access_role_name(app_name)}"
    )


def show_service_status(name: str) -> str | None:
    try:
        rows = execute_sql(f"DESCRIBE SERVICE {_DB_SCHEMA}.{name}")
        if rows:
            return rows[0].get("status")
    except Exception:
        logger.warning("show_service_status failed for %s.%s", _DB_SCHEMA, name, exc_info=True)
        return None
    return None


def show_all_service_statuses() -> dict[str, str]:
    """Return {SERVICE_NAME: status} for all services in the schema, in one query."""
    try:
        rows = execute_sql(f"SHOW SERVICES IN SCHEMA {_DB_SCHEMA}")
        return {row["name"]: row.get("status") for row in rows}
    except Exception:
        logger.warning("show_all_service_statuses failed for schema %s", _DB_SCHEMA, exc_info=True)
        return {}


def get_service_endpoint(name: str) -> str | None:
    try:
        rows = execute_sql(f"SHOW ENDPOINTS IN SERVICE {_DB_SCHEMA}.{name}")
        for row in rows:
            url = row.get("ingress_url")
            # While ingress is still provisioning, SHOW ENDPOINTS returns a
            # human-readable message ("Endpoints provisioning in progress. ...")
            # in this column rather than a host. A real host has a dot and no
            # spaces; anything else means "not available yet".
            if url and "." in url and " " not in url:
                return url if url.startswith("https://") else f"https://{url}"
    except Exception:
        logger.warning("get_service_endpoint failed for %s.%s", _DB_SCHEMA, name, exc_info=True)
        return None
    return None


def get_service_logs(name: str, container: str = "mendix-app", lines: int = 100) -> str:
    # Bind the arguments rather than interpolate: callers pass allowlisted service +
    # container names today, but binding keeps this safe regardless of the source.
    rows = execute_sql(
        "SELECT SYSTEM$GET_SERVICE_LOGS(%s, 0, %s, %s) AS logs",
        (f"{_DB_SCHEMA}.{name}", container, int(lines)),
    )
    if rows:
        return rows[0].get("LOGS", "")
    return ""


def put_file(local_path: str, stage_path: str) -> None:
    """Upload a local file to an internal stage via PUT."""
    execute_sql(f"PUT file://{local_path} {stage_path} AUTO_COMPRESS=FALSE OVERWRITE=TRUE")


def get_compute_pool(pool_name: str) -> dict | None:
    rows = execute_sql(f"SHOW COMPUTE POOLS LIKE '{pool_name}'")
    if not rows:
        return None
    row = rows[0]
    return {
        "name": row.get("name"),
        "state": row.get("state"),
        "min_nodes": row.get("min_nodes"),
        "max_nodes": row.get("max_nodes"),
        "instance_family": row.get("instance_family"),
        "auto_suspend_secs": row.get("auto_suspend_secs"),
        "num_services": row.get("num_services"),
    }


def alter_compute_pool(
    pool_name: str,
    *,
    min_nodes: int | None = None,
    max_nodes: int | None = None,
    auto_suspend_secs: int | None = None,
) -> None:
    parts = []
    if min_nodes is not None:
        parts.append(f"MIN_NODES = {int(min_nodes)}")
    if max_nodes is not None:
        parts.append(f"MAX_NODES = {int(max_nodes)}")
    if auto_suspend_secs is not None:
        parts.append(f"AUTO_SUSPEND_SECS = {int(auto_suspend_secs)}")
    if not parts:
        return
    execute_sql(f"ALTER COMPUTE POOL {pool_name} SET {' '.join(parts)}")
