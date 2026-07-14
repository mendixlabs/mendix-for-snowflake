from __future__ import annotations

from app import main


class TestGetExternalAccessSlots:
    def test_shape_all_four_slots_present(self, client, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {})
        resp = client.get("/system/external-access", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        slots = resp.json()["slots"]
        assert [s["key"] for s in slots] == ["app_eai_1", "app_eai_2", "app_eai_3", "app_eai_4"]
        assert all(s["bound"] is False for s in slots)
        assert all(s["integration_name"] is None for s in slots)
        assert all(s["label"] for s in slots)

    def test_bound_slot_reports_bound_true(self, client, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        resp = client.get("/system/external-access", headers=role_headers("OWNER_ROLE"))
        slots = {s["key"]: s for s in resp.json()["slots"]}
        assert slots["app_eai_1"]["bound"] is True
        assert slots["app_eai_2"]["bound"] is False

    def test_non_privileged_caller_does_not_see_integration_name(self, client, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        resp = client.get("/system/external-access", headers=role_headers("OWNER_ROLE"))
        slots = {s["key"]: s for s in resp.json()["slots"]}
        assert slots["app_eai_1"]["bound"] is True
        assert slots["app_eai_1"]["integration_name"] is None

    def test_privileged_caller_sees_integration_name(self, client, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        resp = client.get("/system/external-access", headers=role_headers("PRIV_ROLE"))
        slots = {s["key"]: s for s in resp.json()["slots"]}
        assert slots["app_eai_1"]["integration_name"] == "REAL_APP_EAI_1"

    def test_available_to_any_authenticated_caller_not_just_privileged(
        self, client, role_headers, monkeypatch
    ):
        # Deliberate deviation from the privileged-only gate other /system/*
        # endpoints use - any operator's role (not PRIV_ROLE) still gets 200.
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {})
        resp = client.get("/system/external-access", headers=role_headers("SOME_OTHER_ROLE"))
        assert resp.status_code == 200


class TestCreateAppExternalAccess:
    def _payload(self, **overrides):
        payload = dict(name="myapp", pg_database="myapp_db", admin_password="adminpw123")
        payload.update(overrides)
        return payload

    def test_bound_slot_persisted_and_composed_into_create_service(
        self, client, fake_sf, fake_registry, role_headers, monkeypatch
    ):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=self._payload(external_access=["app_eai_1"]))
        assert resp.status_code == 201
        record = fake_registry.get_app("myapp")
        assert record.external_access == ["app_eai_1"]

        args, kw = fake_sf.calls_for("create_service")[0]
        eai_names = args[3]
        assert eai_names == ["TEST_EAI", "REAL_APP_EAI_1"]

    def test_unbound_slot_rejected_422(self, client, fake_sf, fake_registry, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {})
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=self._payload(external_access=["app_eai_1"]))
        assert resp.status_code == 422
        assert "app_eai_1" in resp.json()["detail"]
        assert fake_registry.get_app("myapp") is None
        assert fake_sf.calls_for("create_schema") == []

    def test_unknown_slot_key_rejected_422_by_model(self, client, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"),
                           json=self._payload(external_access=["not_a_real_slot"]))
        assert resp.status_code == 422

    def test_no_external_access_defaults_to_empty(self, client, fake_registry, role_headers):
        resp = client.post("/apps", headers=role_headers("PRIV_ROLE"), json=self._payload())
        assert resp.status_code == 201
        assert fake_registry.get_app("myapp").external_access == []


class TestUpdateExternalAccess:
    def test_unknown_slot_422(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["not_a_real_slot"]})
        assert resp.status_code == 422

    def test_unbound_slot_422(self, client, fake_registry, make_record, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {})
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 422
        assert "app_eai_1" in resp.json()["detail"]

    def test_stranger_403(self, client, fake_registry, make_record, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_1"})
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OTHER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 403

    def test_missing_app_404(self, client, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_1"})
        resp = client.put("/apps/ghost/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 404

    def test_transient_409(self, client, fake_registry, make_record, role_headers, monkeypatch):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_1"})
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", last_deploy_status="DEPLOYING"))
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 409

    def test_happy_path_202_applies_and_records_history(
        self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, monkeypatch
    ):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        final = fake_registry.get_app("myapp")
        assert final.external_access == ["app_eai_1"]
        assert final.last_deploy_status == "READY"

        args, kw = fake_sf.calls_for("set_service_external_access")[0]
        assert args[0] == record.service_name
        assert args[1] == ["TEST_EAI", "REAL_APP_EAI_1"]
        # No spec rebuild at all - this endpoint never touches _build_spec.
        assert fake_sf.calls_for("alter_service_spec") == []

        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["operation"] == "external_access"
        assert rows[0]["status"] == "READY"
        assert rows[0]["external_access"] == ["app_eai_1"]

    def test_empty_slots_detaches_everything(
        self, client, fake_sf, fake_registry, make_record, role_headers, monkeypatch
    ):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        record = make_record(name="myapp", owner_role="OWNER_ROLE", external_access=["app_eai_1"])
        fake_registry.add(record)
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"), json={"slots": []})
        assert resp.status_code == 202
        args, kw = fake_sf.calls_for("set_service_external_access")[0]
        assert args[1] == ["TEST_EAI"]
        assert fake_registry.get_app("myapp").external_access == []

    def test_failure_marks_failed_operation_external_access(
        self, client, fake_sf, fake_registry, make_record, role_headers, monkeypatch
    ):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_sf.raise_on["set_service_external_access"] = RuntimeError("boom")
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "external_access"
        assert final.status_detail == "boom"

    def test_no_pad_required(self, client, fake_sf, fake_registry, make_record, role_headers, monkeypatch):
        # Unlike constants/spec/license/role-mapping, this endpoint never rebuilds
        # the spec, so an app with no PAD deployed yet can still set external
        # access ahead of its first deploy.
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        record = make_record(name="myapp", owner_role="OWNER_ROLE", pad_stage_path=None,
                             last_deploy_status="NOT_DEPLOYED")
        fake_registry.add(record)
        resp = client.put("/apps/myapp/external-access", headers=role_headers("OWNER_ROLE"),
                          json={"slots": ["app_eai_1"]})
        assert resp.status_code == 202


class TestRollbackAppliesExternalAccess:
    def _seed_history_row(self, fake_deploy_history, **overrides):
        row = {
            "app_name": "myapp", "operation": "deploy", "status": "READY", "detail": None,
            "pad_stage_path": "apps/myapp/current.zip", "resource_tier": "medium",
            "use_caller_rights": False, "constant_names": [], "license_id": None,
            "role_mapping": {}, "external_access": [],
        }
        row.update(overrides)
        fake_deploy_history.rows.append(row)

    def test_rollback_reapplies_recorded_external_access(
        self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, staged_pad,
        monkeypatch,
    ):
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {"app_eai_1": "REAL_APP_EAI_1"})
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             pad_stage_path="apps/myapp/current.zip", external_access=[])
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/OldRelease.zip",
                               resource_tier="large", external_access=["app_eai_1"])

        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202

        final = fake_registry.get_app("myapp")
        assert final.external_access == ["app_eai_1"]

        # Applied after the spec, via _compose_eai_names (PG_EAI first).
        assert fake_sf.calls_for("alter_service_spec")
        eai_args, kw = fake_sf.calls_for("set_service_external_access")[0]
        assert eai_args[1] == ["TEST_EAI", "REAL_APP_EAI_1"]

        history_rows = fake_deploy_history.list_for_app("myapp")
        assert history_rows[0]["operation"] == "rollback"
        assert history_rows[0]["external_access"] == ["app_eai_1"]

    def test_rollback_drops_since_unbound_slot_silently(
        self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, staged_pad,
        monkeypatch,
    ):
        # The history row recorded app_eai_1 as attached, but it's no longer
        # bound (consumer removed the reference) - rollback must not fail, it
        # just filters the slot out via _compose_eai_names.
        monkeypatch.setattr(main, "BOUND_EAI_SLOTS", {})
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/OldRelease.zip",
                               external_access=["app_eai_1"])

        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        eai_args, kw = fake_sf.calls_for("set_service_external_access")[0]
        assert eai_args[1] == ["TEST_EAI"]
