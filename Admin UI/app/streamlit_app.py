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

with st.sidebar:
    st.markdown(f"**Operator**\n\n`{operator}`")
    st.markdown(f"**Controller**\n\n`{controller_url()}`")
    st.divider()
    st.caption("Pages")
    st.page_link("pages/1_Apps.py", label="Apps", icon=":material/apps:")
    st.page_link("pages/2_Register.py", label="Register", icon=":material/add:")
    st.page_link("pages/3_Logs.py", label="Logs", icon=":material/article:")
    st.page_link("pages/4_Upload.py", label="Upload PAD", icon=":material/upload_file:")
    st.page_link("pages/5_Activity.py", label="Activity", icon=":material/history:")

st.title("Mendix Deployment Admin")
st.write(
    "Manage Mendix apps deployed on Snowpark Container Services. "
    "Use the sidebar to navigate."
)
st.page_link("pages/1_Apps.py", label="Go to Apps", icon=":material/arrow_forward:")
