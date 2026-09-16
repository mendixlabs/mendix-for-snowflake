from __future__ import annotations

from app import main, progress


class TestProgressModule:
    """Unit tests for the bare in-memory dict (progress.py) - no FastAPI/registry
    involved, mirroring log_jobs.py's own module being tested standalone."""

    def test_get_missing_returns_none(self):
        assert progress.get_progress("ghost") is None

    def test_set_then_get(self):
        progress.set_progress("myapp", "applying changes")
        assert progress.get_progress("myapp") == "applying changes"

    def test_case_insensitive_key(self):
        progress.set_progress("MyApp", "applying changes")
        assert progress.get_progress("myapp") == "applying changes"
        assert progress.get_progress("MYAPP") == "applying changes"

    def test_clear_removes_entry(self):
        progress.set_progress("myapp", "applying changes")
        progress.clear_progress("myapp")
        assert progress.get_progress("myapp") is None

    def test_clear_missing_is_a_noop(self):
        progress.clear_progress("never-set")  # must not raise


class TestProgressDuringLifecycleTask:
    """Integration: _run_lifecycle_task (main.py) drives progress.py end to end
    through a real route + background task, not just direct calls into
    progress.py."""

    def test_set_during_run_then_cleared_on_success(self, client, fake_sf, fake_registry, make_record,
                                                     role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        # TestClient runs the background task synchronously before returning, so
        # by the time the response comes back the task (success or failure) has
        # already finished and cleared its own progress entry.
        assert progress.get_progress("myapp") is None

    def test_set_during_run_then_cleared_on_failure(self, client, fake_sf, fake_registry, make_record,
                                                     role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.raise_on["suspend_service"] = RuntimeError("boom")
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert progress.get_progress("myapp") is None

    def test_phases_observed_applying_then_waiting(self, client, fake_sf, fake_registry, make_record,
                                                    role_headers, monkeypatch):
        # Fake the clock (same pattern as TestPollStatus in test_main_helpers.py)
        # so two non-matching polls before SUSPENDED happen without a real sleep,
        # and spy on set_progress (delegating through to the real dict) to
        # observe the phase text _run_lifecycle_task writes at each step.
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.status_queue[record.service_name] = ["STARTING", "STARTING", "SUSPENDED"]

        state = {"t": 0.0}
        monkeypatch.setattr(main.time, "time", lambda: state["t"])

        def fake_sleep(secs):
            state["t"] += secs

        monkeypatch.setattr(main.time, "sleep", fake_sleep)

        seen: list[str] = []
        real_set = progress.set_progress

        def spy_set(name, text):
            seen.append(text)
            real_set(name, text)

        monkeypatch.setattr(main.progress, "set_progress", spy_set)

        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert seen == [
            "applying changes",
            "waiting for SUSPENDED (0s)",
            "waiting for SUSPENDED (10s)",
        ]
        # Cleared for real (not just the spy) once the task finished.
        assert progress.get_progress("myapp") is None


class TestGetProgressEndpoint:
    def test_no_task_running_returns_null(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/progress", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"progress": None}

    def test_returns_current_progress_text(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        progress.set_progress("myapp", "waiting for RUNNING (30s)")
        resp = client.get("/apps/myapp/progress", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"progress": "waiting for RUNNING (30s)"}

    def test_privileged_reads_ok(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/progress", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200

    def test_stranger_gets_404_not_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/progress", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 404

    def test_nonexistent_app_404(self, client, fake_registry, role_headers):
        resp = client.get("/apps/ghost/progress", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_no_warehouse_status_query(self, client, fake_sf, fake_registry, make_record, role_headers):
        # The progress value itself must never trigger a service-status /
        # warehouse-backed query - that's the whole point of keeping it in
        # memory instead of the registry.
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/progress", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert fake_sf.calls_for("show_service_status") == []
        assert fake_sf.calls_for("show_all_service_statuses") == []
