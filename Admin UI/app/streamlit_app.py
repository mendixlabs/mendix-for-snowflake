"""Admin UI for the Mendix SPCS deployment controller."""
from __future__ import annotations

import streamlit as st

from auth import controller_url, current_operator
from branding import apply_branding

st.set_page_config(
    page_title="Mendix Deployment Admin",
    page_icon=":satellite:",
    layout="wide",
)
apply_branding()

operator = current_operator()

# This block, plus apply_branding() above, is emitted here - before
# st.navigation()/pg.run() below - specifically so it renders on EVERY page,
# not just this one. st.navigation() takes over the sidebar's page-nav area
# entirely (replacing Streamlit's bare auto-generated nav that otherwise shows
# on every page under the old pages/-folder auto-discovery, complete with a
# "streamlit app" entry for this file); this custom identity block stays
# above that nav in the sidebar because it's written first.
with st.sidebar:
    st.markdown(f"**Operator**\n\n`{operator}`")
    st.markdown(f"**Controller**\n\n`{controller_url()}`")
    st.divider()


def _home() -> None:
    st.title("Mendix Deployment Admin")
    st.write(
        "Manage Mendix apps deployed on Snowpark Container Services. "
        "Use the sidebar to navigate."
    )
    st.page_link("pages/1_Apps.py", label="Go to Apps", icon=":material/arrow_forward:")


pages = [
    st.Page(_home, title="Home", icon=":material/home:", default=True),
    st.Page("pages/1_Apps.py", title="Apps", icon=":material/apps:"),
    st.Page("pages/2_Register.py", title="Register", icon=":material/add:"),
    st.Page("pages/3_Logs.py", title="Logs", icon=":material/article:"),
    st.Page("pages/4_Upload.py", title="Upload PAD", icon=":material/upload_file:"),
    st.Page("pages/5_Activity.py", title="Activity", icon=":material/history:"),
    st.Page("pages/6_Setup.py", title="Setup / Verify", icon=":material/settings:"),
    st.Page("pages/7_Infrastructure.py", title="Infrastructure", icon=":material/dns:"),
]

pg = st.navigation(pages)
pg.run()
