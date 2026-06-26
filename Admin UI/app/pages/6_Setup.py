"""Setup / Verify page: the one-time consumer-side prerequisites the Native App
cannot create itself, plus a check that they exist and look healthy.

The app installs the controller + admin UI on its own, but a Snowflake-managed
Postgres instance, its network policy, the egress EAI, and the PG credential
secret are account-level objects an application object may not create. An
ACCOUNTADMIN runs the SQL below once, binds pg_secret + pg_eai at install, then
the services come up. This page is a copyable runbook for that work and a
verifier for the consumer-owned pieces.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from branding import apply_branding
from setup_checks import run_checks

st.set_page_config(page_title="Setup / Verify", layout="wide")
apply_branding()
st.title("Setup / Verify")
st.caption(
    "One-time, ACCOUNTADMIN-only prerequisites the app cannot create for itself, "
    "and a check that they exist. Run the SQL outside the app (the app installs "
    "the controller and admin UI; these account-level objects do not)."
)

# --- Parameters --------------------------------------------------------------
with st.expander("Names and values", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        app_name = st.text_input("Application object", value="MENDIX_SPCS_APP")
        instance = st.text_input("Postgres instance", value="MENDIX_PG")
        pg_host = st.text_input("Postgres host", value="<pg-host>",
                                help="From the CREATE POSTGRES INSTANCE output. Shown once.")
    with c2:
        eai = st.text_input("Egress EAI", value="MENDIX_PG_EAI")
        net_policy = st.text_input("PG network policy", value="MENDIX_PG_POLICY")
        pg_port = st.text_input("Postgres port", value="5432")
    with c3:
        secret_db = st.text_input("Secret database", value="<YOUR_DB>",
                                  help="Consumer database that holds the bound secret.")
        secret_schema = st.text_input("Secret schema", value="PUBLIC")
        secret_name = st.text_input("Secret name", value="MENDIX_NATIVE_PG_SECRET")

ingress_rule = "SPCS_TO_PG_INGRESS"
egress_rule = "MENDIX_PG_EGRESS"
secret_fqn = f"{secret_db}.{secret_schema}.{secret_name}"
host_port = f"{pg_host}:{pg_port}"

st.divider()

# --- Step 1: network + Postgres instance -------------------------------------
st.subheader("1 - Postgres instance + network policy")
st.caption("ACCOUNTADMIN. Credentials in the CREATE output are shown only once - save them.")
st.code(
    f"""-- Current SPCS egress IP ranges (feed the CIDRs into the ingress rule below).
SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES();

CREATE NETWORK RULE {ingress_rule}
  TYPE = IPV4
  VALUE_LIST = ('<cidr1>', '<cidr2>')   -- from the query above; these expire, see step 5 note
  MODE = POSTGRES_INGRESS;

CREATE NETWORK POLICY {net_policy}
  ALLOWED_NETWORK_RULE_LIST = ({ingress_rule});

CREATE POSTGRES INSTANCE {instance}
  COMPUTE_FAMILY = 'STANDARD_M'
  STORAGE_SIZE_GB = 10
  AUTHENTICATION_AUTHORITY = POSTGRES
  POSTGRES_VERSION = 17
  NETWORK_POLICY = '{net_policy}';""",
    language="sql",
)

# --- Step 2: egress EAI ------------------------------------------------------
st.subheader("2 - Egress EAI (SPCS -> Postgres)")
st.caption("ACCOUNTADMIN. Bound into the app as the pg_eai reference at install.")
st.code(
    f"""CREATE NETWORK RULE {egress_rule}
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('{host_port}');

