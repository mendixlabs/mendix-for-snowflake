from __future__ import annotations

import streamlit as st

import branding


class TestApplyBranding:
    def test_tooltip_script_html_differs_every_call(self, monkeypatch):
        """st.iframe reuses its iframe DOM node across reruns when the HTML
        argument is unchanged, which silently stops the tooltip-tab-order
        script from ever running again if a single mount is lost (dropped
        WebSocket during a cold start, a redeploy mid-session). Each call must
        pass different HTML so the frontend always treats it as a fresh
        element and retries the mount."""
        st.session_state.clear()
        monkeypatch.setattr(branding.st, "logo", lambda *a, **k: None)
        monkeypatch.setattr(branding.st, "markdown", lambda *a, **k: None)
        seen = []
        monkeypatch.setattr(branding.st, "iframe", lambda html, **k: seen.append(html))

        branding.apply_branding()
        branding.apply_branding()
        branding.apply_branding()

        assert len(seen) == 3
        assert len(set(seen)) == 3, "each call must render distinct HTML to force a remount"
        for html in seen:
            assert "mendixTooltipObserver" in html

    def test_rerun_counter_persists_across_script_reruns(self, monkeypatch):
        """The counter lives in session_state, not a module global, so it
        survives across the full-script reruns Streamlit does on every
        interaction (a fresh Python import would reset a module-level
        counter, defeating the point)."""
        st.session_state.clear()
        monkeypatch.setattr(branding.st, "logo", lambda *a, **k: None)
        monkeypatch.setattr(branding.st, "markdown", lambda *a, **k: None)
        monkeypatch.setattr(branding.st, "iframe", lambda html, **k: None)

        branding.apply_branding()
        first = st.session_state["_tooltip_fix_rerun"]
        branding.apply_branding()
        second = st.session_state["_tooltip_fix_rerun"]

        assert second == first + 1
