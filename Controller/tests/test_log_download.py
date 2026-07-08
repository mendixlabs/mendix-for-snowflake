from __future__ import annotations

import time as time_module

from app import main


class TestAppLogDownload:
    def test_start_and_ready(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_sf.logs = "the log body"

        resp = client.post("/apps/myapp/logs/download", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "PENDING"
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/apps/myapp/logs/download/{job_id}", headers=role_headers("OWNER_ROLE"))
        assert status_resp.status_code == 200
        assert status_resp.json() == {"status": "READY", "logs": "the log body", "error": None}

        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert kwargs["lines"] == main.LOG_DOWNLOAD_LINES
        assert kwargs["container"] == "mendix-app"

    def test_failure_reported_as_failed_not_502(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_sf.raise_on["get_service_logs"] = RuntimeError("access denied")

        resp = client.post("/apps/myapp/logs/download", headers=role_headers("OWNER_ROLE"))
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/apps/myapp/logs/download/{job_id}", headers=role_headers("OWNER_ROLE"))
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["status"] == "FAILED"
        assert body["logs"] is None
        assert "access denied" in body["error"]

    def test_unauthorized_role_gets_404_not_403(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Same existence-hiding behavior as the other per-app routes (_record_for_read).
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.post("/apps/myapp/logs/download", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 404

    def test_unknown_app_404(self, client, fake_sf, role_headers):
        resp = client.post("/apps/ghost/logs/download", headers=role_headers("ANY_ROLE"))
        assert resp.status_code == 404

    def test_job_id_scoped_to_its_own_app(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        fake_registry.add(make_record(name="otherapp", owner_role="OWNER_ROLE"))

        resp = client.post("/apps/myapp/logs/download", headers=role_headers("OWNER_ROLE"))
        job_id = resp.json()["job_id"]

        # Operator is authorized for otherapp too, but the job belongs to myapp.
        cross_resp = client.get(f"/apps/otherapp/logs/download/{job_id}", headers=role_headers("OWNER_ROLE"))
        assert cross_resp.status_code == 404

    def test_unknown_job_id_404(self, client, fake_sf, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/logs/download/nonexistent-job", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 404


class TestSystemLogDownload:
    def test_start_requires_privileged_role(self, client, fake_sf, role_headers):
        resp = client.post("/system/logs/controller/download", headers=role_headers("NORMAL_ROLE"))
        assert resp.status_code == 403

    def test_start_unknown_target_404(self, client, fake_sf, role_headers):
        resp = client.post("/system/logs/nonsense/download", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_happy_path_uses_correct_service_and_container(self, client, fake_sf, role_headers):
        fake_sf.logs = "sys log body"
        resp = client.post("/system/logs/controller/download", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/system/logs/controller/download/{job_id}", headers=role_headers("PRIV_ROLE"))
        assert status_resp.json() == {"status": "READY", "logs": "sys log body", "error": None}

        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_CONTROLLER"
        assert kwargs["container"] == "controller"

    def test_admin_ui_target_uses_streamlit_container(self, client, fake_sf, role_headers):
        resp = client.post("/system/logs/admin-ui/download", headers=role_headers("PRIV_ROLE"))
        job_id = resp.json()["job_id"]
        client.get(f"/system/logs/admin-ui/download/{job_id}", headers=role_headers("PRIV_ROLE"))
        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_ADMIN_UI"
        assert kwargs["container"] == "streamlit"

    def test_status_check_requires_privileged_role(self, client, fake_sf, role_headers):
        resp = client.post("/system/logs/controller/download", headers=role_headers("PRIV_ROLE"))
        job_id = resp.json()["job_id"]
        status_resp = client.get(f"/system/logs/controller/download/{job_id}", headers=role_headers("NORMAL_ROLE"))
        assert status_resp.status_code == 403

    def test_status_check_unknown_target_404(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/nonsense/download/some-job", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_job_scoped_to_its_own_target(self, client, fake_sf, role_headers):
        resp = client.post("/system/logs/controller/download", headers=role_headers("PRIV_ROLE"))
        job_id = resp.json()["job_id"]
        cross_resp = client.get(f"/system/logs/admin-ui/download/{job_id}", headers=role_headers("PRIV_ROLE"))
        assert cross_resp.status_code == 404


class TestLogJobPruning:
    def test_stale_finished_jobs_pruned_on_next_job_creation(
        self, client, fake_sf, fake_registry, make_record, role_headers, monkeypatch,
    ):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))

        resp = client.post("/apps/myapp/logs/download", headers=role_headers("OWNER_ROLE"))
        old_job_id = resp.json()["job_id"]
        assert client.get(
            f"/apps/myapp/logs/download/{old_job_id}", headers=role_headers("OWNER_ROLE"),
        ).json()["status"] == "READY"

        real_time = time_module.time()
        monkeypatch.setattr(main.time, "time", lambda: real_time + main._LOG_JOB_TTL_SECS + 1)

        # Any new job creation sweeps stale finished jobs.
        client.post("/apps/myapp/logs/download", headers=role_headers("OWNER_ROLE"))

        stale_check = client.get(f"/apps/myapp/logs/download/{old_job_id}", headers=role_headers("OWNER_ROLE"))
        assert stale_check.status_code == 404
