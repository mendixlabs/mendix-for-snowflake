"""Siemens iX-aligned theming for the admin UI.

The iX Classic *dark* palette is delivered through Streamlit's native theme
config (STREAMLIT_THEME_* env vars in the service spec, set by setup_script.sql),
pinned to `base=dark` because the app runs embedded behind SPCS and would
otherwise default to light. Native theming colors Streamlit's own widgets
correctly.

This module adds only what the native theme cannot: the Titillium Web font
(native `theme.font` takes no custom family) and the Siemens Deep Blue backdrop
behind the white-only logo / header bar.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

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

# Every `help=` kwarg on a widget renders a separately-focusable "(?)" icon
# (data-testid="stTooltipIcon") right next to the widget's label, so tabbing
# through a form with several help texts (e.g. the Register page) keeps
# landing on tooltip icons instead of the next field. This drops those icons
# out of the tab order without hiding them - hovering still shows the
# tooltip, keyboard Tab just skips past it. A MutationObserver keeps catching
# newly-rendered icons across reruns and page switches, so it only needs to
# be injected once.
_SKIP_TOOLTIP_TAB_STOPS_JS = """
<script>
  const doc = window.parent.document;
  // Natively-focusable tags need no explicit tabindex attribute (a plain
  // querySelector('[tabindex]') misses them), so match on tag too.
  const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]';
  // Guard every write with a check: setting tabIndex re-fires the attribute
  // mutation even when the value doesn't change, so an unconditional write
  // here would retrigger this same observer callback forever.
  const detab = () => {
    doc.querySelectorAll('[data-testid="stTooltipIcon"]').forEach((el) => {
      const targets = el.matches(FOCUSABLE) ? [el, ...el.querySelectorAll(FOCUSABLE)] : el.querySelectorAll(FOCUSABLE);
      targets.forEach((t) => { if (t.tabIndex !== -1) t.tabIndex = -1; });
    });
  };
  detab();
  if (!doc.__mendixTooltipObserver) {
    doc.__mendixTooltipObserver = new MutationObserver(detab);
    // attributes:true too - React re-applies its own tabIndex prop on
    // rerender without necessarily removing/reinserting the node, which a
    // childList-only observer would miss.
    doc.__mendixTooltipObserver.observe(doc.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['tabindex'],
    });
  }
</script>
"""


def apply_branding() -> None:
    """Inject Siemens iX styling and the persistent logo. Call once per page,
    after st.set_page_config. Idempotent within a rerun."""
    st.logo(_LOGO_PATH, size="large")
    st.markdown(_CSS, unsafe_allow_html=True)
    components.html(_SKIP_TOOLTIP_TAB_STOPS_JS, height=0)
