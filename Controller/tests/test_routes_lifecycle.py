from __future__ import annotations

import yaml

from app.models import HIDDEN_VALUE


class TestTriggerDeploy:
    def test_no_zip_staged_400(self, client, fake_sf, fake_registry, make_record, role_headers, staged_pad):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        # staged_pad fixture only points DEPLOY_STAGE_MOUNT at tmp_path; we never
        # call the stage() callable, so apps/myapp/ has no zip.
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 400

    def test_new_constant_with_no_default_422(self, client, fake_sf, fake_registry, make_record,
                                              role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"New.Const" = ""',
                             variables_text='"New.Const" = ${?NEW_CONST}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 422
        assert resp.json()["detail"]["missing"] == ["New.Const"]

    def test_happy_path_constants_changed(self, client, fake_sf, fake_registry, make_record,
                                          role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             constants={"Mod.Hidden": HIDDEN_VALUE})
        fake_registry.add(record)
        fake_sf.endpoints[record.service_name] = "https://live.example.com"
        zpath = make_pad_zip(
            defaults_text='"Mod.Hidden" = "unused"\n"Mod.New" = "world"',
            variables_text='"Mod.Hidden" = ${?MOD_HIDDEN}\n"Mod.New" = ${?MOD_NEW}',
        )
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        # Mod.Hidden already existed as HIDDEN and stays HIDDEN (unchanged) so no
        # secret write; Mod.New is new so it gets a secret write, and its
        # presence makes constants_changed True overall.
        secret_names = {a[0] for (a, k) in fake_sf.calls_for("create_or_replace_secret")}
        assert "TESTDB.MXAPP_MYAPP.MX_CONST_MOD_NEW" in secret_names
        assert "TESTDB.MXAPP_MYAPP.MX_CONST_MOD_HIDDEN" not in secret_names
        assert fake_sf.calls_for("alter_service_spec")

        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert final.pad_stage_path == "apps/myapp/current.zip"
        assert final.endpoint_url == "https://live.example.com"
        assert final.constants["Mod.New"] == "world"

    def test_unchanged_constants_still_rebuilds_spec(self, client, fake_sf, fake_registry, make_record,
                                                      role_headers, staged_pad, make_pad_zip):
        # Regression guard: PAD_STAGE_PATH must be refreshed even when constants
        # are unchanged, since the staged filename can still differ from the
        # previous deploy's. alter_service_spec (not suspend/resume) is the only
        # path that can update it, so it must always run.
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             constants={"Mod.Same": "same-value"})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.Same" = "same-value"',
                             variables_text='"Mod.Same" = ${?MOD_SAME}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_sf.calls_for("suspend_service") == []
        assert fake_sf.calls_for("resume_service") == []
        assert fake_sf.calls_for("alter_service_spec")
        assert fake_registry.get_app("myapp").last_deploy_status == "READY"
        assert fake_registry.get_app("myapp").pad_stage_path == "apps/myapp/current.zip"

    def test_redeploy_with_noncanonical_filename_updates_pad_stage_path(
        self, client, fake_sf, fake_registry, make_record, role_headers, staged_pad, make_pad_zip
    ):
        # The whole point of _resolve_staged_pad's newest-.zip fallback: a consumer
        # can `snow stage copy` their PAD under its own name without renaming it to
        # current.zip first. PAD_STAGE_PATH in the rebuilt spec must point at that
        # exact file, or the container's entrypoint (no fallback of its own) 404s.
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath, filename="MyReleasePad_20260706.zip")
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        alter_call = fake_sf.calls_for("alter_service_spec")[-1]
        spec = yaml.safe_load(alter_call[0][1])
        assert spec["spec"]["containers"][0]["env"]["PAD_STAGE_PATH"].endswith(
            "apps/myapp/MyReleasePad_20260706.zip"
        )
        assert fake_registry.get_app("myapp").pad_stage_path == "apps/myapp/MyReleasePad_20260706.zip"

    def test_poll_failure_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                       role_headers, staged_pad, make_pad_zip, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_alter_service_spec_exception_marks_failed_no_escape(self, client, fake_sf, fake_registry, make_record,
                                                                  role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202  # exception happens in the background task, not the request
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_deploy_persists_detected_user_roles(self, client, fake_sf, fake_registry, make_record,
                                                 role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        zpath = make_pad_zip(
            defaults_text='"Mod.New" = "world"',
            variables_text='"Mod.New" = ${?MOD_NEW}',
            metadata_json={"Roles": {
                "uuid-1": {"Name": "User"},
                "uuid-2": {"Name": "Administrator"},
            }},
        )
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert sorted(final.user_roles) == ["Administrator", "User"]

    def test_deploy_without_metadata_json_stores_empty_user_roles(self, client, fake_sf, fake_registry,
                                                                   make_record, role_headers, staged_pad,
                                                                   make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert final.user_roles == []


class TestRoleMappingSurvivesRestarts:
    """Regression guard for the six _build_spec call sites (plan section 5a): missing
    one silently strips MX_ROLE_MAPPING from the spec on the next unrelated restart."""

    def _mx_role_mapping_env(self, fake_sf):
        alter_call = fake_sf.calls_for("alter_service_spec")[-1]
        parsed = yaml.safe_load(alter_call[0][1])
        return parsed["spec"]["containers"][0]["env"].get("MX_ROLE_MAPPING")

    def test_update_constants_keeps_role_mapping(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 202
        assert self._mx_role_mapping_env(fake_sf) is not None

    def test_update_spec_keeps_role_mapping(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 202
        assert self._mx_role_mapping_env(fake_sf) is not None

    def test_trigger_deploy_constants_changed_keeps_role_mapping(self, client, fake_sf, fake_registry,
                                                                  make_record, role_headers, staged_pad,
                                                                  make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={},
                             role_mapping={"ROLE_A": "Administrator"})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert self._mx_role_mapping_env(fake_sf) is not None


class TestUpdateConstants:
    def test_all_hidden_unchanged(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Discrepancy vs the plan: the route decorator fixes status_code=202 for
        # every normal (non-exception) return, including this early-exit
        # "UNCHANGED" branch, so it comes back 202, not 200.
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      constants={"Mod.A": HIDDEN_VALUE}))
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": HIDDEN_VALUE}})
        assert resp.status_code == 202
        assert resp.json() == {"status": "UNCHANGED"}
        assert fake_sf.calls_for("create_or_replace_secret") == []

    def test_new_name_with_hidden_422(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", constants={}))
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.New": HIDDEN_VALUE}})
        assert resp.status_code == 422

    def test_changed_value_202_secret_written_synchronously(self, client, fake_sf, fake_registry,
                                                             make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"})
        fake_registry.add(record)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 202
        assert fake_sf.calls_for("create_or_replace_secret") == [
            (("TESTDB.MXAPP_MYAPP.MX_CONST_MOD_A", "new-value"), {})
        ]
        assert fake_registry.get_app("myapp").constants == {"Mod.A": "new-value"}

    def test_failed_restart_still_persists_constants(self, client, fake_sf, fake_registry, make_record,
                                                      role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"})
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.constants == {"Mod.A": "new-value"}
        assert final.last_deploy_status == "FAILED"

    def test_invalid_constant_name_422(self, client, role_headers):
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"bad name": "v"}})
        assert resp.status_code == 422


class TestUpdateSpec:
    def test_both_fields_none_400(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"), json={})
        assert resp.status_code == 400

    def test_tier_change_persisted(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium"))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.resource_tier == "large"
        assert final.last_deploy_status == "READY"

    def test_caller_rights_off_to_on_updates_registry(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=False))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"use_caller_rights": True})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.use_caller_rights is True


class TestSuspendResume:
    def test_suspend_transient_then_success(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "SUSPENDING"}
        statuses = [f.get("last_deploy_status") for (_, f) in fake_registry.updates]
        assert "SUSPENDING" in statuses
        assert fake_registry.get_app("myapp").last_deploy_status == "SUSPENDED"

    def test_suspend_poll_failure_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                               role_headers, monkeypatch):
        # Default status "RUNNING" never matches the "SUSPENDED" poll target,
        # but _poll_status(..., timeout_secs=120) would burn 120 real seconds
        # discovering that; patch it directly instead.
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_suspend_sf_exception_marks_failed(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.raise_on["suspend_service"] = RuntimeError("boom")
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"

    def test_resume_transient_then_success(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "RESUMING"}
        statuses = [f.get("last_deploy_status") for (_, f) in fake_registry.updates]
        assert "RESUMING" in statuses
        assert fake_registry.get_app("myapp").last_deploy_status == "READY"

    def test_resume_sf_exception_marks_failed(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.raise_on["resume_service"] = RuntimeError("boom")
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").last_deploy_status == "FAILED"
