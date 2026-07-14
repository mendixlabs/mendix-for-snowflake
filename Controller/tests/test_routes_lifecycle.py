from __future__ import annotations

import pytest
import yaml

from app.models import HIDDEN_VALUE


class TestTriggerDeploy:
    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_unknown_app_404(self, client, fake_registry, make_record, role_headers):
        resp = client.post("/apps/ghost/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404

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

    def test_success_stamps_platform_image_and_clears_flag(self, client, fake_sf, fake_registry, make_record,
                                                            role_headers, staged_pad, make_pad_zip):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={},
                             platform_image="/repo/mendix-base:old", platform_update_available=True)
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.platform_image == main.MENDIX_BASE_IMAGE
        assert final.platform_update_available is False

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

    def test_poll_timeout_records_status_detail_and_op(self, client, fake_sf, fake_registry, make_record,
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
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "deploy"
        assert final.status_detail == "Timed out waiting for RUNNING after 300s"

    def test_running_but_containers_never_ready_marks_failed(self, client, fake_sf, fake_registry, make_record,
                                                              role_headers, staged_pad, make_pad_zip, monkeypatch):
        # Regression guard for the live patch-25 bug: SPCS can report a service
        # RUNNING as soon as a container starts, before its readinessProbe
        # passes (sf.show_service_containers's docstring). A crash-looping
        # container must fail the deploy, not be declared READY on the
        # transient service-level RUNNING alone.
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "FAILED",
             "message": "User application error, check container logs"},
        ]
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        # Fake the clock only now, after the PAD zip is written (zipfile itself
        # calls time.time() for its own file timestamps, so patching it earlier
        # would corrupt the archive).
        state = {"t": 0.0}
        monkeypatch.setattr(main.time, "time", lambda: state["t"])
        monkeypatch.setattr(main.time, "sleep", lambda secs: state.update(t=state["t"] + secs))
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "deploy"
        assert "User application error, check container logs" in final.status_detail

    def test_all_containers_ready_succeeds_with_no_status_detail(self, client, fake_sf, fake_registry, make_record,
                                                                  role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "READY", "message": None},
        ]
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert final.status_detail is None

    def test_exception_records_truncated_status_detail_and_op(self, client, fake_sf, fake_registry, make_record,
                                                               role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.failed_operation == "deploy"
        assert final.status_detail == "boom"

    def test_success_clears_prior_status_detail_and_failed_operation(self, client, fake_sf, fake_registry,
                                                                      make_record, role_headers, staged_pad,
                                                                      make_pad_zip):
        # Regression guard: a redeploy that succeeds must wipe out whatever
        # FAILED-run detail a previous attempt left behind - otherwise a stale
        # "Failed during deploy: ..." caption would keep showing in the UI for
        # an app that is actually READY again.
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={},
                             status_detail="Timed out waiting for RUNNING after 300s",
                             failed_operation="deploy")
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"',
                             variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert final.status_detail is None
        assert final.failed_operation is None


