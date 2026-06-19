"""Siemens iX-aligned theming for the admin UI.

The iX Classic *dark* palette is delivered through Streamlit's native theme
config (STREAMLIT_THEME_* env vars in setup.ps1 / update.ps1), pinned to
`base=dark` because the app runs embedded behind SPCS and would otherwise
default to light. Native theming colors Streamlit's own widgets correctly.

This module adds only what the native theme cannot: the Titillium Web font
(native `theme.font` takes no custom family) and the Siemens Deep Blue backdrop
behind the white-only logo / header bar. See PLAN-2-siemens-ix-styling.md.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

_LOGO_PATH = str((Path(__file__).parent / "assets" / "siemens-logo-white.svg").resolve())

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Titillium+Web:wght@400;600;700&display=swap');

:root {
  --siemens-deep-blue: #000028;  /* logo backdrop + header accent */
  --ix-primary: #00bde3;         /* iX Classic dark primary (focus ring) */
}

/* Titillium Web everywhere (native theme.font accepts no custom family). */
html, body, [data-testid="stAppViewContainer"], .stApp,
[data-testid="stSidebar"], button, input, textarea, select {
  font-family: "Titillium Web", "Siemens Sans", system-ui, -apple-system, sans-serif;
}

/* Siemens Deep Blue header bar + logo backdrop. The logo SVG is white-only with
   a transparent background, so the dark box keeps the mark legible. */
[data-testid="stHeader"], [data-testid="stSidebarHeader"] {
  background-color: var(--siemens-deep-blue);
}
[data-testid="stLogo"] {
  background-color: var(--siemens-deep-blue);
  padding: 0.35rem 0.6rem;
  border-radius: 4px;
  height: 2.6rem;
  box-sizing: content-box;
}

/* Keep focus rings visible for accessibility (do not strip outlines). */
:focus-visible { outline: 2px solid var(--ix-primary); outline-offset: 2px; }
</style>
"""


def apply_branding() -> None:
    """Inject Siemens iX styling and the persistent logo. Call once per page,
    after st.set_page_config. Idempotent within a rerun."""
    st.logo(_LOGO_PATH, size="large")
    st.markdown(_CSS, unsafe_allow_html=True)
