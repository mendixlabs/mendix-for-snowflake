from __future__ import annotations

import pytest
from snowflake.connector import errors as sf_errors

from app import snowflake_client as sf


class TestAssertIdentifier:
    @pytest.mark.parametrize("fqn", ["A.B.C", "_X", "A$1", "TESTDB.PUBLIC.MYAPP_SERVICE"])
    def test_accepts_valid(self, fqn):
        sf._assert_identifier(fqn)  # must not raise

    @pytest.mark.parametrize("fqn", [
        "A'B", "A B", "A;B", "A-B", "1ABC", "", "A..B",
    ])
    def test_rejects_invalid(self, fqn):
        with pytest.raises(ValueError):
            sf._assert_identifier(fqn)


class TestCreateOrReplaceSecret:
    def test_escapes_single_quote(self, fake_execute_sql):
        sf.create_or_replace_secret("TESTDB.PUBLIC.MYSECRET", "a'b")
        sql, params = fake_execute_sql.calls[0]
        assert "SECRET_STRING = 'a''b'" in sql

    def test_invalid_fqn_raises_before_sql(self, fake_execute_sql):
        with pytest.raises(ValueError):
            sf.create_or_replace_secret("bad;fqn", "value")
        assert fake_execute_sql.calls == []


class TestDropSecret:
    def test_emits_drop_secret(self, fake_execute_sql):
        sf.drop_secret("TESTDB.PUBLIC.MYSECRET")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "DROP SECRET IF EXISTS TESTDB.PUBLIC.MYSECRET"

    def test_invalid_fqn_raises_with_no_sql(self, fake_execute_sql):
        with pytest.raises(ValueError):
            sf.drop_secret("bad;fqn")
        assert fake_execute_sql.calls == []


class TestCreateSchema:
    def test_emits_create_schema(self, fake_execute_sql):
        sf.create_schema("TESTDB.MXAPP_MYAPP")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "CREATE SCHEMA IF NOT EXISTS TESTDB.MXAPP_MYAPP"

    def test_invalid_fqn_raises(self, fake_execute_sql):
        with pytest.raises(ValueError):
            sf.create_schema("bad;fqn")
        assert fake_execute_sql.calls == []


class TestDropSchemaCascade:
    def test_emits_drop_schema_cascade_for_mxapp_schema(self, fake_execute_sql):
        sf.drop_schema_cascade("TESTDB.MXAPP_MYAPP")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "DROP SCHEMA IF EXISTS TESTDB.MXAPP_MYAPP CASCADE"

    def test_case_insensitive_prefix_check(self, fake_execute_sql):
        sf.drop_schema_cascade("TESTDB.mxapp_myapp")  # lowercase prefix still allowed
        assert len(fake_execute_sql.calls) == 1

    def test_refuses_non_per_app_schema(self, fake_execute_sql):
        # Safety interlock: must never be able to drop the shared app schema
        # (registry, deploy stage, services all live there).
        with pytest.raises(ValueError):
            sf.drop_schema_cascade("TESTDB.PUBLIC")
        assert fake_execute_sql.calls == []

    def test_refuses_app_public(self, fake_execute_sql):
        with pytest.raises(ValueError):
            sf.drop_schema_cascade("TESTDB.APP_PUBLIC")
        assert fake_execute_sql.calls == []


class TestIsRecoverable:
    def test_reauth_errno(self):
        exc = Exception("boom")
        exc.errno = 390114
        assert sf._is_recoverable(exc) is True

    def test_other_reauth_errnos(self):
        for errno in (390111, 390195):
            exc = Exception("boom")
            exc.errno = errno
            assert sf._is_recoverable(exc) is True

    def test_operational_error(self):
        assert sf._is_recoverable(sf_errors.OperationalError("x")) is True

    def test_interface_error(self):
        assert sf._is_recoverable(sf_errors.InterfaceError("x")) is True

    def test_token_expired_message_case_insensitive(self):
        assert sf._is_recoverable(Exception("Token has EXPIRED")) is True
        assert sf._is_recoverable(Exception("token HAS expired")) is True

    def test_generic_value_error_not_recoverable(self):
        assert sf._is_recoverable(ValueError("nope")) is False


class FakeCursor:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self._raise_exc is not None:
            exc = self._raise_exc
            self._raise_exc = None  # only raise once
            raise exc

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self, *_a, **_kw):
        return self._cursor

    def is_closed(self):
        return self.closed


