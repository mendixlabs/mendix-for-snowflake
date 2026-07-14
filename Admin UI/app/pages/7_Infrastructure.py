"""Infrastructure page: compute pool settings for privileged operators."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client, is_privileged_operator
from controller_client import ControllerError

# apply_branding() runs once in streamlit_app.py, before st.navigation()/pg.run(),
# so it (and the persistent sidebar it builds) applies to every page already.
st.set_page_config(page_title="Infrastructure", layout="wide")
st.title("Infrastructure")

if not is_privileged_operator():
    st.warning("This page is restricted to privileged operators.")
    st.stop()

st.subheader("Compute Pool")
st.caption(
    "Resize the shared compute pool. Changes take effect immediately; "
    "running app services are not restarted."
)

try:
    pool = client().get_compute_pool()
except ControllerError as e:
    st.error(f"Could not load compute pool: {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("State", pool.get("state") or "—")
c2.metric("Instance family", pool.get("instance_family") or "—")
c3.metric("Services running", pool.get("num_services") if pool.get("num_services") is not None else "—")
c4.metric("Pool name", pool.get("name") or "—")

st.divider()

with st.form("compute_pool_form"):
    col1, col2, col3 = st.columns(3)
    with col1:
        min_nodes = st.number_input(
            "Min nodes",
            min_value=1,
            max_value=128,
            value=int(pool.get("min_nodes") or 1),
            step=1,
        )
    with col2:
        max_nodes = st.number_input(
            "Max nodes",
            min_value=1,
            max_value=128,
            value=int(pool.get("max_nodes") or 1),
            step=1,
        )
    with col3:
        auto_suspend = st.number_input(
            "Auto-suspend (seconds, 0 = disabled)",
            min_value=0,
            value=int(pool.get("auto_suspend_secs") or 3600),
            step=60,
        )
    submitted = st.form_submit_button("Save", type="primary")

if submitted:
    if min_nodes > max_nodes:
        st.error("Min nodes cannot exceed max nodes.")
    else:
        try:
            updated = client().update_compute_pool(
                min_nodes=min_nodes,
                max_nodes=max_nodes,
                auto_suspend_secs=auto_suspend,
            )
            st.success(
                f"Compute pool updated: MIN_NODES={updated.get('min_nodes')}, "
                f"MAX_NODES={updated.get('max_nodes')}, "
                f"AUTO_SUSPEND_SECS={updated.get('auto_suspend_secs')}"
            )
        except ControllerError as e:
            st.error(f"Update failed: {e}")

st.divider()

# --- Egress IP expiry ---------------------------------------------------------
st.subheader("Egress IP expiry")
st.caption(
    "SPCS's outbound IP whitelist rotates on Snowflake's own schedule with no push "
    "notice, so this section (and the Apps-page banner below the expiry threshold) "
    "is the only warning you get. Refresh the consumer's Postgres network policy "
    "ingress rule with the current CIDRs before they expire - see the regenerated "
    "fix-up SQL below."
)

try:
    _egress = client().get_egress_status()
except ControllerError as e:
    st.error(f"Could not load egress status: {e}")
    _egress = None

if _egress is not None:
    _days_remaining = _egress.get("days_remaining")
    ec1, ec2 = st.columns(2)
    ec1.metric("Min expiry", _egress.get("min_expiry") or "—")
    ec2.metric("Days remaining", _days_remaining if _days_remaining is not None else "—")

    _ranges = _egress.get("ranges") or []
    if _ranges:
        st.dataframe(_ranges, use_container_width=True, hide_index=True)
    else:
        st.caption("No egress ranges recorded yet - the daily watch hasn't completed an iteration.")

    st.markdown("**Fix-up SQL for the Postgres network policy**")
    ingress_rule_name = st.text_input(
        "PG network policy ingress rule name", value="SPCS_TO_PG_INGRESS",
        help="Must match the rule created in the Setup page's step 1.",
        key="egress_ingress_rule_name",
    )
    _cidr_list = [r.get("ipv4_prefix") for r in _ranges if r.get("ipv4_prefix")]
    _cidr_clause = ", ".join(f"'{c}'" for c in _cidr_list) if _cidr_list else "'<cidr1>', '<cidr2>'"
    st.code(
        f"""-- Current SPCS egress IP ranges (regenerated from the values above).
ALTER NETWORK RULE {ingress_rule_name}
  SET VALUE_LIST = ({_cidr_clause});""",
        language="sql",
    )

    st.markdown("**Acknowledge this rotation**")
    st.caption(
        "Silences the banner/email for this specific expiry; both reappear "
        "automatically once Snowflake rotates to a later one."
    )
    ack_c1, ack_c2 = st.columns([2, 1])
    with ack_c1:
        _ack_date = st.date_input("Acknowledge through", key="egress_ack_date")
    with ack_c2:
        st.write("")  # vertical alignment with the date input's label row
        if st.button("Acknowledge", key="egress_ack_button"):
            try:
                client().ack_egress(_ack_date.isoformat())
                st.success(f"Acknowledged through {_ack_date.isoformat()}.")
                st.cache_data.clear()
                st.rerun()
            except ControllerError as e:
                st.error(f"Failed to acknowledge: {e}")

    st.markdown("**Email alerts (optional)**")
    st.caption(
        "Sent once per day while the expiry is within 30 days and unacknowledged. "
        "Recipients must be verified same-account user emails, and the consumer "
        "must CREATE NOTIFICATION INTEGRATION + GRANT USAGE to the app first - see "
        "the Setup page's 'Email alerts' section for the copyable SQL."
    )
    with st.form("egress_alert_form"):
        ac1, ac2 = st.columns(2)
        with ac1:
            _integration_name = st.text_input(
                "Notification integration name", value=_egress.get("alert_integration") or "",
            )
        with ac2:
            _recipients_text = st.text_input(
                "Recipients (comma-separated)",
                value=", ".join(_egress.get("alert_recipients") or []),
            )
        _alert_submitted = st.form_submit_button("Save alert config", type="primary")
    if _alert_submitted:
        _recipients = [r.strip() for r in _recipients_text.split(",") if r.strip()]
        try:
            client().set_egress_alert_config(_integration_name.strip(), _recipients)
            st.success("Alert configuration saved.")
            st.cache_data.clear()
            st.rerun()
        except ControllerError as e:
            st.error(f"Failed to save alert config: {e}")
