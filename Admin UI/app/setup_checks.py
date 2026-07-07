"""Verification SQL for the consumer-owned prerequisites the Native App cannot
create itself: the Snowflake-managed Postgres instance, the egress EAI, and the
PG credential secret bound as a reference.

This is rendered as a copy-paste block for the operator to run in their own
Snowsight session, not executed by the app. The app's own session runs under
restricted caller's rights (the operator's privileges intersected with the
application object's), and the app holds no grant on these account-level
objects, so that session can never see them - activating extra roles doesn't
change that, since the app side of the intersection is the binding constraint.

pg_secret and pg_eai are separately app-scoped references and cannot be probed
from either session; their binding is implied by this admin UI being reachable
at all, because the services start only after both references bind
(setup_script.sql::maybe_start_services).
"""
from __future__ import annotations


def _secret_show_clause(secret_fqn: str) -> str:
    """SHOW SECRETS clause for a 1-, 2-, or 3-part secret name."""
    parts = secret_fqn.split(".")
    name = parts[-1]
    if len(parts) == 3:
        return f"SHOW SECRETS LIKE '{name}' IN SCHEMA {parts[0]}.{parts[1]};"
    if len(parts) == 2:
        return f"SHOW SECRETS LIKE '{name}' IN SCHEMA {parts[0]};"
    return f"SHOW SECRETS LIKE '{name}';"


def render_verify_sql(instance: str, eai: str, secret_fqn: str) -> str:
    """Render the SQL block that checks the three prerequisites exist and look
    healthy, for the operator to run in their own Snowsight session."""
    return f"""SHOW POSTGRES INSTANCES LIKE '{instance}';
-- expect one row, state READY or ACTIVE

SHOW EXTERNAL ACCESS INTEGRATIONS LIKE '{eai}';
-- expect one row, enabled = true

{_secret_show_clause(secret_fqn)}
-- expect one row"""