class TestExecuteSqlRetry:
    def test_recoverable_error_reconnects_and_retries(self, monkeypatch):
        sf._conn = None
        recoverable = sf_errors.OperationalError("token has expired")
        cursor1 = FakeCursor(raise_exc=recoverable)
        cursor2 = FakeCursor(rows=[{"A": 1}])
        conns = [FakeConnection(cursor1), FakeConnection(cursor2)]
        calls = {"n": 0}

        def fake_get_connection():
            conn = conns[calls["n"]]
            calls["n"] += 1
            return conn

        monkeypatch.setattr(sf, "get_connection", fake_get_connection)
        result = sf.execute_sql("SELECT 1")
        assert result == [{"A": 1}]
        assert calls["n"] == 2

    def test_non_recoverable_reraises_no_retry(self, monkeypatch):
        sf._conn = None
        cursor1 = FakeCursor(raise_exc=ValueError("nope"))
        conn = FakeConnection(cursor1)
        calls = {"n": 0}

        def fake_get_connection():
            calls["n"] += 1
            return conn

        monkeypatch.setattr(sf, "get_connection", fake_get_connection)
        with pytest.raises(ValueError):
            sf.execute_sql("SELECT 1")
        assert calls["n"] == 1
        assert sf._conn is None

    def test_retry_itself_failing_propagates(self, monkeypatch):
        sf._conn = None
        recoverable = sf_errors.OperationalError("token has expired")
        cursor1 = FakeCursor(raise_exc=recoverable)
        cursor2 = FakeCursor(raise_exc=RuntimeError("second failure"))
        conns = [FakeConnection(cursor1), FakeConnection(cursor2)]
        calls = {"n": 0}

        def fake_get_connection():
            conn = conns[calls["n"]]
            calls["n"] += 1
            return conn

        monkeypatch.setattr(sf, "get_connection", fake_get_connection)
        with pytest.raises(RuntimeError):
            sf.execute_sql("SELECT 1")


class TestGetServiceEndpoint:
    def test_provisioning_message_skipped(self, fake_execute_sql):
        fake_execute_sql.returns = [[{"ingress_url": "Endpoints provisioning in progress. Please wait."}]]
        assert sf.get_service_endpoint("myapp_service") is None

    def test_bare_host_prefixed_https(self, fake_execute_sql):
        fake_execute_sql.returns = [[{"ingress_url": "abc.snowflakecomputing.app"}]]
        assert sf.get_service_endpoint("myapp_service") == "https://abc.snowflakecomputing.app"

    def test_already_https_passed_through(self, fake_execute_sql):
        fake_execute_sql.returns = [[{"ingress_url": "https://abc.snowflakecomputing.app"}]]
        assert sf.get_service_endpoint("myapp_service") == "https://abc.snowflakecomputing.app"


class TestGetServiceLogs:
    def test_uses_bound_params_not_interpolation(self, fake_execute_sql):
        fake_execute_sql.returns = [[{"LOGS": "log text"}]]
        result = sf.get_service_logs("MYAPP_SERVICE", container="mendix-app", lines=50)
        sql, params = fake_execute_sql.calls[0]
        assert "%s" in sql
        assert "MYAPP_SERVICE" not in sql
        assert params == ("TESTDB.PUBLIC.MYAPP_SERVICE", "mendix-app", 50)
        assert result == "log text"


class TestAlterComputePool:
    def test_only_provided_clauses_appear(self, fake_execute_sql):
        sf.alter_compute_pool("TEST_POOL", min_nodes=2)
        sql, params = fake_execute_sql.calls[0]
        assert "MIN_NODES = 2" in sql
        assert "MAX_NODES" not in sql
        assert "AUTO_SUSPEND_SECS" not in sql

    def test_values_int_coerced(self, fake_execute_sql):
        sf.alter_compute_pool("TEST_POOL", min_nodes="3", max_nodes="5")
        sql, params = fake_execute_sql.calls[0]
        assert "MIN_NODES = 3" in sql
        assert "MAX_NODES = 5" in sql

    def test_all_none_no_sql_executed(self, fake_execute_sql):
        sf.alter_compute_pool("TEST_POOL")
        assert fake_execute_sql.calls == []