class TestRoleMappingSurvivesRestarts:
    """Regression guard for the six _build_spec call sites (plan section 5a): missing
    one silently strips MX_ROLE_MAPPING from the spec on the next unrelated restart."""

    def _mx_role_mapping_env(self, fake_sf):
        alter_call = fake_sf.calls_for("alter_service_spec")[-1]
        parsed = yaml.safe_load(alter_call[0][1])
        return parsed["spec"]["containers"][0]["env"].get("MX_ROLE_MAPPING")

    def test_update_constants_keeps_role_mapping(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             role_mapping={"ROLE_A": "Administrator"},
                             pad_stage_path="apps/myapp/current.zip")
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

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"}))
        resp = client.put("/apps/myapp/constants", headers=role_headers("OTHER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 403

    def test_changed_value_202_secret_written_synchronously(self, client, fake_sf, fake_registry,
                                                             make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             pad_stage_path="apps/myapp/current.zip")
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
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.constants == {"Mod.A": "new-value"}
        assert final.last_deploy_status == "FAILED"

    def test_no_pad_deployed_yet_409(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Regression guard for the 2026-07-07 dry-run finding: saving constants
        # before ever calling trigger-deploy must not silently restart the
        # service against whatever PAD happens to be staged at the conventional
        # apps/{name}/current.zip fallback path - that path skips _prepare_deploy,
        # so pad_stage_path and user_roles never get recorded even though the
        # restart can appear to succeed.
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             pad_stage_path=None)
        fake_registry.add(record)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 409
        assert fake_sf.calls_for("create_or_replace_secret") == []
        assert fake_sf.calls_for("alter_service_spec") == []

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

    def test_success_stamps_platform_image_and_clears_flag(self, client, fake_sf, fake_registry,
                                                            make_record, role_headers):
        # _run_update_spec always rebuilds the spec, so a plain tier/caller-rights
        # change also picks up the current platform image - and clears a
        # previously-set staleness flag, since the freshly-applied spec is current.
        from app import main
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                                      platform_image="/repo/mendix-base:old",
                                      platform_update_available=True))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.platform_image == main.MENDIX_BASE_IMAGE
        assert final.platform_update_available is False

    def test_caller_rights_off_to_on_updates_registry(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", use_caller_rights=False))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"use_caller_rights": True})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.use_caller_rights is True

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OTHER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 403

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.put("/apps/ghost/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 404

    def test_alter_service_spec_exception_marks_failed_no_escape(self, client, fake_sf, fake_registry,
                                                                  make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 202  # exception happens in the background task, not the request
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "spec"
        assert final.status_detail == "boom"

    def test_success_clears_prior_status_detail_and_failed_operation(self, client, fake_sf, fake_registry,
                                                                      make_record, role_headers):
        # _run_update_spec's on_success bypasses _stamp_deploy_success (its own
        # inline registry.update_app), so it needs the same clearing.
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                                     status_detail="boom", failed_operation="spec"))
        resp = client.put("/apps/myapp/spec", headers=role_headers("OWNER_ROLE"),
                          json={"resource_tier": "large"})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.status_detail is None
        assert final.failed_operation is None


class TestPlatformUpdate:
    def test_not_flagged_409(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_update_available=False))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert fake_sf.calls_for("alter_service_spec") == []

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_update_available=True))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_unknown_app_404(self, client, fake_registry, role_headers):
        resp = client.post("/apps/ghost/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404

    def test_no_pad_deployed_yet_409(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_update_available=True, pad_stage_path=None))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert fake_sf.calls_for("alter_service_spec") == []

    def test_flagged_triggers_background_respec_unchanged_tier_and_caller(
            self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      resource_tier="large", use_caller_rights=True,
                                      platform_image="/repo/mendix-base:old",
                                      platform_update_available=True))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}
        assert fake_sf.calls_for("alter_service_spec")
        final = fake_registry.get_app("myapp")
        assert final.resource_tier == "large"
        assert final.use_caller_rights is True

    def test_success_clears_flag_and_stamps_image(self, client, fake_sf, fake_registry, make_record,
                                                   role_headers):
        from app import main
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_image="/repo/mendix-base:old",
                                      platform_update_available=True))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.platform_image == main.MENDIX_BASE_IMAGE
        assert final.platform_update_available is False
        assert final.last_deploy_status == "READY"

    def test_transient_blocked_409(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_update_available=True,
                                      last_deploy_status="DEPLOYING"))
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert fake_sf.calls_for("alter_service_spec") == []

    def test_failure_marks_failed_operation(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      platform_update_available=True))
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.post("/apps/myapp/platform-update", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202  # exception happens in the background task, not the request
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "platform_update"
        assert final.status_detail == "boom"


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
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "suspend"
        assert final.status_detail == "boom"

    def test_suspend_poll_timeout_records_detail(self, client, fake_sf, fake_registry, make_record,
                                                 role_headers, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.failed_operation == "suspend"
        assert final.status_detail == "Timed out waiting for SUSPENDED after 120s"

    def test_suspend_success_clears_prior_status_detail(self, client, fake_sf, fake_registry, make_record,
                                                        role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             status_detail="boom", failed_operation="deploy")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.status_detail is None
        assert final.failed_operation is None

    def test_suspend_success_does_not_stamp_platform_image(self, client, fake_sf, fake_registry,
                                                            make_record, role_headers):
        # Suspend never rebuilds the spec, so it must not touch platform_image /
        # platform_update_available either way.
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             platform_image="/repo/mendix-base:old", platform_update_available=True)
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.platform_image == "/repo/mendix-base:old"
        assert final.platform_update_available is True

    def test_suspend_never_calls_show_service_containers(self, client, fake_sf, fake_registry, make_record,
                                                          role_headers):
        # SUSPENDED has no container-readiness concept - _poll_ready must
        # return on the service-level match alone, same as before this file's
        # container-readiness change.
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_sf.calls_for("show_service_containers") == []

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
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "resume"
        assert final.status_detail == "boom"

    def test_resume_success_clears_prior_status_detail(self, client, fake_sf, fake_registry, make_record,
                                                        role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             status_detail="boom", failed_operation="suspend")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.status_detail is None
        assert final.failed_operation is None

    def test_resume_success_does_not_stamp_platform_image(self, client, fake_sf, fake_registry,
                                                           make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             platform_image="/repo/mendix-base:old", platform_update_available=True)
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.platform_image == "/repo/mendix-base:old"
        assert final.platform_update_available is True

    def test_resume_running_but_containers_never_ready_marks_failed(self, client, fake_sf, fake_registry,
                                                                     make_record, role_headers, monkeypatch):
        # Resume targets RUNNING like every restart, so a crash-loop right
        # after resume must not be declared READY either.
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "FAILED",
             "message": "User application error, check container logs"},
        ]
        state = {"t": 0.0}
        monkeypatch.setattr(main.time, "time", lambda: state["t"])
        monkeypatch.setattr(main.time, "sleep", lambda secs: state.update(t=state["t"] + secs))
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "resume"
        assert "User application error, check container logs" in final.status_detail