CREATE EXTERNAL ACCESS INTEGRATION {eai}
  ALLOWED_NETWORK_RULES = ({egress_rule})
  ENABLED = TRUE;""",
    language="sql",
)

# --- Step 3: grant CREATEDB to the application PG user -----------------------
st.subheader("3 - Grant CREATEDB to the `application` Postgres user")
st.caption(
    "One-time. Each Mendix app gets its own database, auto-created at startup, so "
    "the application user needs CREATEDB. The instance is unreachable from a "
    "workstation; run it as a short SPCS job using the mendix-base image."
)
st.code(
    f"""CREATE SERVICE {secret_db}.{secret_schema}.PG_SETUP_JOB
  IN COMPUTE POOL <a-compute-pool>
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  EXTERNAL_ACCESS_INTEGRATIONS = ({eai})
  FROM SPECIFICATION $$
spec:
  containers:
  - name: psql
    image: /<provider_db>/<provider_schema>/<repo>/mendix-base:latest
    command: ["bash", "-c", "PGPASSWORD='<application-password>' PGSSLMODE=require psql -h {pg_host} -p {pg_port} -U application -d postgres -c 'ALTER USER application CREATEDB;'"]
$$;

-- Wait ~30s, then confirm it logged "ALTER ROLE":
CALL SYSTEM$GET_SERVICE_LOGS('{secret_db}.{secret_schema}.PG_SETUP_JOB', '0', 'psql', 5);
DROP SERVICE {secret_db}.{secret_schema}.PG_SETUP_JOB;""",
    language="sql",
)

# --- Step 4: PG credential secret --------------------------------------------
st.subheader("4 - PG credential secret")
st.caption(
    "ACCOUNTADMIN. A single GENERIC_STRING secret holding host:port + password as "
    "JSON; the controller reads it at /secrets/pg/secret_string. Bound as pg_secret."
)
st.code(
    f"""CREATE OR REPLACE SECRET {secret_fqn}
  TYPE = GENERIC_STRING
  SECRET_STRING = '{{"host":"{host_port}","password":"<application-password>"}}';""",
    language="sql",
)

# --- Step 5: install, grant, bind --------------------------------------------
st.subheader("5 - Install the app, grant privileges, bind references")
st.caption(
    "After GET from the listing. The reference binds fire grant_callback / "
    "register_reference, which start the controller and admin UI."
)
st.code(
    f"""GRANT CREATE COMPUTE POOL   ON ACCOUNT TO APPLICATION {app_name};
GRANT CREATE WAREHOUSE      ON ACCOUNT TO APPLICATION {app_name};
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO APPLICATION {app_name};
GRANT APPLICATION ROLE {app_name}.app_admin TO ROLE ACCOUNTADMIN;

CALL {app_name}.app_public.grant_callback(
  ARRAY_CONSTRUCT('CREATE COMPUTE POOL','CREATE WAREHOUSE','BIND SERVICE ENDPOINT'));

CALL {app_name}.app_public.register_reference(
  'pg_secret','ADD', SYSTEM$REFERENCE('SECRET','{secret_fqn}','PERSISTENT','READ'));
CALL {app_name}.app_public.register_reference(
  'pg_eai','ADD',    SYSTEM$REFERENCE('EXTERNAL_ACCESS_INTEGRATION','{eai}','PERSISTENT','USAGE'));

-- Let operators manage apps (repeat per operator role):
GRANT APPLICATION ROLE {app_name}.app_admin TO ROLE <operator_role>;""",
    language="sql",
)
st.info(
    "The SPCS egress CIDRs in step 1 expire and pin the ingress rule. Refresh "
    f"`{ingress_rule}` with `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` before they lapse."
)

st.divider()

# --- Verify ------------------------------------------------------------------
st.subheader("Verify")
st.caption(
    "Checks the consumer-owned prerequisites exist, using your own roles. "
    "pg_secret and pg_eai are app-scoped references and cannot be probed here; "
    "their binding is already implied - the services (and this page) start only "
    "after both bind. Postgres egress reachability is confirmed when an app first boots."
)
if st.button("Run checks", type="primary"):
    with st.spinner("Querying..."):
        results = run_checks(instance, eai, secret_fqn)
    for r in results:
        (st.success if r.ok else st.error)(f"**{r.label}** - {r.detail}")
