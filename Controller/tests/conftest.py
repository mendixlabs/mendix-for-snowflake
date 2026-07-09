"""Shared fixtures for the Controller test suite.

Env vars are set at module import time (before pytest imports any test module),
which is required because ``app.main``, ``app.snowflake_client``, ``app.registry``,
``app.activity`` and ``app.auth`` all read ``os.environ[...]`` at import time.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

_TEST_ENV = {
    "DB_SCHEMA": "TESTDB.PUBLIC",
    "COMPUTE_POOL": "TEST_POOL",
    "IMAGE_REPO": "testdb/public/test_repo",
    "PG_EAI": "TEST_EAI",
    "QUERY_WAREHOUSE": "TEST_WH",
    "SNOWFLAKE_HOST": "test.snowflakecomputing.example",
    "SNOWFLAKE_ACCOUNT": "TESTACCT",
    "INTERNAL_AUTH_TOKEN": "test-internal-token",
    "PRIVILEGED_ROLES": "PRIV_ROLE",
    "PG_HOST": "pg.test.local:5432",
    "PG_PASS": "test-pg-password",
}
os.environ.update(_TEST_ENV)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../Controller

from fastapi.testclient import TestClient  # noqa: E402

from app import activity, auth, main, pg_admin, registry, snowflake_client  # noqa: E402
from app.models import AppRecord, HIDDEN_VALUE  # noqa: E402
from app.pad_parser import PadConstant  # noqa: E402


# ---------------------------------------------------------------------------
# Autouse module-state reset
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    main._PG_HOST = None
    main._PG_PASSWORD = None
    main._log_jobs.clear()
    snowflake_client._conn = None
    auth._identity_cache.clear()
    yield


# ---------------------------------------------------------------------------
# fake_sf: records every snowflake_client call main.py makes
# ---------------------------------------------------------------------------

_PATCHED_SF_FUNCS = [
    "create_schema",
    "create_stage",
    "create_or_replace_secret",
    "drop_secret",
    "create_service",
    "alter_service_spec",
    "suspend_service",
    "resume_service",
    "drop_service",
    "drop_schema_cascade",
    "create_app_access_role",
    "grant_endpoint_to_app_role",
    "drop_app_access_role",
    "show_service_status",
    "show_all_service_statuses",
    "get_service_endpoint",
    "get_service_logs",
    "get_compute_pool",
    "alter_compute_pool",
]


class FakeSF:
    """Records every call main.py makes into snowflake_client, with scriptable
    return values / exceptions. Never touches the network."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        # name -> status returned by show_service_status/show_all_service_statuses
        self.service_statuses: dict[str, str] = {}
        # name -> queue of statuses; show_service_status pops until one remains,
        # then repeats the last value forever (so a poll loop can be scripted
        # without ever sleeping).
        self.status_queue: dict[str, list[str]] = {}
        self.endpoints: dict[str, str] = {}
        self.logs = "fake log line"
        self.compute_pool: dict | None = {
            "name": "TEST_POOL",
            "state": "ACTIVE",
            "min_nodes": 1,
            "max_nodes": 1,
            "instance_family": "CPU_X64_XS",
            "auto_suspend_secs": 600,
            "num_services": 1,
        }
        self.raise_on: dict[str, Exception] = {}

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.raise_on:
            raise self.raise_on[name]

    def calls_for(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for (n, a, k) in self.calls if n == name]

    def create_schema(self, fqn):
        self._record("create_schema", (fqn,), {})

    def create_stage(self, fqn):
        self._record("create_stage", (fqn,), {})

    def create_or_replace_secret(self, fqn, value):
        self._record("create_or_replace_secret", (fqn, value), {})

    def drop_secret(self, fqn):
        self._record("drop_secret", (fqn,), {})

    def create_service(self, name, spec, compute_pool, eai, warehouse):
        self._record("create_service", (name, spec, compute_pool, eai, warehouse), {})

    def alter_service_spec(self, name, spec):
        self._record("alter_service_spec", (name, spec), {})

    def suspend_service(self, name):
        self._record("suspend_service", (name,), {})

    def resume_service(self, name):
        self._record("resume_service", (name,), {})

    def drop_service(self, name):
        self._record("drop_service", (name,), {})

    def drop_schema_cascade(self, fqn):
        self._record("drop_schema_cascade", (fqn,), {})

    def create_app_access_role(self, app_name):
        self._record("create_app_access_role", (app_name,), {})

    def grant_endpoint_to_app_role(self, service_name, app_role):
        self._record("grant_endpoint_to_app_role", (service_name, app_role), {})

    def drop_app_access_role(self, app_name):
        self._record("drop_app_access_role", (app_name,), {})

    def show_service_status(self, name):
        self._record("show_service_status", (name,), {})
        q = self.status_queue.get(name)
        if q:
            val = q[0]
            if len(q) > 1:
                q.pop(0)
            return val
        return self.service_statuses.get(name, "RUNNING")

    def show_all_service_statuses(self):
        self._record("show_all_service_statuses", (), {})
        return dict(self.service_statuses)

    def get_service_endpoint(self, name):
        self._record("get_service_endpoint", (name,), {})
        return self.endpoints.get(name)

    def get_service_logs(self, name, container="mendix-app", lines=100):
        self._record("get_service_logs", (name,), {"container": container, "lines": lines})
        return self.logs

    def get_compute_pool(self, pool_name):
        self._record("get_compute_pool", (pool_name,), {})
        return self.compute_pool

    def alter_compute_pool(self, pool_name, *, min_nodes=None, max_nodes=None, auto_suspend_secs=None):
        self._record(
            "alter_compute_pool",
            (pool_name,),
            {"min_nodes": min_nodes, "max_nodes": max_nodes, "auto_suspend_secs": auto_suspend_secs},
        )


