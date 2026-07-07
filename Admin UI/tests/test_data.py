from __future__ import annotations

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