class TestServiceLifecycleSql:
    def test_create_service_sql(self, fake_execute_sql):
        sf.create_service("MYAPP_SERVICE", "spec: yaml", "TEST_POOL", "TEST_EAI", "TEST_WH")
        sql, params = fake_execute_sql.calls[0]
        assert "CREATE SERVICE TESTDB.PUBLIC.MYAPP_SERVICE" in sql
        assert "IN COMPUTE POOL TEST_POOL" in sql
        assert "FROM SPECIFICATION $$spec: yaml$$" in sql
        assert "EXTERNAL_ACCESS_INTEGRATIONS = (TEST_EAI)" in sql
        assert "QUERY_WAREHOUSE = TEST_WH" in sql

    def test_create_service_grants_monitor_to_app_admin(self, fake_execute_sql):
        # Without this, get_service_logs() 403s for every caller: a freshly
        # created service is owned by the application, not app_admin.
        sf.create_service("MYAPP_SERVICE", "spec: yaml", "TEST_POOL", "TEST_EAI", "TEST_WH")
        sql, params = fake_execute_sql.calls[1]
        assert sql == "GRANT MONITOR ON SERVICE TESTDB.PUBLIC.MYAPP_SERVICE TO APPLICATION ROLE app_admin"

    def test_alter_service_spec_sql(self, fake_execute_sql):
        sf.alter_service_spec("MYAPP_SERVICE", "spec: yaml")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "ALTER SERVICE TESTDB.PUBLIC.MYAPP_SERVICE FROM SPECIFICATION $$spec: yaml$$"

    def test_suspend_service_sql(self, fake_execute_sql):
        sf.suspend_service("MYAPP_SERVICE")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "ALTER SERVICE TESTDB.PUBLIC.MYAPP_SERVICE SUSPEND"

    def test_resume_service_sql(self, fake_execute_sql):
        sf.resume_service("MYAPP_SERVICE")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "ALTER SERVICE TESTDB.PUBLIC.MYAPP_SERVICE RESUME"

    def test_drop_service_sql(self, fake_execute_sql):
        sf.drop_service("MYAPP_SERVICE")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "DROP SERVICE IF EXISTS TESTDB.PUBLIC.MYAPP_SERVICE"

    def test_create_app_access_role_sql(self, fake_execute_sql):
        sf.create_app_access_role("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "CREATE APPLICATION ROLE IF NOT EXISTS app_myapp_user"

    def test_grant_endpoint_to_app_role_sql(self, fake_execute_sql):
        sf.grant_endpoint_to_app_role("MYAPP_SERVICE", "app_myapp_user")
        sql, params = fake_execute_sql.calls[0]
        assert "GRANT SERVICE ROLE TESTDB.PUBLIC.MYAPP_SERVICE!ALL_ENDPOINTS_USAGE" in sql
        assert "TO APPLICATION ROLE app_myapp_user" in sql

    def test_drop_app_access_role_sql(self, fake_execute_sql):
        sf.drop_app_access_role("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert sql == "DROP APPLICATION ROLE IF EXISTS app_myapp_user"

    def test_put_file_sql(self, fake_execute_sql):
        sf.put_file("/tmp/x.zip", "@TESTDB.PUBLIC.MENDIX_DEPLOY_STAGE/apps/myapp/")
        sql, params = fake_execute_sql.calls[0]
        assert "PUT file:///tmp/x.zip @TESTDB.PUBLIC.MENDIX_DEPLOY_STAGE/apps/myapp/" in sql

    def test_get_compute_pool_none_when_not_found(self, fake_execute_sql):
        fake_execute_sql.returns = [[]]
        assert sf.get_compute_pool("TEST_POOL") is None

    def test_get_compute_pool_shape(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "name": "TEST_POOL", "state": "ACTIVE", "min_nodes": 1, "max_nodes": 2,
            "instance_family": "CPU_X64_XS", "auto_suspend_secs": 600, "num_services": 1,
        }]]
        pool = sf.get_compute_pool("TEST_POOL")
        assert pool == {
            "name": "TEST_POOL", "state": "ACTIVE", "min_nodes": 1, "max_nodes": 2,
            "instance_family": "CPU_X64_XS", "auto_suspend_secs": 600, "num_services": 1,
        }


class TestMisc:
    def test_app_access_role_name_lowercases(self):
        assert sf.app_access_role_name("MyApp") == "app_myapp_user"

    def test_show_all_service_statuses_swallows_exceptions(self, monkeypatch):
        def raiser(sql, params=()):
            raise RuntimeError("boom")

        monkeypatch.setattr(sf, "execute_sql", raiser)
        assert sf.show_all_service_statuses() == {}

    def test_show_service_status_swallows_exceptions(self, monkeypatch):
        def raiser(sql, params=()):
            raise RuntimeError("boom")

        monkeypatch.setattr(sf, "execute_sql", raiser)
        assert sf.show_service_status("myapp_service") is None