@pytest.fixture
def fake_sf(monkeypatch):
    fake = FakeSF()
    for name in _PATCHED_SF_FUNCS:
        monkeypatch.setattr(snowflake_client, name, getattr(fake, name))
    return fake


# ---------------------------------------------------------------------------
# fake_registry: in-memory AppRecord store
# ---------------------------------------------------------------------------

class FakeRegistry:
    def __init__(self):
        self.apps: dict[str, AppRecord] = {}  # keyed by UPPER(name)
        self.updates: list[tuple[str, dict]] = []

    def add(self, record: AppRecord) -> None:
        self.apps[record.name.upper()] = record

    def get_app(self, name: str):
        return self.apps.get(name.upper())

    def list_apps(self):
        return list(self.apps.values())

    def create_app(self, record: AppRecord) -> None:
        self.apps[record.name.upper()] = record

    def update_app(self, name: str, fields: dict) -> None:
        key = name.upper()
        rec = self.apps.get(key)
        self.updates.append((name, dict(fields)))
        if rec is None:
            return
        self.apps[key] = rec.model_copy(update=fields)

    def delete_app(self, name: str) -> None:
        self.apps.pop(name.upper(), None)


@pytest.fixture
def fake_registry(monkeypatch):
    fake = FakeRegistry()
    for name in ("get_app", "list_apps", "create_app", "update_app", "delete_app"):
        monkeypatch.setattr(registry, name, getattr(fake, name))
    return fake


# ---------------------------------------------------------------------------
# fake_pg_admin: records per-app Postgres provisioning/deprovisioning calls
# ---------------------------------------------------------------------------

class FakePgAdmin:
    """Records every call main.py makes into pg_admin, with a scriptable
    provisioned password. Never touches a real Postgres server."""

    def __init__(self):
        self.provisioned_password = "per-app-generated-pw"
        self.provision_calls: list[tuple] = []
        self.deprovision_calls: list[tuple] = []

    def provision_app(self, host_port, bootstrap_password, pg_database, pg_username):
        self.provision_calls.append((host_port, bootstrap_password, pg_database, pg_username))
        return self.provisioned_password

    def deprovision_app(self, host_port, bootstrap_password, pg_database, pg_username):
        self.deprovision_calls.append((host_port, bootstrap_password, pg_database, pg_username))


@pytest.fixture
def fake_pg_admin(monkeypatch):
    fake = FakePgAdmin()
    monkeypatch.setattr(pg_admin, "provision_app", fake.provision_app)
    monkeypatch.setattr(pg_admin, "deprovision_app", fake.deprovision_app)
    return fake


# ---------------------------------------------------------------------------
# fake_activity
# ---------------------------------------------------------------------------

class FakeActivity:
    def __init__(self):
        self.rows: list[dict] = []
        self.preset: list[dict] = []

    def insert(self, operator, action, app_name, detail, result="accepted") -> None:
        self.rows.append({
            "operator": operator, "action": action, "app_name": app_name,
            "detail": detail, "result": result,
        })

    def init_table(self) -> None:
        pass

    def query(self, app=None, operator=None, limit=100):
        rows = self.preset
        if app:
            rows = [r for r in rows if r.get("app_name") == app]
        if operator:
            rows = [r for r in rows if r.get("operator") == operator]
        return rows[:limit]


