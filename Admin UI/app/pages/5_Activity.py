"""Activity page: audit log of mutating operations across apps."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from controller_client import ControllerError
from data import list_apps

# apply_branding() runs once in streamlit_app.py, before st.navigation()/pg.run(),
# so it (and the persistent sidebar it builds) applies to every page already.
st.set_page_config(page_title="Activity", layout="wide")
st.title("Activity")
st.caption(
    "Every mutating call recorded by the controller. Includes operator, action, "
    "and the request path."
)

try:
    apps = list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps for filter: {e}")
    apps = []

app_names = [a["name"] for a in apps]

cols = st.columns([2, 2, 1, 1])
with cols[0]:
    app_filter = st.selectbox("App filter", ["(all)"] + app_names, index=0)
with cols[1]:
    op_filter = st.text_input("Operator filter (exact match)")
with cols[2]:
    limit = st.number_input("Limit", min_value=10, max_value=1000, value=100, step=50)
with cols[3]:
    if st.button("Refresh", use_container_width=True):
        st.rerun()

try:
    activity_rows = client().list_activity(
        app=(app_filter if app_filter != "(all)" else None),
        operator=(op_filter or None),
        limit=int(limit),
    )
except ControllerError as e:
    st.error(str(e))
    st.stop()

if not activity_rows:
    st.info("No activity matches these filters.")
    st.stop()

table_rows = [
    {
        "ts": r.get("ts"),
        "operator": r.get("operator"),
        "action": r.get("action"),
        "app": r.get("app_name") or "",
        "result": r.get("result"),
    }
    for r in activity_rows
]

selection = st.dataframe(
    table_rows,
    use_container_width=True,
    hide_index=True,
    selection_mode="single-row",
    on_select="rerun",
    column_config={
        "action": st.column_config.TextColumn("action", width="medium"),
        "result": st.column_config.TextColumn("result", width="large"),
    },
    key="activity-dataframe",
)

selected_rows = selection.selection.rows if selection and selection.selection else []
if selected_rows:
    row = activity_rows[selected_rows[0]]
    st.divider()
    st.subheader("Detail")
    c1, c2, c3 = st.columns(3)
    c1.metric("Operator", row.get("operator") or "—")
    c2.metric("Action", row.get("action") or "—")
    c3.metric("Result", row.get("result") or "—")
    st.write(f"Timestamp: `{row.get('ts')}`")
    st.write(f"App: `{row.get('app_name') or '—'}`")
    with st.expander("Raw detail", expanded=True):
        st.json(row.get("detail") or {})
