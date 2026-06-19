"""Per-app authorization for the controller.

Two caller types reach the controller on different network paths:

- The admin UI calls over the internal service-to-service endpoint. SPCS injects
  no identity there, so the admin UI vouches for the operator's roles in the
  ``X-Operator-Roles`` header (it derived them from Snowflake under the operator's
  identity). Trusted at the service level.
- Machine clients (e.g. ``upload-pad.ps1``) call the public endpoint with a PAT.
  SPCS injects ``Sf-Context-Current-User-Token`` (because the service runs with
  ``executeAsCaller: true``), which is Snowflake-asserted and cannot be forged.
  We resolve the caller's roles ourselves and ignore any ``X-Operator-Roles``.

The discriminator is the presence of the caller token. See the memory
``reference-spcs-ingress-identity-headers`` for why this is the correct boundary.
"""
from __future__ import annotations

import json
import logging
import os

import snowflake.connector
from fastapi import Request

logger = logging.getLogger(__name__)

_CALLER_TOKEN_HEADER = "Sf-Context-Current-User-Token"
_OPERATOR_ROLES_HEADER = "X-Operator-Roles"
_SERVICE_TOKEN_FILE = "/snowflake/session/token"


def _privileged_roles() -> frozenset[str]:
    raw = os.environ.get("PRIVILEGED_ROLES", "MENDIX_DEPLOY_CONTROLLER_ROLE")
    return frozenset(r.strip().upper() for r in raw.split(",") if r.strip())


# Roles that may act on any app regardless of owner_role (e.g. the deploy
# automation, whose single restricted role cannot be expressed per-app).
PRIVILEGED_ROLES = _privileged_roles()


def _read_service_token() -> str:
    with open(_SERVICE_TOKEN_FILE) as f:
        return f.read().strip()


def _roles_from_snowflake(caller_token: str) -> set[str]:
    """Open a short-lived caller-rights session and read the caller's roles.

    Caller-rights discipline: this session runs ONLY CURRENT_AVAILABLE_ROLES(),
    opens and closes per call, and is never reused for any other query. It is
    separate from snowflake_client's owner-rights connection.
    """
    compound = f"{_read_service_token()}.{caller_token}"
    conn = snowflake.connector.connect(
        host=os.environ["SNOWFLAKE_HOST"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        token=compound,
        authenticator="oauth",
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_AVAILABLE_ROLES()")
        row = cur.fetchone()
        raw = row[0] if row else None
        roles = json.loads(raw) if raw else []
        return {str(r).upper() for r in roles}
    finally:
        conn.close()


def resolve_caller_roles(request: Request) -> set[str]:
    """Return the authoritative role set for the request, or empty (fail closed)."""
    caller_token = request.headers.get(_CALLER_TOKEN_HEADER)
    if caller_token:
        try:
            return _roles_from_snowflake(caller_token)
        except Exception:
            logger.exception("Failed to resolve caller roles from Snowflake")
            return set()
    raw = request.headers.get(_OPERATOR_ROLES_HEADER, "")
    return {r.strip().upper() for r in raw.split(",") if r.strip()}


def authorize(owner_role: str | None, roles: set[str]) -> bool:
    """True if the caller may act on an app owned by owner_role."""
    if roles & PRIVILEGED_ROLES:
        return True
    if not owner_role:
        return False
    return owner_role.upper() in roles
