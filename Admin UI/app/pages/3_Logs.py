"""Logs page: tail the service logs of a selected app."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st
import streamlit.components.v1 as components

from auth import client, is_privileged_operator
from branding import apply_branding
from controller_client import ControllerError
from data import list_apps
from log_status import LARGE_LINES_THRESHOLD, classify_log_fetch_failure

st.set_page_config(page_title="Logs", layout="wide")
apply_branding()
st.title("Service logs")

# Infrastructure services, shown only to privileged operators. Each entry is
# (display label, kind, key) where kind routes to the right controller call.
_SYSTEM_SOURCES = [
    ("🛠 Controller (system)", "system", "controller"),
    ("🛠 Admin UI (system)", "system", "admin-ui"),
]

try:
    apps = list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

sources = (_SYSTEM_SOURCES if is_privileged_operator() else []) + [
    (a["name"], "app", a["name"]) for a in apps
]

if not sources:
    st.info("No apps registered yet.")
    st.stop()

labels = [s[0] for s in sources]
selected_label = st.selectbox("Source", labels)
selected_kind, selected_key = next((s[1], s[2]) for s in sources if s[0] == selected_label)
selected = selected_label

cols = st.columns([1, 1, 4])
with cols[0]:
    lines = st.number_input(
        "Lines",
        min_value=10,
        max_value=2000,
        value=200,
        step=50,
        help=(
            f"Values above {LARGE_LINES_THRESHOLD} can take longer to fetch than the "
            "request timeout window allows; the app keeps running even if the fetch fails."
        ),
    )
    if lines > LARGE_LINES_THRESHOLD:
        st.caption(f"⚠️ {int(lines)} lines may be slow to fetch and time out.")
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
      const snap = () => {{
        const wraps = doc.querySelectorAll('[data-testid="stVerticalBlockBorderWrapper"]');
        if (!wraps.length) return;
        // The fixed-height container scrolls on the wrapper or an inner element;
        // find the last actually-scrollable node so scrollTop lands.
        let target = null;
        for (let i = wraps.length - 1; i >= 0 && !target; i--) {{
          const w = wraps[i];
          if (w.scrollHeight > w.clientHeight + 4) target = w;
          else target = [...w.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 4);
        }}
        if (!target) target = wraps[wraps.length - 1];
        const isFirst = {first_js};
        const atBottom = (target.scrollHeight - target.scrollTop - target.clientHeight) < 40;
        if (isFirst || atBottom) target.scrollTop = target.scrollHeight;
      }};
      // Defer past layout + syntax highlighting so scrollHeight is final.
      requestAnimationFrame(() => requestAnimationFrame(snap));
    </script>
    """


def _selected_service_status() -> str | None:
    """Live status of the selected app, or None when it isn't known.

    Re-fetches from the controller rather than reading the module-level `apps`
    list, which is captured once at page load. An auto-refresh tick reruns only
    this fragment, not the enclosing script, so `apps` would be stale and a
    restart that began after the page loaded would be misclassified. Called only
    on the 502 failure path, so it adds a round trip only when a fetch has
    already failed. System sources have no status lookup here, so this is None
    for those.
    """
    if selected_kind != "app":
        return None
    try:
        current = list_apps()
    except ControllerError:
        return None
    return next((a.get("service_status") for a in current if a["name"] == selected_key), None)


@st.fragment(run_every=10 if auto else None)
def _log_view() -> None:
    try:
        if selected_kind == "system":
            logs = client().get_system_logs(selected_key, lines=int(lines))
        else:
            logs = client().get_logs(selected_key, lines=int(lines))
    except ControllerError as e:
        if e.status_code == 502:
            severity, message = classify_log_fetch_failure(_selected_service_status(), int(lines))
            getattr(st, severity)(message)
        else:
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
