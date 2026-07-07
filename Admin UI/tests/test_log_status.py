from __future__ import annotations

import log_status as ls


class TestClassifyLogFetchFailure:
    def test_non_running_status_reports_restart_regardless_of_lines(self):
        # Direct evidence of a restart (status != RUNNING) wins even for a
        # small `lines` value that would otherwise look like a timeout.
        severity, message = ls.classify_log_fetch_failure("PENDING", 200)
        assert severity == "info"
        assert "restarting" in message
        assert "PENDING" in message

    def test_running_status_with_large_lines_reports_timeout_not_restart(self):
        # This is the reported bug: a confirmed-RUNNING service, large `lines`
        # (reproduced live at 1300+), must not claim the container is restarting.
        severity, message = ls.classify_log_fetch_failure("RUNNING", 1300)
        assert severity == "warning"
        assert "restarting" not in message
        assert "1300" in message

    def test_running_status_with_small_lines_is_a_generic_transient_message(self):
        severity, message = ls.classify_log_fetch_failure("RUNNING", 200)
        assert severity == "info"
        assert "restarting" not in message

    def test_unknown_status_with_large_lines_reports_timeout(self):
        # System logs (controller/admin-ui) have no status lookup, so status
        # is None; the `lines` heuristic is the only signal available.
        severity, message = ls.classify_log_fetch_failure(None, 1300)
        assert severity == "warning"
        assert "restarting" not in message

    def test_unknown_status_with_small_lines_is_generic_transient_message(self):
        severity, message = ls.classify_log_fetch_failure(None, 200)
        assert severity == "info"
        assert "restarting" not in message

    def test_threshold_boundary_is_exclusive(self):
        at_threshold = ls.classify_log_fetch_failure("RUNNING", ls.LARGE_LINES_THRESHOLD)
        above_threshold = ls.classify_log_fetch_failure("RUNNING", ls.LARGE_LINES_THRESHOLD + 1)
        assert at_threshold[0] == "info"
        assert above_threshold[0] == "warning"

    def test_severity_names_a_real_streamlit_call(self):
        # The Logs page does getattr(st, severity)(message); a typo here would
        # blow up at runtime instead of at import time, so pin the exact set.
        for status, lines in [("PENDING", 10), ("RUNNING", 5000), ("RUNNING", 10), (None, 5000)]:
            severity, _ = ls.classify_log_fetch_failure(status, lines)
            assert severity in {"info", "warning"}
