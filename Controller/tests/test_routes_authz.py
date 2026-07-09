from __future__ import annotations


def _seed(fake_registry, make_record):
    record = make_record(name="myapp", owner_role="OWNER_ROLE")
    fake_registry.add(record)
    return record


class TestListApps:
    def test_privileged_sees_app(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.get("/apps", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_owner_sees_app(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.get("/apps", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_stranger_sees_empty(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.get("/apps", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_anonymous_sees_empty(self, client, fake_registry, make_record):
        _seed(fake_registry, make_record)
        resp = client.get("/apps")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_status_header_absent_when_statuses_fetch_succeeds(
        self, client, fake_registry, make_record, role_headers
    ):
        _seed(fake_registry, make_record)
        resp = client.get("/apps", headers=role_headers("OWNER_ROLE"))
        assert "x-service-status-unavailable" not in resp.headers

    def test_status_header_set_when_statuses_fetch_fails(
        self, client, fake_registry, make_record, role_headers, fake_sf
    ):
        _seed(fake_registry, make_record)
        fake_sf.raise_on["show_all_service_statuses"] = RuntimeError("boom")
        resp = client.get("/apps", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        assert resp.headers["x-service-status-unavailable"] == "true"
        # The apps themselves still come back - a status-query failure
        # degrades to unknown statuses, not a hard failure of the whole route.
        assert len(resp.json()) == 1
        assert resp.json()[0]["service_status"] is None


class TestGetAppAndLogs:
    def test_privileged_reads_ok(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        assert client.get("/apps/myapp", headers=role_headers("PRIV_ROLE")).status_code == 200
        assert client.get("/apps/myapp/logs", headers=role_headers("PRIV_ROLE")).status_code == 200

    def test_owner_reads_ok(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        assert client.get("/apps/myapp", headers=role_headers("OWNER_ROLE")).status_code == 200
        assert client.get("/apps/myapp/logs", headers=role_headers("OWNER_ROLE")).status_code == 200

    def test_stranger_gets_404_not_403(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        assert client.get("/apps/myapp", headers=role_headers("OTHER_ROLE")).status_code == 404
        assert client.get("/apps/myapp/logs", headers=role_headers("OTHER_ROLE")).status_code == 404

    def test_nonexistent_app_404_for_everyone(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        assert client.get("/apps/ghost", headers=role_headers("PRIV_ROLE")).status_code == 404
        assert client.get("/apps/ghost", headers=role_headers("OWNER_ROLE")).status_code == 404

    def test_get_service_logs_failure_502(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Mirrors TestSystemLogs.test_get_service_logs_failure_502 in
        # test_routes_system.py for the per-app sibling endpoint.
        _seed(fake_registry, make_record)
        fake_sf.raise_on["get_service_logs"] = RuntimeError("access denied")
        resp = client.get("/apps/myapp/logs", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 502
        assert "access denied" in resp.json()["detail"]


class TestMutationsAuthz:
    def test_delete_stranger_403(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.delete("/apps/myapp", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_suspend_stranger_403(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 403

    def test_delete_nonexistent_404(self, client, fake_registry, make_record, role_headers):
        _seed(fake_registry, make_record)
        resp = client.delete("/apps/ghost", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 404

    def test_suspend_owner_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = _seed(fake_registry, make_record)
        # _run_suspend polls for "SUSPENDED"; without this the fake's default
        # "RUNNING" status never matches and the background task burns a real
        # 120s timeout sleeping (see _poll_status).
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202

    def test_suspend_privileged_succeeds(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = _seed(fake_registry, make_record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 202

    def test_create_app_owner_role_not_in_callers_roles_403(self, client, role_headers):
        resp = client.post(
            "/apps",
            headers=role_headers("SOME_ROLE"),
            json={"name": "newapp", "pg_database": "newapp_db", "admin_password": "pw",
                  "owner_role": "OTHER_ROLE"},
        )
        assert resp.status_code == 403

    def test_create_app_privileged_may_assign_any_owner_role(self, client, fake_sf, fake_registry, role_headers):
        resp = client.post(
            "/apps",
            headers=role_headers("PRIV_ROLE"),
            json={"name": "newapp", "pg_database": "newapp_db", "admin_password": "pw",
                  "owner_role": "ANY_ROLE"},
        )
        assert resp.status_code == 201


class TestSystemAuthz:
    def test_system_logs_non_privileged_403(self, client, role_headers):
        resp = client.get("/system/logs/controller", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 403

    def test_system_logs_privileged_200(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/controller", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200

    def test_compute_pool_get_non_privileged_403(self, client, role_headers):
        resp = client.get("/system/compute-pool", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 403

    def test_compute_pool_get_privileged_200(self, client, fake_sf, role_headers):
        resp = client.get("/system/compute-pool", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200

    def test_pg_info_get_non_privileged_403(self, client, role_headers):
        resp = client.get("/system/pg-info", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 403

    def test_pg_info_get_privileged_200(self, client, fake_sf, role_headers):
        resp = client.get("/system/pg-info", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200


class TestActivityAuthz:
    def test_privileged_sees_all_rows(self, client, fake_activity, role_headers):
        fake_activity.preset = [
            {"app_name": "myapp", "operator": "bob"},
            {"app_name": None, "operator": "bob"},
        ]
        resp = client.get("/activity", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_non_privileged_sees_only_owned_apps(self, client, fake_registry, make_record, fake_activity, role_headers):
        _seed(fake_registry, make_record)
        fake_activity.preset = [
            {"app_name": "myapp", "operator": "bob"},
            {"app_name": "otherapp", "operator": "bob"},
        ]
        resp = client.get("/activity", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["app_name"] == "myapp"

    def test_rows_with_no_app_hidden_from_non_privileged(self, client, fake_registry, make_record, fake_activity, role_headers):
        _seed(fake_registry, make_record)
        fake_activity.preset = [{"app_name": None, "operator": "bob"}]
        resp = client.get("/activity", headers=role_headers("OWNER_ROLE"))
        assert resp.json() == []
