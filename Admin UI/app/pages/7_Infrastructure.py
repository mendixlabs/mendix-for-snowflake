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
