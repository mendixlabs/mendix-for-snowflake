"""Upload page: push a Mendix PAD to a registered app and trigger deploy."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from branding import apply_branding
from controller_client import ControllerError
from data import list_apps

st.set_page_config(page_title="Upload PAD", layout="centered")
apply_branding()
st.title("Upload PAD")

st.caption(
    "Pushes a PAD zip to the chosen app and triggers a deploy in one step. "
    "The browser upload size is capped at 1 GB; for larger PADs, fall back to "
    "`upload-pad.ps1`."
)

try:
    apps = list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

if not apps:
    st.info("No apps registered yet. Use the Register page to add one.")
    st.stop()

names = [a["name"] for a in apps]
selected = st.selectbox("App", names)

uploaded = st.file_uploader(
    "PAD zip",
    type=["zip"],
    help="Mendix Portable App Distribution (.zip)",
    accept_multiple_files=False,
)

if uploaded is not None:
    size_mb = uploaded.size / (1024 * 1024)
    st.write(f"Selected `{uploaded.name}` ({size_mb:.1f} MB)")

    if st.button("Upload and deploy", type="primary"):
        try:
            with st.spinner("Uploading PAD and triggering deploy..."):
                result = client().deploy_pad(selected, uploaded)
            st.success(
                f"Deploy accepted (status={result.get('status')}). "
                "The controller is processing the new PAD; the service will restart "
                "once the deploy completes."
            )
            st.page_link(
                "pages/1_Apps.py",
                label="Watch progress on the Apps page",
                icon=":material/arrow_forward:",
            )
        except ControllerError as e:
            st.error(str(e))
