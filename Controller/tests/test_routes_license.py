from __future__ import annotations

import json
import logging

import yaml


class TestUpdateLicense:
    def test_happy_path_secret_written_before_background_task(self, client, fake_sf, fake_registry,
                                                               make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        secret_calls = fake_sf.calls_for("create_or_replace_secret")
        assert (("TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY", "top-secret-key"), {}) in secret_calls

        # The secret is written by the endpoint handler itself, before the
        # background task (which does the alter_service_spec restart) ever runs.
        names_in_order = [n for (n, a, k) in fake_sf.calls]
        assert names_in_order.index("create_or_replace_secret") < names_in_order.index("alter_service_spec")

        statuses = [f.get("last_deploy_status") for (_, f) in fake_registry.updates]
        assert "DEPLOYING" in statuses

        final = fake_registry.get_app("myapp")
        assert final.license_id == "LIC-1"
        assert final.last_deploy_status == "READY"

    def test_spec_rebuilt_with_license(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                   json={"license_id": "LIC-1", "license_key": "top-secret-key"})

        alter_call = fake_sf.calls_for("alter_service_spec")[0]
        parsed = yaml.safe_load(alter_call[0][1])
        env = parsed["spec"]["containers"][0]["env"]
        assert env["RUNTIME_LICENSE_ID"] == "LIC-1"
        secrets = parsed["spec"]["containers"][0]["secrets"]
        entry = next(s for s in secrets if s["directoryPath"] == "/secrets/mx_license_key")
        assert entry["snowflakeSecret"] == "TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY"

    def test_poll_timeout_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                       role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        # license_id is persisted up front, independent of the restart outcome -
        # same pattern as constants (see _run_update_constants).
        assert final.license_id == "LIC-1"
        assert final.last_deploy_status == "FAILED"

    def test_alter_service_spec_exception_marks_failed_no_escape(self, client, fake_sf, fake_registry,
                                                                  make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 202  # exception happens in the background task, not the request
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.put("/apps/ghost/license", headers=role_headers("OWNER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 404

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/license", headers=role_headers("OTHER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 403

    def test_privileged_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/license", headers=role_headers("PRIV_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 202

    def test_invalid_body_missing_fields_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"), json={})
        assert resp.status_code == 422

    def test_key_never_leaks_in_response_registry_or_logs(self, client, fake_sf, fake_registry,
                                                           make_record, role_headers, caplog):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        secret_value = "SUPER-SECRET-LICENSE-KEY-VALUE"
        with caplog.at_level(logging.DEBUG):
            resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                              json={"license_id": "LIC-1", "license_key": secret_value})
        assert secret_value not in resp.text
        for (_name, fields) in fake_registry.updates:
            assert secret_value not in json.dumps(fields)
        assert secret_value not in caplog.text


class TestDeleteLicense:
    def test_happy_path_nulls_license_and_drops_secret_after_running(self, client, fake_sf, fake_registry,
                                                                      make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1")
        fake_registry.add(record)
        resp = client.delete("/apps/myapp/license", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        final = fake_registry.get_app("myapp")
        assert final.license_id is None
        assert final.last_deploy_status == "READY"

        alter_call = fake_sf.calls_for("alter_service_spec")[0]
        parsed = yaml.safe_load(alter_call[0][1])
        env = parsed["spec"]["containers"][0]["env"]
        assert "RUNTIME_LICENSE_ID" not in env
        secrets = parsed["spec"]["containers"][0]["secrets"]
        assert all(s["directoryPath"] != "/secrets/mx_license_key" for s in secrets)

        # drop_secret only happens after the restart has actually happened (poll saw RUNNING).
        names_in_order = [n for (n, a, k) in fake_sf.calls]
        assert names_in_order.index("alter_service_spec") < names_in_order.index("drop_secret")
        assert fake_sf.calls_for("drop_secret") == [(("TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY",), {})]

    def test_poll_timeout_leaves_secret_undropped_and_failed(self, client, fake_sf, fake_registry,
                                                              make_record, role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1")
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.delete("/apps/myapp/license", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_sf.calls_for("drop_secret") == []
        final = fake_registry.get_app("myapp")
        # license_id is nulled up front regardless of the restart outcome.
        assert final.license_id is None
        assert final.last_deploy_status == "FAILED"

    def test_alter_exception_leaves_secret_undropped_and_failed(self, client, fake_sf, fake_registry,
                                                                 make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1")
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.delete("/apps/myapp/license", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_sf.calls_for("drop_secret") == []
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.delete("/apps/ghost/license", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1"))
        resp = client.delete("/apps/myapp/license", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_privileged_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1"))
        resp = client.delete("/apps/myapp/license", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 202


def _mx_role_mapping_env(fake_sf):
    alter_call = fake_sf.calls_for("alter_service_spec")[-1]
    parsed = yaml.safe_load(alter_call[0][1])
    return parsed["spec"]["containers"][0]["env"].get("MX_ROLE_MAPPING")


class TestRoleMappingSurvivesLicenseRestarts:
    """Regression guard for the six _build_spec call sites (plan section 5a): the
    license endpoints must not silently strip MX_ROLE_MAPPING from the spec."""

    def test_update_license_keeps_role_mapping(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        resp = client.put("/apps/myapp/license", headers=role_headers("OWNER_ROLE"),
                          json={"license_id": "LIC-1", "license_key": "top-secret-key"})
        assert resp.status_code == 202
        assert _mx_role_mapping_env(fake_sf) is not None

    def test_delete_license_keeps_role_mapping(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", license_id="LIC-1",
                             role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        resp = client.delete("/apps/myapp/license", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert _mx_role_mapping_env(fake_sf) is not None
