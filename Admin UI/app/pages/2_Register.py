"""Register page: create a new app via POST /apps."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client, operator_roles
from branding import apply_branding
from controller_client import ControllerError

st.set_page_config(page_title="Register", layout="centered")
apply_branding()
st.title("Register a new app")

_DEFAULT_OWNER_ROLE = "MENDIX_ADMIN_OPERATOR_ROLE"
_owner_candidates = [
    r for r in operator_roles()
    if r != "PUBLIC" and not r.startswith("SNOWFLAKE_")
]

st.caption(
    "Creates the SPCS service, filestorage stage, and PG/admin secrets. "
    "Upload the PAD afterward via the Upload page (`snow stage copy`), then Redeploy on the Apps page."
)

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

with st.form("register"):
    name = st.text_input(
        "App name",
        help="Letters, digits, underscores. Must start with a letter.",
    )
    pg_database = st.text_input(
        "Postgres database name",
        help="Database is auto-created at first start if it does not exist.",
    )
    admin_password = st.text_input(
        "MxAdmin password",
        type="password",
        help="Stored as a Snowflake secret and mounted into the service.",
    )
    resource_tier = st.selectbox(
        "Resource tier",
        options=["small", "medium", "large"],
        index=1,
    )
    use_caller_rights = st.checkbox(
        "Enable caller's rights",
        value=False,
        help="Sets executeAsCaller=true so the service receives the operator's "
             "OAuth token on each request.",
    )
    if _owner_candidates:
        owner_role = st.selectbox(
            "Owner role",
            options=_owner_candidates,
            help="Operators holding this role will see and manage the app.",
        )
    else:
        st.warning(
            "Could not resolve your Snowflake roles; the app will be owned by "
            f"`{_DEFAULT_OWNER_ROLE}`."
        )
        owner_role = _DEFAULT_OWNER_ROLE
    constants_text = st.text_area(
        "Constants (JSON object, optional)",
        value="{}",
        help='e.g. { "Module.Setting": "value" }',
        height=150,
    )
    license_id = st.text_input(
        "Mendix license ID (optional)",
        help="Leave both license fields blank to deploy trial-licensed (6 concurrent "
             "users, restarts every 2-4 hours). Can also be set later on the Apps page.",
    )
    license_key = st.text_input(
        "Mendix license key (optional)",
        type="password",
        help="Required if a license ID is given above.",
    )
    submitted = st.form_submit_button("Register", type="primary")

if submitted:
    errors: list[str] = []
    if not _NAME_RE.match(name or ""):
        errors.append("App name must match `^[A-Za-z][A-Za-z0-9_]*$`.")
    if not pg_database:
        errors.append("Postgres database name is required.")
    if not admin_password:
        errors.append("MxAdmin password is required.")
    constants: dict = {}
    try:
        parsed = json.loads(constants_text or "{}")
        if not isinstance(parsed, dict):
            errors.append("Constants must be a JSON object.")
        else:
            constants = parsed
    except json.JSONDecodeError as e:
        errors.append(f"Constants JSON is invalid: {e}")
    if bool(license_id) != bool(license_key):
        errors.append("Provide both a license ID and a license key, or leave both blank.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        payload = {
            "name": name,
            "pg_database": pg_database,
            "admin_password": admin_password,
            "resource_tier": resource_tier,
            "use_caller_rights": use_caller_rights,
            "constants": constants,
            "owner_role": owner_role,
        }
        if license_id and license_key:
            payload["license_id"] = license_id
            payload["license_key"] = license_key
        try:
            with st.spinner("Registering app..."):
                result = client().create_app(payload)
            st.cache_data.clear()
            st.success(f"Registered. Service `{result.get('service_name')}` is starting.")
            st.page_link("pages/1_Apps.py", label="Go to Apps", icon=":material/arrow_forward:")
        except ControllerError as e:
            st.error(str(e))
