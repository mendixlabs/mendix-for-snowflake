from __future__ import annotations

import pytest

from app import pg_admin


# ---------------------------------------------------------------------------
# Fakes: record every statement psycopg.connect()'s cursor executes, mirroring
# the style of FakeCursor/FakeConnection in test_snowflake_client.py. Composed
# sql.SQL/sql.Identifier/sql.Literal objects are rendered via .as_string(None)
# so tests can assert on the final, safely-quoted SQL text.
# ---------------------------------------------------------------------------

def _render(query) -> str:
    if hasattr(query, "as_string"):
        return query.as_string(None)
    return query


class FakeCursor:
    def __init__(self, recorder: "Recorder"):
        self._recorder = recorder

    def execute(self, query, params=None):
        text = _render(query)
        self._recorder.calls.append((text, params))
        if self._recorder.raise_on is not None and self._recorder.raise_on(text):
            raise RuntimeError(f"boom: {text}")

    def fetchone(self):
        if self._recorder.fetchone_values:
            return self._recorder.fetchone_values.pop(0)
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, recorder: "Recorder", cursor: FakeCursor):
        self._cursor = cursor
        self._recorder = recorder

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._recorder.closed_count += 1
        return False


class Recorder:
    """Records every psycopg.connect() call and every statement its cursor
    executes (across however many connections a call under test opens), plus
    a scriptable fetchone() queue and an optional per-statement failure hook."""

    def __init__(self, fetchone_values=None, raise_on=None):
        self.calls: list[tuple[str, object]] = []
        self.connect_kwargs: list[dict] = []
        self.closed_count = 0
        self.fetchone_values = list(fetchone_values or [])
        self.raise_on = raise_on
        self._cursor = FakeCursor(self)

    def connect(self, **kwargs):
        self.connect_kwargs.append(kwargs)
        return FakeConnection(self, self._cursor)

    def texts(self) -> list[str]:
        return [text for text, _params in self.calls]


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()

    def _install(fetchone_values=None, raise_on=None):
        rec.fetchone_values = list(fetchone_values or [])
        rec.raise_on = raise_on
        return rec

    monkeypatch.setattr(pg_admin.psycopg, "connect", rec.connect)
    return _install, rec


# ---------------------------------------------------------------------------
# provision_app
# ---------------------------------------------------------------------------

class TestProvisionAppCreatePath:
    """Role and database both absent: full CREATE path."""

    def test_issues_expected_statements_in_order(self, recorder):
        install, rec = recorder
        install(fetchone_values=[None, None])  # role absent, db absent

        password = pg_admin.provision_app(
            "pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role"
        )

        texts = rec.texts()
        assert any(t.startswith("CREATE ROLE ") and "myapp_role" in t for t in texts)
        assert any("LOGIN PASSWORD" in t for t in texts)
        assert any(t.startswith("CREATE DATABASE ") and "myapp_db" in t and "application" in t for t in texts)
        assert any(t == 'REVOKE CONNECT ON DATABASE "myapp_db" FROM PUBLIC' for t in texts)
        assert any(t == 'GRANT CONNECT ON DATABASE "myapp_db" TO "myapp_role"' for t in texts)
        assert any(t == 'GRANT ALL ON SCHEMA public TO "myapp_role"' for t in texts)
        # Ownership of the public schema is intentionally NOT reassigned to the
        # app role: `application` isn't a member of it, so ALTER SCHEMA OWNER
        # would fail, and CREATE + USAGE from GRANT ALL already suffices.
        assert not any(t.startswith("ALTER SCHEMA") for t in texts)

        # ordering: role/db setup before the isolation revoke/grant, and the
        # maintenance-db work happens before the app-db schema grants.
        create_role_idx = next(i for i, t in enumerate(texts) if t.startswith("CREATE ROLE"))
        create_db_idx = next(i for i, t in enumerate(texts) if t.startswith("CREATE DATABASE"))
        revoke_idx = next(i for i, t in enumerate(texts) if t.startswith("REVOKE CONNECT"))
        grant_connect_idx = next(i for i, t in enumerate(texts) if t.startswith("GRANT CONNECT"))
        schema_grant_idx = next(i for i, t in enumerate(texts) if t.startswith("GRANT ALL ON SCHEMA"))
        assert create_role_idx < create_db_idx < revoke_idx < grant_connect_idx < schema_grant_idx

        assert isinstance(password, str) and len(password) > 0

    def test_connects_as_bootstrap_application_role_with_ssl(self, recorder):
        install, rec = recorder
        install(fetchone_values=[None, None])

        pg_admin.provision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")

        assert len(rec.connect_kwargs) == 2  # maintenance db, then the app db
        first, second = rec.connect_kwargs
        assert first["user"] == "application"
        assert first["password"] == "bootstrap-pw"
        assert first["dbname"] == "postgres"
        assert first["sslmode"] == "require"
        assert first["autocommit"] is True
        assert first["host"] == "pg.test.local"
        assert first["port"] == 5432
        assert second["dbname"] == "myapp_db"

    def test_default_port_when_absent(self, recorder):
        install, rec = recorder
        install(fetchone_values=[None, None])

        pg_admin.provision_app("pg.test.local", "bootstrap-pw", "myapp_db", "myapp_role")

        assert rec.connect_kwargs[0]["host"] == "pg.test.local"
        assert rec.connect_kwargs[0]["port"] == 5432


