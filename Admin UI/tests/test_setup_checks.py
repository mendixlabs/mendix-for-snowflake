from __future__ import annotations

import setup_checks as sc


class TestRows:
    def test_lowercases_columns_and_zips_rows(self):
        class Cur:
            description = [("NAME",), ("STATE",)]

            def fetchall(self):
                return [("pg1", "READY")]

        result = sc._rows(Cur())
        assert result == [{"name": "pg1", "state": "READY"}]


class TestFind:
    def test_first_matching_needle_wins(self):
        row = {"instance_state": "READY", "other": "x"}
        assert sc._find(row, "state", "status") == "READY"

    def test_none_when_absent(self):
        row = {"foo": "bar"}
        assert sc._find(row, "state", "status") is None


class TestCheckPgInstance:
    def test_no_rows_not_ok_step1_hint(self, fake_cursor):
        cur = fake_cursor([("SHOW POSTGRES INSTANCES", [], [])])
        result = sc._check_pg_instance(cur, "myinstance")
        assert result.ok is False
        assert "step 1" in result.detail

    def test_ready_state_mixed_case_ok(self, fake_cursor):
        cur = fake_cursor([("SHOW POSTGRES INSTANCES", ["name", "state"], [("myinstance", "Ready")])])
        result = sc._check_pg_instance(cur, "myinstance")
        assert result.ok is True

    def test_active_state_ok(self, fake_cursor):
        cur = fake_cursor([("SHOW POSTGRES INSTANCES", ["name", "state"], [("myinstance", "active")])])
        result = sc._check_pg_instance(cur, "myinstance")
        assert result.ok is True

    def test_other_state_not_ok_detail_includes_state(self, fake_cursor):
        cur = fake_cursor([("SHOW POSTGRES INSTANCES", ["name", "state"], [("myinstance", "SUSPENDED")])])
        result = sc._check_pg_instance(cur, "myinstance")
        assert result.ok is False
        assert "SUSPENDED" in result.detail


class TestCheckEai:
    def test_missing_not_ok(self, fake_cursor):
        cur = fake_cursor([("SHOW EXTERNAL ACCESS INTEGRATIONS", [], [])])
        result = sc._check_eai(cur, "myeai")
        assert result.ok is False

    def test_enabled_true_ok(self, fake_cursor):
        cur = fake_cursor([("SHOW EXTERNAL ACCESS INTEGRATIONS", ["name", "enabled"], [("myeai", "true")])])
        result = sc._check_eai(cur, "myeai")
        assert result.ok is True

    def test_enabled_column_absent_ok_present(self, fake_cursor):
        cur = fake_cursor([("SHOW EXTERNAL ACCESS INTEGRATIONS", ["name"], [("myeai",)])])
        result = sc._check_eai(cur, "myeai")
        assert result.ok is True
        assert "present" in result.detail


class TestCheckSecret:
    def test_three_part_fqn_emits_scoped_show(self, fake_cursor):
        cur = fake_cursor([
            (lambda sql: "SHOW SECRETS LIKE 'MYSECRET' IN SCHEMA DB.SCHEMA" == sql, ["name"], [("MYSECRET",)])
        ])
        result = sc._check_secret(cur, "DB.SCHEMA.MYSECRET")
        assert cur.executed[0] == "SHOW SECRETS LIKE 'MYSECRET' IN SCHEMA DB.SCHEMA"
        assert result.ok is True

    def test_one_part_fqn_no_scope_clause(self, fake_cursor):
        cur = fake_cursor([
            (lambda sql: sql == "SHOW SECRETS LIKE 'MYSECRET'", ["name"], [("MYSECRET",)])
        ])
        sc._check_secret(cur, "MYSECRET")
        assert cur.executed[0] == "SHOW SECRETS LIKE 'MYSECRET'"

    def test_found_ok_with_type_detail(self, fake_cursor):
        cur = fake_cursor([
            (lambda sql: True, ["name", "secret_type"], [("MYSECRET", "GENERIC_STRING")])
        ])
        result = sc._check_secret(cur, "MYSECRET")
        assert result.ok is True
        assert "GENERIC_STRING" in result.detail


class TestRunChecks:
    def test_no_caller_session_single_failing_result(self, monkeypatch):
        monkeypatch.setattr(sc, "open_caller_session", lambda: None)
        results = sc.run_checks("inst", "eai", "DB.SCHEMA.SECRET")
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].label == "Caller-rights session"

    def test_open_caller_session_raises_returns_failing_result_not_raise(self, monkeypatch):
        def _raise():
            raise RuntimeError("OAUTH_ACCESS_TOKEN_EXPIRED")

        monkeypatch.setattr(sc, "open_caller_session", _raise)
        results = sc.run_checks("inst", "eai", "DB.SCHEMA.SECRET")
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].label == "Caller-rights session"
        assert "OAUTH_ACCESS_TOKEN_EXPIRED" in results[0].detail
        assert "5b" in results[0].detail

    def test_happy_path_three_results_conn_closed(self, monkeypatch, fake_cursor, fake_conn):
        cur = fake_cursor([
            (lambda sql: "POSTGRES INSTANCES" in sql, ["name", "state"], [("inst", "READY")]),
            (lambda sql: "EXTERNAL ACCESS" in sql, ["name", "enabled"], [("eai", "true")]),
            (lambda sql: "SECRETS" in sql, ["name", "secret_type"], [("SECRET", "GENERIC_STRING")]),
        ])
        conn = fake_conn(cur)
        monkeypatch.setattr(sc, "open_caller_session", lambda: conn)
        results = sc.run_checks("inst", "eai", "DB.SCHEMA.SECRET")
        assert len(results) == 3
        assert all(r.ok for r in results)
        assert conn.closed is True

    def test_one_check_raising_others_unaffected(self, monkeypatch, fake_conn):
        class RaisingCursor:
            description = []

            def execute(self, sql, *params):
                raise RuntimeError("insufficient privileges")

            def fetchall(self):
                return []

        conn = fake_conn(RaisingCursor())
        monkeypatch.setattr(sc, "open_caller_session", lambda: conn)
        results = sc.run_checks("inst", "eai", "DB.SCHEMA.SECRET")
        assert len(results) == 3
        assert all(r.ok is False for r in results)
        assert all("check failed:" in r.detail for r in results)
        assert conn.closed is True
