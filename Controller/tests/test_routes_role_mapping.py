from __future__ import annotations

import json

import yaml


class TestUpdateRoleMapping:
    def test_happy_path_persisted_up_front_and_spec_env(self, client, fake_sf, fake_registry,
                                                         make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=True,
                             user_roles=["User", "Administrator"])
        fake_registry.add(record)
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"my_role": "Administrator"}})
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING", "warnings": []}

        final = fake_registry.get_app("myapp")
        assert final.role_mapping == {"MY_ROLE": "Administrator"}
        assert final.last_deploy_status == "READY"

        alter_call = fake_sf.calls_for("alter_service_spec")[0]
        parsed = yaml.safe_load(alter_call[0][1])
        env = parsed["spec"]["containers"][0]["env"]
        assert json.loads(env["MX_ROLE_MAPPING"]) == {"MY_ROLE": "Administrator"}

    def test_never_masked_real_mapping_in_registry_update(self, client, fake_sf, fake_registry,
                                                           make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=True,
                             user_roles=["Administrator"])
        fake_registry.add(record)
        client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                   json={"role_mapping": {"role_a": "Administrator"}})
        mapping_updates = [f["role_mapping"] for (_, f) in fake_registry.updates if "role_mapping" in f]
        assert {"ROLE_A": "Administrator"} in mapping_updates
        assert all(v != "<HIDDEN>" for m in mapping_updates for v in m.values())

    def test_keys_uppercased(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=True,
                             user_roles=["Administrator"])
        fake_registry.add(record)
        client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                   json={"role_mapping": {"my_role": "Administrator"}})
        assert fake_registry.get_app("myapp").role_mapping == {"MY_ROLE": "Administrator"}

    def test_duplicate_after_uppercasing_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "User", "ROLE_A": "Administrator"}})
        assert resp.status_code == 422

    def test_empty_mapping_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {}})
        assert resp.status_code == 422

    def test_quote_in_value_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "bad'role"}})
        assert resp.status_code == 422

    def test_more_than_fifty_entries_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        mapping = {f"role_{i}": "User" for i in range(51)}
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": mapping})
        assert resp.status_code == 422

    def test_unknown_userrole_vs_stored_user_roles_422(self, client, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", user_roles=["User", "Administrator"])
        fake_registry.add(record)
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Nonexistent"}})
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert body["unknown_userroles"] == ["Nonexistent"]
        assert body["detected_userroles"] == ["User", "Administrator"]

    def test_no_user_roles_stored_202_with_warning(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", user_roles=[])
        fake_registry.add(record)
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 202
        assert any("No userroles detected" in w for w in resp.json()["warnings"])

    def test_use_caller_rights_off_202_with_inert_warning(self, client, fake_sf, fake_registry,
                                                           make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=False,
                             user_roles=["Administrator"])
        fake_registry.add(record)
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 202
        assert any("use_caller_rights is off" in w for w in resp.json()["warnings"])

    def test_alter_service_spec_exception_marks_failed_no_escape(self, client, fake_sf, fake_registry,
                                                                  make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", user_roles=["Administrator"])
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_poll_timeout_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                       role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", user_roles=["Administrator"])
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.role_mapping == {"ROLE_A": "Administrator"}
        assert final.last_deploy_status == "FAILED"

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.put("/apps/ghost/role-mapping", headers=role_headers("OWNER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 404

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OTHER_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 403

    def test_privileged_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", user_roles=["Administrator"]))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("PRIV_ROLE"),
                          json={"role_mapping": {"role_a": "Administrator"}})
        assert resp.status_code == 202

    def test_invalid_body_missing_field_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"), json={})
        assert resp.status_code == 422


class TestDeleteRoleMapping:
    def test_happy_path_nulls_mapping_and_env_removed(self, client, fake_sf, fake_registry,
                                                       make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        resp = client.delete("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        final = fake_registry.get_app("myapp")
        # The fake registry stores the raw update (SQL NULL, mirrored as None) without
        # re-applying registry._row_to_record's None -> {} default; the real registry
        # round-trips NULL back to {} on the next read.
        assert not final.role_mapping
        assert final.last_deploy_status == "READY"

        alter_call = fake_sf.calls_for("alter_service_spec")[0]
        parsed = yaml.safe_load(alter_call[0][1])
        env = parsed["spec"]["containers"][0]["env"]
        assert "MX_ROLE_MAPPING" not in env

    def test_poll_timeout_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                       role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.delete("/apps/myapp/role-mapping", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.delete("/apps/ghost/role-mapping", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      role_mapping={"ROLE_A": "Administrator"}))
        resp = client.delete("/apps/myapp/role-mapping", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_privileged_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      role_mapping={"ROLE_A": "Administrator"}))
        resp = client.delete("/apps/myapp/role-mapping", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 202
