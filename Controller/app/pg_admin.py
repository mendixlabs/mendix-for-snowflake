"""Per-app Postgres role/database provisioning (the tenant isolation boundary).

Before this module existed, every Mendix app container connected to the shared
Snowflake-managed Postgres instance as the same bootstrap ``application`` role,
using one password shared by every app. Any container could therefore open a
connection to any other app's database: a full cross-tenant breach.

The fix implemented here: for each app, the controller provisions a dedicated
Postgres role with its own randomly generated password, and a dedicated
database that only that role (plus the bootstrap ``application`` role, which
owns it) may connect to. The container is handed only its own scoped
credential, never the bootstrap one.

Isolation model
----------------
- ``application`` is the bootstrap superuser-ish role. It is the only
  identity that can create/alter roles and databases, and it owns every
  per-app database so the controller can later drop it. **Only the
  controller holds this credential — a customer's app container never
  receives it.**
- Each app gets a role ``pg_username`` with LOGIN but NOSUPERUSER,
  NOCREATEDB, NOCREATEROLE, and a fresh random password.
- ``REVOKE CONNECT ... FROM PUBLIC`` on the app's database, followed by
  ``GRANT CONNECT ... TO`` only that app's role, is the actual isolation
  boundary: without the REVOKE, every other role (including every other
  app's role) retains the default PUBLIC CONNECT privilege Postgres grants
  new databases, and the breach this module fixes would persist.
- The app's role is then given rights to create objects in its own
  database's ``public`` schema, since Mendix creates its own tables there
  on first boot.

This module is intentionally pure: it takes connection parameters and
credentials as arguments and never reads secrets, environment variables, or
``app.main`` itself, so it can be unit-tested without any of that machinery
and without a real Postgres server.
"""
from __future__ import annotations

import logging
import secrets

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)

# The bootstrap role every controller-side connection authenticates as. Never
# used by, or exposed to, a customer's app container.
_BOOTSTRAP_USER = "application"

_DEFAULT_PORT = 5432


def _parse_host_port(host_port: str) -> tuple[str, int]:
    """Split "host:port" into (host, port), defaulting to 5432 if no port."""
    host, sep, port = host_port.rpartition(":")
    if not sep:
        return host_port, _DEFAULT_PORT
    return host, int(port)


def _connect(host_port: str, password: str, dbname: str) -> psycopg.Connection:
    """Open an autocommit connection as the bootstrap ``application`` role.

    Autocommit is required because CREATE DATABASE / DROP DATABASE cannot run
    inside a transaction block; using it for every statement here also keeps
    each grant/revoke independently effective even if a later statement in
    the same call fails.
    """
    host, port = _parse_host_port(host_port)
    return psycopg.connect(
        host=host,
        port=port,
        user=_BOOTSTRAP_USER,
        password=password,
        dbname=dbname,
        sslmode="require",
        autocommit=True,
    )


def provision_app(
    host_port: str,
    bootstrap_password: str,
    pg_database: str,
    pg_username: str,
) -> str:
    """Idempotently ensure a per-app Postgres role + database exist and are
    isolated from every other app, and return the role's freshly generated
    password.

    Safe to call repeatedly (e.g. on redeploy): an existing role has its
    password rotated rather than failing, and an existing database is left in
    place (only the CONNECT grants are re-asserted).
    """
    password = secrets.token_urlsafe(32)

    with _connect(host_port, bootstrap_password, "postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (pg_username,)
            )
            role_exists = cur.fetchone() is not None

            role_verb = "ALTER ROLE" if role_exists else "CREATE ROLE"
            cur.execute(
                sql.SQL(
                    role_verb
                    + " {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(pg_username), sql.Literal(password))
            )

            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (pg_database,)
            )
            db_exists = cur.fetchone() is not None

            if not db_exists:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(pg_database), sql.Identifier(_BOOTSTRAP_USER)
                    )
                )

            # The isolation boundary: without the REVOKE, PUBLIC keeps the
            # default CONNECT privilege Postgres grants every new database,
            # and every other app's role could still connect here.
            cur.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(pg_database)
                )
            )
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(pg_database), sql.Identifier(pg_username)
                )
            )

    # Mendix builds its own tables in `public` on first boot, so the app role
    # needs rights to create objects there. This requires a second connection
    # to the app's own database (GRANT ... ON SCHEMA is database-local).
    # GRANT ALL grants CREATE + USAGE, which is all Mendix needs; tables it
    # creates are owned by the app role. We deliberately do NOT reassign the
    # schema's ownership: `application` owns the database (and, in PG15+, the
    # public schema) and is not a member of the per-app role, so
    # `ALTER SCHEMA public OWNER TO <app_role>` would fail with "must be member
    # of role" - and it buys nothing, since CREATE + USAGE already lets the app
    # own and manage its own objects.
    with _connect(host_port, bootstrap_password, pg_database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(
                    sql.Identifier(pg_username)
                )
            )

    return password


def deprovision_app(
    host_port: str,
    bootstrap_password: str,
    pg_database: str,
    pg_username: str,
) -> None:
    """Best-effort teardown of an app's Postgres role and database.

    Every step is attempted even if an earlier one fails (mirroring
    ``main._teardown_app_objects``'s best-effort style): each failure is
    logged and swallowed rather than raised, so a partially-broken app can
    still have its remaining Postgres objects cleaned up.
    """
    try:
        conn = _connect(host_port, bootstrap_password, "postgres")
    except Exception:
        logger.warning(
            "deprovision_app %s: failed to connect as bootstrap role", pg_database,
            exc_info=True,
        )
        return

    with conn:
        with conn.cursor() as cur:
            try:
                # Terminate any live sessions first; DROP DATABASE fails if
                # anything is still connected.
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s",
                    (pg_database,),
                )
            except Exception:
                logger.warning(
                    "deprovision_app %s: pg_terminate_backend failed", pg_database,
                    exc_info=True,
                )

            try:
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(pg_database)
                    )
                )
            except Exception:
                logger.warning(
                    "deprovision_app %s: DROP DATABASE failed", pg_database,
                    exc_info=True,
                )

            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(pg_username)
                    )
                )
            except Exception:
                logger.warning(
                    "deprovision_app %s: DROP ROLE %s failed", pg_database, pg_username,
                    exc_info=True,
                )
