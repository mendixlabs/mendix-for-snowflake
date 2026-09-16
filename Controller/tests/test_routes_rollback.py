from __future__ import annotations


class TestHistoryWrites:
    """_run_lifecycle_task records a deploy_history row on success/failure for
    every op that threads a `record` kwarg (deploy/constants/spec/license/
    role_mapping/platform_update/rollback) - suspend/resume never pass one."""

    def test_deploy_success_writes_ready_row(self, client, fake_sf, fake_registry, fake_deploy_history,
                                             make_record, role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"', variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["operation"] == "deploy"
        assert rows[0]["status"] == "READY"

    def test_deploy_poll_timeout_writes_failed_row(self, client, fake_sf, fake_registry, fake_deploy_history,
                                                    make_record, role_headers, staged_pad, make_pad_zip, monkeypatch):
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        monkeypatch.setattr(main, "_poll_status", lambda *a, **k: False)
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"', variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["operation"] == "deploy"
        assert rows[0]["status"] == "FAILED"

    def test_deploy_exception_writes_failed_row(self, client, fake_sf, fake_registry, fake_deploy_history,
                                                 make_record, role_headers, staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"', variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["status"] == "FAILED"
        assert rows[0]["detail"] == "boom"

    def test_deploy_containers_never_ready_writes_failed_row(self, client, fake_sf, fake_registry,
                                                              fake_deploy_history, make_record, role_headers,
                                                              staged_pad, make_pad_zip, monkeypatch):
        # Regression guard for the live patch-25 bug: a service-level RUNNING
        # with a crash-looping container must land a FAILED history row, not a
        # READY one - otherwise it becomes deploy_history.last_success and a
        # later no-body rollback resolves right back onto the broken config.
        from app import main
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "FAILED",
             "message": "User application error, check container logs"},
        ]
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"', variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        # Fake the clock only now, after the PAD zip is written (zipfile itself
        # calls time.time() for its own file timestamps, so patching it earlier
        # would corrupt the archive).
        state = {"t": 0.0}
        monkeypatch.setattr(main.time, "time", lambda: state["t"])
        monkeypatch.setattr(main.time, "sleep", lambda secs: state.update(t=state["t"] + secs))
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["operation"] == "deploy"
        assert rows[0]["status"] == "FAILED"
        assert fake_deploy_history.last_success("myapp") is None

    def test_constants_update_writes_history_row(self, client, fake_sf, fake_registry, fake_deploy_history,
                                                  make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={"Mod.A": "old"},
                             pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        resp = client.put("/apps/myapp/constants", headers=role_headers("OWNER_ROLE"),
                          json={"constants": {"Mod.A": "new-value"}})
        assert resp.status_code == 202
        rows = fake_deploy_history.list_for_app("myapp")
        assert len(rows) == 1
        assert rows[0]["operation"] == "constants"
        assert rows[0]["status"] == "READY"
        assert rows[0]["constant_names"] == ["Mod.A"]

    def test_suspend_writes_no_history(self, client, fake_sf, fake_registry, fake_deploy_history,
                                       make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_deploy_history.rows == []

    def test_resume_writes_no_history(self, client, fake_sf, fake_registry, fake_deploy_history,
                                      make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        resp = client.post("/apps/myapp/resume", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_deploy_history.rows == []

    def test_history_write_failure_does_not_fail_the_deploy(self, client, fake_sf, fake_registry,
                                                             fake_deploy_history, make_record, role_headers,
                                                             staged_pad, make_pad_zip):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", constants={})
        fake_registry.add(record)
        fake_deploy_history.raise_on_record = RuntimeError("history db unreachable")
        zpath = make_pad_zip(defaults_text='"Mod.New" = "world"', variables_text='"Mod.New" = ${?MOD_NEW}')
        staged_pad("myapp", src_zip=zpath)
        resp = client.post("/apps/myapp/trigger-deploy", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        # The deploy itself must still succeed even though recording its history blew up.
        assert fake_registry.get_app("myapp").last_deploy_status == "READY"


class TestGetAppHistory:
    def test_stranger_404(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/history", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 404

    def test_returns_history_list(self, client, fake_registry, fake_deploy_history, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_deploy_history.rows.append({
            "app_name": "myapp", "operation": "deploy", "status": "READY", "detail": None,
            "pad_stage_path": "apps/myapp/current.zip", "resource_tier": "medium",
            "use_caller_rights": False, "constant_names": [], "license_id": None,
            "role_mapping": {}, "external_access": [],
        })
        resp = client.get("/apps/myapp/history", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json()["history"][0]["operation"] == "deploy"


class TestRollback:
    def _seed_history_row(self, fake_deploy_history, **overrides):
        row = {
            "app_name": "myapp", "operation": "deploy", "status": "READY", "detail": None,
            "pad_stage_path": "apps/myapp/current.zip", "resource_tier": "medium",
            "use_caller_rights": False, "constant_names": [], "license_id": None,
            "role_mapping": {}, "external_access": [],
        }
        row.update(overrides)
        fake_deploy_history.rows.append(row)

    def test_unknown_app_404(self, client, role_headers):
        resp = client.post("/apps/ghost/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404

    def test_stranger_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_transient_409(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE", last_deploy_status="DEPLOYING"))
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409

    def test_no_history_404(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404
        assert "no successful deployment" in resp.json()["detail"].lower()

    def test_identical_config_409(self, client, fake_registry, fake_deploy_history, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE",
                             pad_stage_path="apps/myapp/current.zip", resource_tier="medium",
                             use_caller_rights=False, license_id=None, role_mapping={})
        fake_registry.add(record)
        self._seed_history_row(fake_deploy_history)  # identical to the record above
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()

    def test_missing_pad_409(self, client, fake_registry, fake_deploy_history, make_record, role_headers,
                             staged_pad):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium")
        fake_registry.add(record)
        # staged_pad only points DEPLOY_STAGE_MOUNT at tmp_path; apps/myapp/old.zip
        # is never actually staged there.
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/old.zip", resource_tier="large")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 409
        assert "no longer on the stage" in resp.json()["detail"]

    def test_failed_app_can_roll_back_even_when_recorded_config_matches(
            self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, staged_pad):
        # Regression guard: a failed deploy/spec change only overwrites the
        # registry's pad_stage_path/resource_tier/etc. in on_success (see
        # _run_deploy/_run_update_spec), so a FAILED app's registry row still
        # reflects its last-GOOD config - identical to last_success's target by
        # construction. The identical-config 409 must not fire here, or the
        # exact "kill a deploy -> FAILED -> rollback -> READY" recovery this
        # endpoint exists for would be permanently blocked.
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             use_caller_rights=False, license_id=None, role_mapping={},
                             pad_stage_path="apps/myapp/current.zip", last_deploy_status="FAILED")
        fake_registry.add(record)
        staged_pad("myapp", filename="current.zip")
        self._seed_history_row(fake_deploy_history)  # identical to the record above
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

    def test_happy_path_applies_history_values_and_current_constants(
            self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, staged_pad):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             use_caller_rights=False, license_id=None, role_mapping={},
                             constants={"Mod.A": "current-value"}, pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        self._seed_history_row(
            fake_deploy_history,
            pad_stage_path="apps/myapp/OldRelease.zip",
            resource_tier="large",
            use_caller_rights=True,
            license_id="LIC-1",
            role_mapping={"ROLE_A": "Administrator"},
        )
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json() == {"status": "DEPLOYING"}

        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "READY"
        assert final.resource_tier == "large"
        assert final.use_caller_rights is True
        assert final.license_id == "LIC-1"
        assert final.role_mapping == {"ROLE_A": "Administrator"}
        assert final.pad_stage_path == "apps/myapp/OldRelease.zip"
        # Constant VALUES are never restored from history - only names are ever
        # snapshotted there. The app keeps whatever is currently in the registry.
        assert final.constants == {"Mod.A": "current-value"}
        assert fake_sf.calls_for("create_or_replace_secret") == []
        assert fake_sf.calls_for("alter_service_spec")

        history_rows = fake_deploy_history.list_for_app("myapp")
        assert history_rows[0]["operation"] == "rollback"
        assert history_rows[0]["status"] == "READY"

    def test_no_body_skips_a_failed_entry_and_uses_older_good_one(
            self, client, fake_sf, fake_registry, fake_deploy_history, make_record, role_headers, staged_pad):
        # Regression guard for the live patch-25 bug: once a RUNNING-but-
        # containers-never-ready deploy is correctly recorded FAILED (see
        # test_deploy_containers_never_ready_writes_failed_row above),
        # deploy_history.last_success must skip right over it and a no-body
        # rollback must resolve to the older, genuinely-good entry instead of
        # 409ing with "already running this configuration".
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             pad_stage_path="apps/myapp/bad.zip", last_deploy_status="FAILED")
        fake_registry.add(record)
        staged_pad("myapp", filename="good.zip")
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/good.zip", resource_tier="large")
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/bad.zip", resource_tier="medium",
                               status="FAILED")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.resource_tier == "large"
        assert final.pad_stage_path == "apps/myapp/good.zip"

    def test_failure_marks_failed_operation_rollback(self, client, fake_sf, fake_registry, fake_deploy_history,
                                                      make_record, role_headers, staged_pad):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium")
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        self._seed_history_row(fake_deploy_history, pad_stage_path="apps/myapp/OldRelease.zip",
                               resource_tier="large")
        fake_sf.raise_on["alter_service_spec"] = RuntimeError("boom")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.last_deploy_status == "FAILED"
        assert final.failed_operation == "rollback"
        assert final.status_detail == "boom"


class TestRollbackByEntryId:
    """POST /apps/{name}/rollback with a {"entry_id": N} body targets a specific
    history row instead of the newest READY one - same downstream checks
    (identical-config 409, missing-PAD 409), just against a caller-chosen row."""

    def _seed(self, fake_deploy_history, **overrides):
        row = {
            "id": 1, "app_name": "myapp", "operation": "deploy", "status": "READY", "detail": None,
            "pad_stage_path": "apps/myapp/current.zip", "resource_tier": "medium",
            "use_caller_rights": False, "constant_names": [], "license_id": None,
            "role_mapping": {}, "external_access": [],
        }
        row.update(overrides)
        fake_deploy_history.rows.append(row)

    def test_happy_path_targets_chosen_entry(self, client, fake_sf, fake_registry, fake_deploy_history,
                                              make_record, role_headers, staged_pad):
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        # Two READY rows; entry_id picks the older one (id=1), not the newest (id=2).
        self._seed(fake_deploy_history, id=1, pad_stage_path="apps/myapp/OldRelease.zip", resource_tier="large")
        self._seed(fake_deploy_history, id=2, pad_stage_path="apps/myapp/current.zip", resource_tier="medium")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"), json={"entry_id": 1})
        assert resp.status_code == 202
        final = fake_registry.get_app("myapp")
        assert final.resource_tier == "large"
        assert final.pad_stage_path == "apps/myapp/OldRelease.zip"

    def test_entry_belonging_to_another_app_404(self, client, fake_registry, fake_deploy_history,
                                                 make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        self._seed(fake_deploy_history, id=99, app_name="otherapp")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"), json={"entry_id": 99})
        assert resp.status_code == 404

    def test_missing_entry_404(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"), json={"entry_id": 404})
        assert resp.status_code == 404

    def test_failed_entry_409(self, client, fake_registry, fake_deploy_history, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        self._seed(fake_deploy_history, id=5, status="FAILED")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"), json={"entry_id": 5})
        assert resp.status_code == 409
        assert "not ready" in resp.json()["detail"].lower()

    def test_no_body_falls_back_to_last_success(self, client, fake_sf, fake_registry, fake_deploy_history,
                                                 make_record, role_headers, staged_pad):
        # Regression guard: adding the optional entry_id body must not disturb
        # the existing no-body path (the admin UI's default "last success" button).
        record = make_record(name="myapp", owner_role="OWNER_ROLE", resource_tier="medium",
                             pad_stage_path="apps/myapp/current.zip")
        fake_registry.add(record)
        staged_pad("myapp", filename="OldRelease.zip")
        self._seed(fake_deploy_history, pad_stage_path="apps/myapp/OldRelease.zip", resource_tier="large")
        resp = client.post("/apps/myapp/rollback", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert fake_registry.get_app("myapp").resource_tier == "large"
