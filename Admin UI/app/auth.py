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


def list_operator_roles() -> tuple[str, ...]:
    """Resolve the operator's available roles via a caller-rights session.

    Opens a compound-token session, runs only CURRENT_AVAILABLE_ROLES(), closes.
    Returns an empty tuple if the caller token is unavailable (e.g. the service
    is not running with executeAsCaller, or the request carried no caller token).
    """
    caller_token = _caller_token()
    if not caller_token:
        return ()
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


def controller_url() -> str:
    return os.environ.get("CONTROLLER_URL", "http://mendix-deploy-controller:8080")


@st.cache_resource
def get_client(operator: str, roles: tuple[str, ...]) -> ControllerClient:
    return ControllerClient(controller_url(), operator=operator, roles=roles)


def client() -> ControllerClient:
    return get_client(current_operator(), operator_roles())
