from __future__ import annotations

import spec_approval as sa


# The real column set returned by SHOW SPECIFICATIONS IN APPLICATION, verbatim
# from a live install: the approval-state column is "status" (not "state") and
# the identifier column "name" is upper-cased. The fixtures below mirror that so
# the parser is guarded against the exact shape it meets in production.
_SPEC_COLS = [
    "name", "requested_on", "type", "sequence_number", "status",
    "status_updated_on", "label", "description", "definition", "system_description",
]


def _spec_row(status: str, sequence: str = "1"):
    return (
        "CALLER_TOKEN_SPEC", "2026-07-06T03:20:33-07:00", "SETTING", sequence,
        status, None, "Extended caller token validity", "Keeps sessions valid.",
        '{"settingName":"SERVICE_CALLER_TOKEN_VALIDITY_SECS","value":"1800"}',
        "Controls the maximum validity period.",
    )


class TestGetCallerTokenSpecStatus:
    def test_pending_spec_parses(self, monkeypatch, fake_cursor, fake_conn):
        cur = fake_cursor([
            ("SHOW SPECIFICATIONS", _SPEC_COLS, [_spec_row("PENDING", "3")]),
        ])
        conn = fake_conn(cur)
        monkeypatch.setattr(sa, "open_caller_session", lambda: conn)
        status = sa.get_caller_token_spec_status("MYAPP")
        assert status.exists is True
        assert status.state == "PENDING"
        assert status.sequence_number == "3"
        assert conn.closed is True

    def test_approved_spec_parses(self, monkeypatch, fake_cursor, fake_conn):
        cur = fake_cursor([
            ("SHOW SPECIFICATIONS", _SPEC_COLS, [_spec_row("APPROVED")]),
        ])
        conn = fake_conn(cur)
        monkeypatch.setattr(sa, "open_caller_session", lambda: conn)
        status = sa.get_caller_token_spec_status("MYAPP")
        # The Setup page keys the "approved" banner off `"approv" in state.lower()`.
        assert status.state == "APPROVED"
        assert "approv" in status.state.lower()

    def test_no_caller_session(self, monkeypatch):
        monkeypatch.setattr(sa, "open_caller_session", lambda: None)
        status = sa.get_caller_token_spec_status("MYAPP")
        assert status.exists is False
        assert "caller session" in status.detail


class TestApproveCallerTokenSpec:
    def test_approve_success_closes_connection(self, monkeypatch, fake_cursor, fake_conn):
        cur = fake_cursor([("APPROVE SPECIFICATION", [], [])])
        conn = fake_conn(cur)
        monkeypatch.setattr(sa, "open_caller_session", lambda: conn)
        ok, message = sa.approve_caller_token_spec("MYAPP", "3")
        assert ok is True
        assert message == "approved"
        assert conn.closed is True

    def test_approve_raises_returns_failure_without_raising(self, monkeypatch, fake_conn):
        class RaisingCursor:
            description = []

            def execute(self, sql, *params):
                raise RuntimeError("insufficient privileges")

            def fetchall(self):
                return []

        conn = fake_conn(RaisingCursor())
        monkeypatch.setattr(sa, "open_caller_session", lambda: conn)
        ok, message = sa.approve_caller_token_spec("MYAPP", 3)
        assert ok is False
        assert "insufficient" in message
        assert conn.closed is True
