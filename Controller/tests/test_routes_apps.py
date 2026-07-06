from __future__ import annotations

import yaml

from app import main
from app.models import HIDDEN_VALUE


def _create_payload(**overrides):
    payload = dict(name="myapp", pg_database="myapp_db", admin_password="adminpw123")
    payload.update(overrides)
    return payload


class TestHealth:
    def test_health_no_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestCreateAppHappyPath:
    def test_call_sequence_and_registry_record(self, client, fake_sf, fake_registry, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(constants={"Mod.A": "value_a"}))
        assert resp.status_code == 201

        names_in_order = [n for (n, a, k) in fake_sf.calls]
        assert names_in_order.index("create_schema") < names_in_order.index("create_stage")
        assert names_in_order.index("create_stage") < names_in_order.index("create_service")

        assert fake_sf.calls_for("create_schema")[0][0] == ("TESTDB.MXAPP_MYAPP",)
        assert fake_sf.calls_for("create_stage")[0][0] == ("TESTDB.MXAPP_MYAPP.FILESTORAGE_STAGE",)

        secret_calls = {args[0]: args[1] for (args, kw) in fake_sf.calls_for("create_or_replace_secret")}
        assert secret_calls["TESTDB.MXAPP_MYAPP.PG_PASS"] == "test-pg-password"
        assert secret_calls["TESTDB.MXAPP_MYAPP.ADMIN_PASS"] == "adminpw123"
        assert secret_calls["TESTDB.MXAPP_MYAPP.MX_CONST_MOD_A"] == "value_a"

        create_service_calls = fake_sf.calls_for("create_service")
        assert len(create_service_calls) == 1
        args, kw = create_service_calls[0]
        assert args[0] == "MYAPP_SERVICE"
        assert args[2:] == ("TEST_POOL", "TEST_EAI", "TEST_WH")

        assert fake_sf.calls_for("create_app_access_role")[0][0] == ("myapp",)
        grant_calls = [args for (args, kw) in fake_sf.calls_for("grant_endpoint_to_app_role")]
        assert ("MYAPP_SERVICE", "app_myapp_user") in grant_calls
        assert ("MYAPP_SERVICE", "app_admin") in grant_calls

        record = fake_registry.get_app("myapp")
        assert record is not None
        assert record.last_deploy_status == "NOT_DEPLOYED"
        assert record.app_schema == "MXAPP_MYAPP"

    def test_use_caller_rights_sets_execute_as_caller(self, client, fake_sf, fake_registry, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(name="callerapp", use_caller_rights=True))
        assert resp.status_code == 201
        spec = fake_sf.calls_for("create_service")[0][0][1]
        parsed = yaml.safe_load(spec)
        assert parsed["capabilities"]["securityContext"]["executeAsCaller"] is True

    def test_duplicate_name_409(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp"))
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"), json=_create_payload())
        assert resp.status_code == 409

    def test_hidden_value_constant_rejected_422(self, client, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(constants={"Mod.A": HIDDEN_VALUE}))
        assert resp.status_code == 422

    def test_missing_pg_password_409(self, client, fake_sf, role_headers, monkeypatch):
        monkeypatch.setenv("PG_PASS", "")
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"), json=_create_payload())
        assert resp.status_code == 409

    def test_missing_pg_password_via_load_pg_credentials_monkeypatch_409(
        self, client, fake_sf, role_headers, monkeypatch
    ):
        # Mirrors the task's stated approach: monkeypatch _load_pg_credentials
        # directly to return an empty password, rather than going through the
        # env-var/global-cache path exercised above.
        monkeypatch.setattr(main, "_load_pg_credentials", lambda: ("localhost:5432", ""))
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"), json=_create_payload())
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Controller PG credentials not mounted at /secrets/pg"

    def test_invalid_body_bad_name_pattern_422(self, client, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(name="1-bad-name"))
        assert resp.status_code == 422

    def test_license_fields_write_secret_and_store_id(self, client, fake_sf, fake_registry, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(license_id="LIC-1", license_key="secret-key-val"))
        assert resp.status_code == 201
        secret_calls = {args[0]: args[1] for (args, kw) in fake_sf.calls_for("create_or_replace_secret")}
        assert secret_calls["TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY"] == "secret-key-val"
        record = fake_registry.get_app("myapp")
        assert record.license_id == "LIC-1"

    def test_no_license_fields_no_secret_no_id(self, client, fake_sf, fake_registry, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"), json=_create_payload())
        assert resp.status_code == 201
        secret_names = {args[0] for (args, kw) in fake_sf.calls_for("create_or_replace_secret")}
        assert "TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY" not in secret_names
        assert fake_registry.get_app("myapp").license_id is None

    def test_exactly_one_license_field_422(self, client, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=_create_payload(license_id="LIC-1"))
        assert resp.status_code == 422


class TestGetApp:
    def test_response_shape(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert "app" in body and "service_status" in body
        assert body["app"]["name"] == "myapp"

    def test_endpoint_healing_when_running_and_stored_empty(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", endpoint_url=None)
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.endpoints[record.service_name] = "https://live.example.com"
        resp = client.get("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json()["app"]["endpoint_url"] == "https://live.example.com"
        assert fake_sf.calls_for("get_service_endpoint")
        updated = fake_registry.get_app("myapp")
        assert updated.endpoint_url == "https://live.example.com"

    def test_stored_real_endpoint_no_lookup(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", endpoint_url="https://already.example.com")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        resp = client.get("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json()["app"]["endpoint_url"] == "https://already.example.com"
        assert fake_sf.calls_for("get_service_endpoint") == []


class TestDeleteApp:
    def test_happy_path_no_secret_sweep(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Current behavior (schema-per-app model): delete_app no longer sweeps
        # individual secrets by FQN. It suspends (best-effort), drops the
        # service + access role, then drops the app's whole schema (CASCADE),
        # which removes the secrets as a side effect. drop_secret is unused
        # here (it still exists in snowflake_client.py and is unit-tested
        # separately in test_snowflake_client.py).
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             constants={"Mod.A": HIDDEN_VALUE, "Mod.B": HIDDEN_VALUE})
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.delete("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 204
        assert fake_sf.calls_for("drop_service") == [(("MYAPP_SERVICE",), {})]
        assert fake_sf.calls_for("drop_app_access_role") == [(("myapp",), {})]
        assert fake_sf.calls_for("drop_schema_cascade") == [(("TESTDB.MXAPP_MYAPP",), {})]
        assert fake_sf.calls_for("drop_secret") == []
        assert fake_registry.get_app("myapp") is None

    def test_suspend_failure_tolerated(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.raise_on["suspend_service"] = RuntimeError("suspend boom")
        resp = client.delete("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 204
        assert fake_sf.calls_for("drop_service")
        assert fake_sf.calls_for("drop_schema_cascade")
        assert fake_registry.get_app("myapp") is None

    def test_drop_failure_returns_502_and_keeps_record(self, client, fake_sf, fake_registry, make_record, role_headers):
        # A cleanup failure must produce a handled, retryable error: the
        # remaining steps are still attempted (all drops are IF EXISTS), and
        # the registry row survives as the operator's handle for retrying.
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        fake_sf.raise_on["drop_service"] = RuntimeError("drop boom")
        resp = client.delete("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 502
        assert "drop service" in resp.json()["detail"]
        # Later steps were still attempted despite the earlier failure.
        assert fake_sf.calls_for("drop_app_access_role")
        assert fake_sf.calls_for("drop_schema_cascade")
        assert fake_registry.get_app("myapp") is not None

    def test_failed_delete_can_be_retried(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        fake_sf.raise_on["drop_schema_cascade"] = RuntimeError("schema boom")
        assert client.delete("/apps/myapp", headers=role_headers("OWNER_ROLE")).status_code == 502
        assert fake_registry.get_app("myapp") is not None
        del fake_sf.raise_on["drop_schema_cascade"]
        resp = client.delete("/apps/myapp", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 204
        assert fake_registry.get_app("myapp") is None
