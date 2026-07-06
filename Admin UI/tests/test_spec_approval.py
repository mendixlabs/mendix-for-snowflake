from __future__ import annotations

import spec_approval as sa


class TestGetCallerTokenSpecStatus:
    def test_pending_spec_parses(self, monkeypatch, fake_cursor, fake_conn):
        cur = fake_cursor([
            (
                "SHOW SPECIFICATIONS",
                ["name", "state", "sequence_number"],
                [("caller_token_spec", "PENDING", "3")],
            ),
        ])
        conn = fake_conn(cur)
        monkeypatch.setattr(sa, "open_caller_session", lambda: conn)
        status = sa.get_caller_token_spec_status("MYAPP")
        assert status.exists is True
        assert status.state == "PENDING"
        assert status.sequence_number == "3"
        assert conn.closed is True

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