class TestSpecRebuildRequiresPad:
    """Every endpoint that rebuilds and re-applies the service spec must refuse to
    run before a PAD has ever been deployed (pad_stage_path is None). Otherwise
    _build_spec falls back to the apps/{name}/current.zip path and could restart
    the service without running _prepare_deploy (recording pad_stage_path /
    user_roles). Regression for the 2026-07-07 dry-run finding, extended from the
    constants endpoint to its spec / license / role-mapping siblings.
    """

    @pytest.mark.parametrize("method,path,body", [
        ("put", "/apps/myapp/spec", {"resource_tier": "large"}),
        ("put", "/apps/myapp/license", {"license_id": "LIC-1", "license_key": "k"}),
        ("delete", "/apps/myapp/license", None),
        ("put", "/apps/myapp/role-mapping", {"role_mapping": {"my_role": "Administrator"}}),
        ("delete", "/apps/myapp/role-mapping", None),
    ])
    def test_no_pad_deployed_yet_409_no_side_effects(
            self, client, fake_sf, fake_registry, make_record, role_headers, method, path, body):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      pad_stage_path=None))
        kwargs = {"headers": role_headers("OWNER_ROLE")}
        if body is not None:
            kwargs["json"] = body
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 409
        # The guard runs before any secret write or service restart.
        assert fake_sf.calls_for("alter_service_spec") == []
        assert fake_sf.calls_for("create_or_replace_secret") == []


class TestTransientStateGuard:
    """Server-side guard (Q1): a mutation route must refuse a second call while a
    prior one is still in flight, instead of racing it on ALTER SERVICE /
    last_deploy_status. Previously this was enforced only client-side, by the
    Admin UI disabling buttons for _TRANSIENT statuses.
    """

    @pytest.mark.parametrize("transient_status", ["DEPLOYING", "SUSPENDING", "RESUMING"])
    def test_suspend_blocked_while_transient(self, client, fake_sf, fake_registry, make_record,
                                             role_headers, transient_status):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      last_deploy_status=transient_status))
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert fake_sf.calls_for("suspend_service") == []

    def test_suspend_succeeds_once_status_is_terminal_again(self, client, fake_sf, fake_registry,
                                                             make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", last_deploy_status="DEPLOYING")
        fake_registry.add(record)
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409

        # The prior deploy finished; the app is back to a terminal status.
        fake_registry.update_app("myapp", {"last_deploy_status": "READY"})
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "SUSPENDING"}
        assert fake_sf.calls_for("suspend_service")

    def test_trigger_deploy_blocked_while_transient(self, client, fake_sf, fake_registry, make_record,
                                                     role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE",
                                      last_deploy_status="SUSPENDING"))
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
