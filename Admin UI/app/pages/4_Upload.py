"""Upload PAD page: stage a Mendix PAD and trigger a deploy.

Browser uploads can't carry production PADs: a Mendix PAD bundles the runtime
and routinely runs to hundreds of MB, which exceeds what the SPCS ingress accepts
on a single upload request (the upload fails with a network error). The supported
path is to copy the PAD straight to the app's deploy stage with the Snow CLI, then
trigger the deploy. This page generates the exact command and offers a trigger
button. The controller deploys whatever .zip is staged under apps/<name>/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from controller_client import ControllerError
from data import list_apps

# apply_branding() runs once in streamlit_app.py, before st.navigation()/pg.run(),
# so it (and the persistent sidebar it builds) applies to every page already.
st.set_page_config(page_title="Upload PAD", layout="centered")
st.title("Upload PAD")

st.caption(
    "Mendix PADs are large (hundreds of MB), so they cannot be uploaded through the "
    "browser. Copy the PAD to the app's deploy stage with the Snow CLI, then trigger "
    "the deploy below or from the Apps page."
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
selected = st.selectbox(
    "App",
    names,
    help="PAD = Portable Application Deployment Archive, Mendix's exported "
         "deployment package format. Pick which app receives the staged PAD.",
)

# The deploy stage FQN is injected into the container by the setup script
# (it depends on the consumer's application database name). Fall back to a
# readable placeholder if the env var is absent.
deploy_stage = os.environ.get("DEPLOY_STAGE", "@<app_db>.APP_PUBLIC.MENDIX_DEPLOY_STAGE")
stage_dir = f"{deploy_stage}/apps/{selected}/"

st.subheader("1. Copy the PAD to the deploy stage")
st.caption(
    "Run this from your workstation with the Snow CLI configured. Requires the "
    "`app_admin` application role (granted to operators at install). Replace the PAD "
    "path and your connection name; the file may keep its own name."
)
st.code(
    f'snow stage copy "C:\\path\\to\\your-app.zip" {stage_dir} '
    f'--connection <your-connection> --overwrite',
    language="bash",
)
st.caption(
    f"Uploads into `apps/{selected}/` on the app's deploy stage. The controller "
    "deploys the newest `.zip` it finds there, so no specific filename is required."
)

st.subheader("2. Trigger the deploy")
st.caption(
    "Once the copy completes, deploy the staged PAD. You can also use the Redeploy "
    "button on the Apps page."
)
if st.button("Trigger deploy", type="primary"):
    try:
        with st.spinner("Triggering deploy from the staged PAD..."):
            result = client().trigger_deploy(selected)
        st.success(
            f"Deploy accepted (status={result.get('status')}). The controller is "
            "deploying the staged PAD; the service restarts when it completes."
        )
        st.page_link(
            "pages/1_Apps.py",
            label="Watch progress on the Apps page",
            icon=":material/arrow_forward:",
        )
    except ControllerError as e:
        missing = e.missing_constants()
        if e.status_code == 422 and missing:
            st.warning(
                "This PAD requires values for these constants:\n"
                + "\n".join(f"- `{m}`" for m in missing)
                + "\n\nSet them on the **Apps** page (select the app -> Constants -> Save), "
                "then deploy again. The Apps page prefills them for you."
            )
            st.page_link(
                "pages/1_Apps.py",
                label="Go to Apps to set constants",
                icon=":material/arrow_forward:",
            )
        else:
            st.error(str(e))
