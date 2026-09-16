from __future__ import annotations

import json

from app import egress_watch


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


class TestGetEgressStatus:
    def test_privileged_reads_ok(self, client, fake_sf, role_headers):
        resp = client.get("/system/egress-status", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200

    def test_non_privileged_403(self, client, fake_sf, role_headers):
        resp = client.get("/system/egress-status", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 403

    def test_anonymous_403(self, client, fake_sf):
        resp = client.get("/system/egress-status")
        assert resp.status_code == 403

    def test_null_safe_before_first_iteration(self, client, fake_sf, role_headers):
        # fake_sf.config starts empty - the background loop is a no-op in this
        # suite (see conftest's _noop_egress_loop), so this is exactly the
        # "loop hasn't run yet" state a fresh install would see.
        resp = client.get("/system/egress-status", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {
            "min_expiry": None,
            "days_remaining": None,
            "ranges": [],
            "acknowledged_through": None,
            "alert_integration": None,
            "alert_recipients": [],
        }

    def test_reflects_persisted_state(self, client, fake_sf, role_headers):
        fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] = "2026-09-07T00:00:00+00:00"
        fake_sf.config[egress_watch.CONFIG_RANGES] = json.dumps(
            [{"ipv4_prefix": "1.2.3.0/24", "effective": "2026-01-01T00:00:00+00:00",
              "expires": "2026-09-07T00:00:00+00:00"}]
        )
        fake_sf.config[egress_watch.CONFIG_ACK_THROUGH] = "2026-09-01"
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        resp = client.get("/system/egress-status", headers=role_headers("PRIV_ROLE"))
        body = resp.json()
        assert body["min_expiry"] == "2026-09-07T00:00:00+00:00"
        assert body["ranges"] == [{"ipv4_prefix": "1.2.3.0/24", "effective": "2026-01-01T00:00:00+00:00",
                                   "expires": "2026-09-07T00:00:00+00:00"}]
        assert body["acknowledged_through"] == "2026-09-01"
        assert body["alert_integration"] == "MY_INT"
        assert body["alert_recipients"] == ["a@example.com"]
        assert isinstance(body["days_remaining"], int)

    def test_malformed_stored_json_degrades_to_empty(self, client, fake_sf, role_headers):
        # Defensive parse: a corrupted internal_config value (shouldn't happen
        # in practice, since only this codebase ever writes it) must not 500.
        fake_sf.config[egress_watch.CONFIG_RANGES] = "not-json"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = "not-json"
        resp = client.get("/system/egress-status", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ranges"] == []
        assert body["alert_recipients"] == []


class TestAcknowledgeEgress:
    def test_privileged_can_ack(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-ack", headers=role_headers("PRIV_ROLE"),
                           json={"through_date": "2026-09-10"})
        assert resp.status_code == 200
        assert resp.json() == {"acknowledged_through": "2026-09-10"}
        assert fake_sf.config[egress_watch.CONFIG_ACK_THROUGH] == "2026-09-10"

    def test_non_privileged_403(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-ack", headers=role_headers("OWNER_ROLE"),
                           json={"through_date": "2026-09-10"})
        assert resp.status_code == 403
        assert egress_watch.CONFIG_ACK_THROUGH not in fake_sf.config

    def test_invalid_date_422(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-ack", headers=role_headers("PRIV_ROLE"),
                           json={"through_date": "not-a-date"})
        assert resp.status_code == 422

    def test_round_trips_through_status_endpoint(self, client, fake_sf, role_headers):
        client.post("/system/egress-ack", headers=role_headers("PRIV_ROLE"),
                   json={"through_date": "2026-09-10"})
        resp = client.get("/system/egress-status", headers=role_headers("PRIV_ROLE"))
        assert resp.json()["acknowledged_through"] == "2026-09-10"


class TestSetEgressAlertConfig:
    def test_privileged_can_save(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-alert-config", headers=role_headers("PRIV_ROLE"),
                           json={"integration_name": "MY_INT", "recipients": ["a@example.com"]})
        assert resp.status_code == 200
        assert resp.json() == {"alert_integration": "MY_INT", "alert_recipients": ["a@example.com"]}
        assert fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] == "MY_INT"
        assert json.loads(fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS]) == ["a@example.com"]

    def test_non_privileged_403(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-alert-config", headers=role_headers("OWNER_ROLE"),
                           json={"integration_name": "MY_INT", "recipients": ["a@example.com"]})
        assert resp.status_code == 403

    def test_empty_clears_existing_config(self, client, fake_sf, role_headers):
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "OLD_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["old@example.com"])
        resp = client.post("/system/egress-alert-config", headers=role_headers("PRIV_ROLE"),
                           json={"integration_name": "", "recipients": []})
        assert resp.status_code == 200
        assert resp.json() == {"alert_integration": None, "alert_recipients": []}
        assert egress_watch.CONFIG_ALERT_INTEGRATION not in fake_sf.config
        assert egress_watch.CONFIG_ALERT_RECIPIENTS not in fake_sf.config

    def test_invalid_recipient_422(self, client, fake_sf, role_headers):
        resp = client.post("/system/egress-alert-config", headers=role_headers("PRIV_ROLE"),
                           json={"integration_name": "MY_INT", "recipients": ["not-an-email"]})
        assert resp.status_code == 422
        assert egress_watch.CONFIG_ALERT_INTEGRATION not in fake_sf.config


class TestGetEgressWarning:
    """Unprivileged: any resolvable caller identity, never gated to
    PRIVILEGED_ROLES (same posture as GET /system/external-access)."""

    def test_13_days_remaining_warns(self, client, fake_sf, role_headers, monkeypatch):
        fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] = "2026-09-07T00:00:00+00:00"
        monkeypatch.setattr(egress_watch, "_now", lambda: egress_watch._parse_ts("2026-08-25T00:00:00+00:00"))
        resp = client.get("/system/egress-warning", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"warn": True, "days_remaining": 13}

    def test_15_days_remaining_does_not_warn(self, client, fake_sf, role_headers, monkeypatch):
        fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] = "2026-09-09T00:00:00+00:00"
        monkeypatch.setattr(egress_watch, "_now", lambda: egress_watch._parse_ts("2026-08-25T00:00:00+00:00"))
        resp = client.get("/system/egress-warning", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"warn": False, "days_remaining": 15}

    def test_within_threshold_but_acked_does_not_warn(self, client, fake_sf, role_headers, monkeypatch):
        fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] = "2026-09-07T00:00:00+00:00"
        fake_sf.config[egress_watch.CONFIG_ACK_THROUGH] = "2026-09-07"
        monkeypatch.setattr(egress_watch, "_now", lambda: egress_watch._parse_ts("2026-08-25T00:00:00+00:00"))
        resp = client.get("/system/egress-warning", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"warn": False, "days_remaining": 13}

    def test_no_expiry_recorded_yet_does_not_warn(self, client, fake_sf, role_headers):
        resp = client.get("/system/egress-warning", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"warn": False, "days_remaining": None}

    def test_anonymous_still_gets_a_boolean_not_403(self, client, fake_sf):
        # Deliberately unprivileged: even a caller resolving to zero roles reads
        # this fine, so the Apps-page banner works for every operator.
        resp = client.get("/system/egress-warning")
        assert resp.status_code == 200
        assert "warn" in resp.json()
