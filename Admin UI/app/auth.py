"""Operator identity and role resolution from the SPCS ingress.

The operator's username arrives in the ``Sf-Context-Current-User`` header. Their
roles are not in any header; we resolve them once per browser session via a
caller-rights Snowflake session (compound token) and forward them to the
controller as ``X-Operator-Roles``. See memory
``reference-spcs-ingress-identity-headers``.
"""
from __future__ import annotations

import json
import os

import snowflake.connector
import streamlit as st

from controller_client import ControllerClient

_OPERATOR_HEADER = "Sf-Context-Current-User"
_CALLER_TOKEN_HEADER = "Sf-Context-Current-User-Token"
_ANONYMOUS = "<anonymous>"
_SERVICE_TOKEN_FILE = "/snowflake/session/token"


def current_operator() -> str:
    """Return the Snowflake username injected by SPCS ingress."""
    try:
        headers = st.context.headers
    except Exception:
        return _ANONYMOUS
    return headers.get(_OPERATOR_HEADER) or _ANONYMOUS


def _caller_token() -> str | None:
    try:
        return st.context.headers.get(_CALLER_TOKEN_HEADER)
    except Exception:
        return None


def _read_service_token() -> str:
    with open(_SERVICE_TOKEN_FILE) as f:
        return f.read().strip()


def open_caller_session() -> "snowflake.connector.SnowflakeConnection | None":
    """Open a caller-rights Snowflake session (compound token) as the operator.

    The session runs with the operator's own roles, so anything it queries
    reflects exactly what that operator is allowed to see. Returns None when no
    caller token is available (the service is not running with executeAsCaller,
    or the request carried no caller token). The caller must close() the result.
    """
    caller_token = _caller_token()
    if not caller_token:
        return None
    compound = f"{_read_service_token()}.{caller_token}"
    conn = snowflake.connector.connect(
        host=os.environ["SNOWFLAKE_HOST"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        token=compound,
        authenticator="oauth",
    )
    # The compound token lands the session in the operator's default role, which
    # for many operators cannot see ACCOUNTADMIN-owned prerequisites (the Postgres
    # instance, the EAI) or the app's specifications. Activate all of the
    # operator's granted roles as secondary roles so what the session sees matches
    # what the operator is actually entitled to. Best-effort: if the session does
    # not permit it, fall back to the primary role rather than failing outright.
    try:
        conn.cursor().execute("USE SECONDARY ROLES ALL")
    except Exception:
        pass
    return conn


def list_operator_roles() -> tuple[str, ...]:
    """Resolve the operator's available roles via a caller-rights session.

    Opens a compound-token session, runs only CURRENT_AVAILABLE_ROLES(), closes.
    Returns an empty tuple if the caller token is unavailable (e.g. the service
    is not running with executeAsCaller, or the request carried no caller token).
    """
    conn = open_caller_session()
    if conn is None:
        return ()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_AVAILABLE_ROLES()")
        row = cur.fetchone()
        raw = row[0] if row else None
        roles = json.loads(raw) if raw else []
        return tuple(str(r).upper() for r in roles)
    finally:
        conn.close()


def operator_roles() -> tuple[str, ...]:
    """The operator's roles, resolved once and cached for the browser session."""
    if "operator_roles" not in st.session_state:
        try:
            st.session_state["operator_roles"] = list_operator_roles()
        except Exception:
            st.session_state["operator_roles"] = ()
    return st.session_state["operator_roles"]


def _privileged_roles() -> frozenset[str]:
    # Mirrors the controller's PRIVILEGED_ROLES contract (same env, same default) so
    # the UI shows system options exactly to the operators the controller will allow.
    raw = os.environ.get("PRIVILEGED_ROLES", "MENDIX_DEPLOY_CONTROLLER_ROLE")
    return frozenset(r.strip().upper() for r in raw.split(",") if r.strip())


def is_privileged_operator() -> bool:
    """True if the operator holds a privileged role (gates system-wide views).

    UX gate only; the controller independently enforces this on every request.
    """
    return bool(set(operator_roles()) & _privileged_roles())


def controller_url() -> str:
    return os.environ.get("CONTROLLER_URL", "http://mendix-deploy-controller:8080")


@st.cache_resource
def get_client(operator: str, roles: tuple[str, ...]) -> ControllerClient:
    return ControllerClient(controller_url(), operator=operator, roles=roles)


def client() -> ControllerClient:
    return get_client(current_operator(), operator_roles())