class TestProvisionAppAlterPath:
    """Role and database both already exist (e.g. a redeploy): ALTER, no
    CREATE DATABASE, but isolation grants are still re-asserted."""

    def test_alters_existing_role_and_skips_create_database(self, recorder):
        install, rec = recorder
        install(fetchone_values=[("exists",), ("exists",)])  # role exists, db exists

        pg_admin.provision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")

        texts = rec.texts()
        assert any(t.startswith("ALTER ROLE ") and "myapp_role" in t for t in texts)
        assert not any(t.startswith("CREATE ROLE") for t in texts)
        assert not any(t.startswith("CREATE DATABASE") for t in texts)
        # isolation boundary is still re-asserted even when nothing was created
        assert any(t.startswith("REVOKE CONNECT") for t in texts)
        assert any(t.startswith("GRANT CONNECT") for t in texts)


class TestProvisionAppPasswordRandomness:
    def test_password_differs_across_calls(self, recorder):
        install, rec = recorder
        install(fetchone_values=[None, None])
        pw1 = pg_admin.provision_app("pg.test.local:5432", "bootstrap-pw", "db1", "role1")

        install(fetchone_values=[None, None])
        pw2 = pg_admin.provision_app("pg.test.local:5432", "bootstrap-pw", "db2", "role2")

        assert pw1 != pw2
        assert len(pw1) > 20
        assert len(pw2) > 20


class TestProvisionAppIdentifierSafety:
    def test_malicious_names_are_quoted_not_interpolated(self, recorder):
        install, rec = recorder
        install(fetchone_values=[None, None])
        evil_role = 'x"; DROP TABLE foo; --'
        evil_db = 'y"; DROP DATABASE prod; --'

        pg_admin.provision_app("pg.test.local:5432", "bootstrap-pw", evil_db, evil_role)

        texts = rec.texts()
        create_role = next(t for t in texts if t.startswith("CREATE ROLE"))
        create_db = next(t for t in texts if t.startswith("CREATE DATABASE"))

        # sql.Identifier double-quotes the identifier and escapes embedded
        # double quotes, so the payload never breaks out of identifier
        # position into a second raw statement.
        assert create_role.startswith('CREATE ROLE "x""; DROP TABLE foo; --" ')
        assert create_db.startswith('CREATE DATABASE "y""; DROP DATABASE prod; --" ')
        # A raw (unquoted) f-string interpolation would have produced a bare
        # `DROP TABLE foo;` statement fragment outside any quotes.
        assert 'DROP TABLE foo; --" ' in create_role  # only inside the closing quote
        assert not create_role.rstrip().endswith("DROP TABLE foo")


# ---------------------------------------------------------------------------
# deprovision_app
# ---------------------------------------------------------------------------

class TestDeprovisionApp:
    def test_issues_terminate_drop_database_drop_role(self, recorder):
        install, rec = recorder
        install()

        pg_admin.deprovision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")

        texts = rec.texts()
        assert any("pg_terminate_backend" in t for t in texts)
        assert any(t == 'DROP DATABASE IF EXISTS "myapp_db"' for t in texts)
        assert any(t == 'DROP ROLE IF EXISTS "myapp_role"' for t in texts)

    def test_terminate_backend_uses_bound_param_not_interpolation(self, recorder):
        install, rec = recorder
        install()

        pg_admin.deprovision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")

        text, params = next(c for c in rec.calls if "pg_terminate_backend" in c[0])
        assert "myapp_db" not in text
        assert params == ("myapp_db",)

    def test_mid_step_failure_does_not_abort_later_steps(self, recorder):
        install, rec = recorder

        def raise_on_drop_database(text: str) -> bool:
            return text.startswith("DROP DATABASE")

        install(raise_on=raise_on_drop_database)

        # Must not raise: failures are logged and swallowed.
        pg_admin.deprovision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")

        texts = rec.texts()
        assert any("pg_terminate_backend" in t for t in texts)
        assert any(t.startswith("DROP DATABASE") for t in texts)  # attempted despite raising
        assert any(t == 'DROP ROLE IF EXISTS "myapp_role"' for t in texts)  # still attempted after

    def test_connect_failure_is_swallowed(self, monkeypatch):
        def raiser(**kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(pg_admin.psycopg, "connect", raiser)

        # Must not raise even though connecting itself fails.
        pg_admin.deprovision_app("pg.test.local:5432", "bootstrap-pw", "myapp_db", "myapp_role")
