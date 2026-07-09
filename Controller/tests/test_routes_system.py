from __future__ import annotations


class TestSystemLogs:
    def test_unknown_target_404(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/nonsense", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_get_service_logs_failure_502(self, client, fake_sf, role_headers):
        fake_sf.raise_on["get_service_logs"] = RuntimeError("access denied")
        resp = client.get("/system/logs/controller", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 502
        assert "access denied" in resp.json()["detail"]

    def test_valid_target_returns_logs(self, client, fake_sf, role_headers):
        fake_sf.logs = "the log body"
        resp = client.get("/system/logs/controller", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"logs": "the log body"}
        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_CONTROLLER"
        assert kwargs["container"] == "controller"

    def test_admin_ui_target_uses_streamlit_container(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/admin-ui", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_ADMIN_UI"
        assert kwargs["container"] == "streamlit"


class TestGetComputePool:
    def test_none_pool_404(self, client, fake_sf, role_headers):
        fake_sf.compute_pool = None
        resp = client.get("/system/compute-pool", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_present_pool_passthrough(self, client, fake_sf, role_headers):
        resp = client.get("/system/compute-pool", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == fake_sf.compute_pool


class TestGetPgInfo:
    def test_returns_host_and_port(self, client, fake_sf, role_headers):
        resp = client.get("/system/pg-info", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"host": "pg.test.local", "port": "5432"}


class TestUpdateComputePool:
    def test_all_none_body_400(self, client, fake_sf, role_headers):
        resp = client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"), json={})
        assert resp.status_code == 400
        assert fake_sf.calls_for("alter_compute_pool") == []

    def test_partial_body_alters_and_returns_refreshed_pool(self, client, fake_sf, role_headers):
        resp = client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"),
                            json={"min_nodes": 2})
        assert resp.status_code == 200
        args, kwargs = fake_sf.calls_for("alter_compute_pool")[0]
        assert args[0] == "TEST_POOL"
        assert kwargs == {"min_nodes": 2, "max_nodes": None, "auto_suspend_secs": None}
        assert resp.json() == fake_sf.compute_pool

    def test_out_of_bounds_422(self, client, fake_sf, role_headers):
        resp = client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"),
                            json={"min_nodes": 99})
        assert resp.status_code == 422
        assert fake_sf.calls_for("alter_compute_pool") == []

    def test_alter_failure_returns_502(self, client, fake_sf, role_headers):
        # T2: a Snowflake failure during the alter surfaces as 502, not a raw 500.
        fake_sf.raise_on["alter_compute_pool"] = RuntimeError("pool busy")
        resp = client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"),
                            json={"min_nodes": 2})
        assert resp.status_code == 502
        assert "compute pool" in resp.json()["detail"]
