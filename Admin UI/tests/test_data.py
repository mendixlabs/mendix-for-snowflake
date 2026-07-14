from __future__ import annotations

import data as data_module
from controller_client import ControllerError
from data import pad_filename


class TestPadFilename:
    def test_extracts_basename_from_staged_path(self):
        # O8: the controller keeps the operator's own filename (no current.zip
        # rename), so the Apps page needs the basename to show what's deployed.
        assert pad_filename("apps/myapp/MyReleasePad_20260707.zip") == "MyReleasePad_20260707.zip"

    def test_legacy_current_zip_path_still_extracts(self):
        # Backward compat: an app that was last deployed before O8 still has
        # "current.zip" recorded as its pad_stage_path until its next redeploy.
        assert pad_filename("apps/myapp/current.zip") == "current.zip"

    def test_none_returns_empty_string_not_none(self):
        # No PAD ever deployed - callers display this directly, so it must be a
        # displayable "" rather than None or a raised error.
        assert pad_filename(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert pad_filename("") == ""


class TestEgressWarning:
    """Covers data.egress_warning's own logic (the client call + ControllerError
    fallback) - the caching decorator itself is exercised incidentally, not the
    point of these tests, so each test uses its own operator/roles key to avoid
    collisions with whatever another test happened to cache."""

    def test_returns_client_result(self, monkeypatch):
        class FakeClient:
            def get_egress_warning(self):
                return {"warn": True, "days_remaining": 5}

        monkeypatch.setattr(data_module, "client", lambda: FakeClient())
        monkeypatch.setattr(data_module, "current_operator", lambda: "egress-test-1")
        monkeypatch.setattr(data_module, "operator_roles", lambda: ("ROLE_A",))
        assert data_module.egress_warning() == {"warn": True, "days_remaining": 5}

    def test_controller_error_degrades_to_no_warning(self, monkeypatch):
        class FakeClient:
            def get_egress_warning(self):
                raise ControllerError(503, "controller down")

        monkeypatch.setattr(data_module, "client", lambda: FakeClient())
        monkeypatch.setattr(data_module, "current_operator", lambda: "egress-test-2")
        monkeypatch.setattr(data_module, "operator_roles", lambda: ("ROLE_A",))
        assert data_module.egress_warning() == {"warn": False, "days_remaining": None}
