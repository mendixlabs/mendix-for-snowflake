"""Siemens iX-aligned theming for the admin UI.

Streamlit's `[theme]` config is a single palette, so per-scheme (light/dark)
colors are delivered here via CSS `@media (prefers-color-scheme)`, using the
official iX v5 Classic theme tokens. The persistent Siemens logo is white-only,
so its container gets a dark backdrop in both schemes to stay legible on the
light background. See PLAN-2-siemens-ix-styling.md.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

_LOGO_PATH = str((Path(__file__).parent / "assets" / "siemens-logo-white.svg").resolve())

# iX v5 Classic tokens (from @siemens/ix theme/classic-{light,dark}.css).
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Titillium+Web:wght@400;600;700&display=swap');

:root {
  /* iX v5 Classic light (default) */
  --ix-color-1: #ffffff;
  --ix-color-2: #eff0f1;
  --ix-color-primary: #006e93;
  --ix-color-text: rgba(0, 10, 20, 0.9);
  --ix-logo-backdrop: #000028;
}
@media (prefers-color-scheme: dark) {
  :root {
    /* iX v5 Classic dark */
    --ix-color-1: #0f1619;
    --ix-color-2: #283236;
    --ix-color-primary: #00bde3;
    --ix-color-text: rgba(245, 252, 255, 0.9);
  }
}

html, body, [data-testid="stAppViewContainer"], .stApp,
[data-testid="stSidebar"], button, input, textarea, select {
  font-family: "Titillium Web", "Siemens Sans", system-ui, -apple-system, sans-serif;
}

[data-testid="stAppViewContainer"], .stApp {
  background-color: var(--ix-color-1);
  color: var(--ix-color-text);
}
[data-testid="stSidebar"] { background-color: var(--ix-color-2); }
[data-testid="stSidebar"] a { color: var(--ix-color-primary); }

/* The logo SVG is white-only with a transparent background. Give the rendered
   image a dark box (via background + padding) so the mark is legible in BOTH
   light and dark mode. Targeting the image directly is robust across the
   header and sidebar placements regardless of container test-ids. */
[data-testid="stLogo"] {
  background-color: var(--ix-logo-backdrop);
  padding: 0.35rem 0.6rem;
  border-radius: 4px;
  height: 2.6rem;
  box-sizing: content-box;
}
[data-testid="stHeader"], [data-testid="stSidebarHeader"] {
  background-color: var(--ix-logo-backdrop);
}

/* Keep focus rings visible for accessibility (do not strip outlines). */
:focus-visible { outline: 2px solid var(--ix-color-primary); outline-offset: 2px; }
</style>
"""


def apply_branding() -> None:
    """Inject Siemens iX styling and the persistent logo. Call once per page,
    after st.set_page_config. Idempotent within a rerun."""
    st.logo(_LOGO_PATH, size="large")
    st.markdown(_CSS, unsafe_allow_html=True)