@pytest.fixture
def fake_activity(monkeypatch):
    fake = FakeActivity()
    monkeypatch.setattr(activity, "insert", fake.insert)
    monkeypatch.setattr(activity, "init_table", fake.init_table)
    monkeypatch.setattr(activity, "query", fake.query)
    return fake


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

@pytest.fixture
def client(fake_sf, fake_registry, fake_activity, fake_pg_admin):
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# make_record
# ---------------------------------------------------------------------------

@pytest.fixture
def make_record():
    def _make(**overrides) -> AppRecord:
        name = overrides.pop("name", "myapp")
        defaults = dict(
            name=name,
            service_name=main._service_name(name),
            app_schema=main._app_schema_name(name),
            pg_database="myapp_db",
            resource_tier="medium",
            use_caller_rights=False,
            constants={},
            owner_role="OWNER_ROLE",
            # A READY app has a deployed PAD; tests that need the pre-deploy state
            # (no PAD staged) override pad_stage_path=None explicitly.
            pad_stage_path=f"apps/{name}/current.zip",
            endpoint_url=None,
            last_deploy_status="READY",
            created_at=None,
            last_deployed_at=None,
        )
        defaults.update(overrides)
        return AppRecord(**defaults)

    return _make


# ---------------------------------------------------------------------------
# role_headers: a factory fixture (not a plain module function) so test modules
# don't need to import from conftest directly, which --import-mode=importlib
# does not reliably support.
# ---------------------------------------------------------------------------

@pytest.fixture
def role_headers():
    def _make(*roles: str, operator: str = "TEST_USER") -> dict[str, str]:
        return {
            "X-Operator": operator,
            "X-Operator-Roles": ",".join(roles),
            "X-Internal-Auth": "test-internal-token",
        }

    return _make


@pytest.fixture
def make_pad_zip(tmp_path):
    def _make(
        *,
        defaults_text: str | None = None,
        variables_text: str | None = None,
        nested: str | None = None,
        omit: str | None = None,
        filename: str = "pad.zip",
        extra_defaults: dict[str, str] | None = None,
        extra_vars: dict[str, str] | None = None,
        metadata_json: str | dict | None = None,
    ):
        if defaults_text is None:
            lines = ['"MyModule.MyConst" = "hello"']
            for k, v in (extra_defaults or {}).items():
                lines.append(f'"{k}" = "{v}"')
            defaults_text = "\n".join(lines) + "\n"
        if variables_text is None:
            lines = ['"MyModule.MyConst" = ${?MyModule_MyConst}']
            for k, v in (extra_vars or {}).items():
                lines.append(f'"{k}" = ${{?{v}}}')
            variables_text = "\n".join(lines) + "\n"
        prefix = f"{nested}/" if nested else ""
        zpath = tmp_path / filename
        with zipfile.ZipFile(zpath, "w") as zf:
            if omit != "defaults":
                zf.writestr(f"{prefix}etc/constants/defaults.conf", defaults_text)
            if omit != "variables":
                zf.writestr(f"{prefix}etc/constants/variables.conf", variables_text)
            if metadata_json is not None:
                text = metadata_json if isinstance(metadata_json, str) else json.dumps(metadata_json)
                zf.writestr(f"{prefix}model/metadata.json", text)
        return zpath

    return _make


@pytest.fixture
def staged_pad(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))

    def _stage(name: str, src_zip=None, filename: str = "current.zip"):
        app_dir = tmp_path / "apps" / name
        app_dir.mkdir(parents=True, exist_ok=True)
        dest = app_dir / filename
        if src_zip is not None:
            shutil.copy(src_zip, dest)
        else:
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("etc/constants/defaults.conf", '"MyModule.MyConst" = "hello"\n')
                zf.writestr("etc/constants/variables.conf", '"MyModule.MyConst" = ${?MyModule_MyConst}\n')
        return dest

    return _stage


# ---------------------------------------------------------------------------
# fake_execute_sql: shared low-level seam for Phase B (registry/activity/
# snowflake_client unit tests)
# ---------------------------------------------------------------------------

class FakeExecuteSql:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.returns: list[list[dict]] = []  # queued return values; last repeats

    def __call__(self, sql: str, params: tuple = ()) -> list[dict]:
        self.calls.append((sql, params))
        if self.returns:
            if len(self.returns) > 1:
                return self.returns.pop(0)
            return self.returns[0]
        return []


@pytest.fixture
def fake_execute_sql(monkeypatch):
    fake = FakeExecuteSql()
    monkeypatch.setattr(snowflake_client, "execute_sql", fake)
    return fake
