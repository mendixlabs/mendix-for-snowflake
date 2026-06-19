"""Logs page: tail the service logs of a selected app."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st
import streamlit.components.v1 as components

from auth import client
from branding import apply_branding
from controller_client import ControllerError
from data import list_apps

st.set_page_config(page_title="Logs", layout="wide")
apply_branding()
st.title("Service logs")

try:
    apps = list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

if not apps:
    st.info("No apps registered yet.")
    st.stop()

names = [a["name"] for a in apps]
selected = st.selectbox("App", names)

cols = st.columns([1, 1, 4])
with cols[0]:
    lines = st.number_input("Lines", min_value=10, max_value=2000, value=200, step=50)
with cols[1]:
    auto = st.toggle("Auto-refresh", value=False, help="Refresh every 10 seconds.")

_LOG_HEIGHT = 600
_SCROLL_INIT_KEY = f"logs-scrolled-init::{selected}"


def _scroll_script(is_first_render: bool) -> str:
    """Snap a Streamlit bordered container to the bottom.

    On first render for an app, always snap so the newest line is visible.
    On subsequent renders (refresh / auto-refresh ticks), snap only if the
    user was already pinned to the bottom — chat-client tail behavior.
    """
    first_js = "true" if is_first_render else "false"
    return f"""
    <script>
      const doc = window.parent.document;
      const containers = doc.querySelectorAll('[data-testid="stVerticalBlockBorderWrapper"]');
      if (containers.length) {{
        const target = containers[containers.length - 1];
        const isFirst = {first_js};
        const atBottom = (target.scrollHeight - target.scrollTop - target.clientHeight) < 8;
        if (isFirst || atBottom) {{
          target.scrollTop = target.scrollHeight;
        }}
      }}
    </script>
    """


@st.fragment(run_every=10 if auto else None)
def _log_view() -> None:
    try:
        logs = client().get_logs(selected, lines=int(lines))
    except ControllerError as e:
        st.error(str(e))
        return

    with st.container(height=_LOG_HEIGHT, border=True):
        if not logs:
            st.info("No log output (service may not be running yet).")
        else:
            st.code(logs, language="log")

    is_first_render = _SCROLL_INIT_KEY not in st.session_state
    st.session_state[_SCROLL_INIT_KEY] = True
    components.html(_scroll_script(is_first_render), height=0)


_log_view()
