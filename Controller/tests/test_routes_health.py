from __future__ import annotations


class TestGetAppHealth:
    def test_all_containers_ready(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "READY", "message": None},
        ]
        resp = client.get("/apps/myapp/health", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["service_status"] == "RUNNING"
        assert body["ready"] is True
        assert body["containers"] == [{"container_name": "mendix-app", "status": "READY", "message": None}]

    def test_running_but_not_ready(self, client, fake_sf, fake_registry, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "RUNNING"
        fake_sf.containers[record.service_name] = [
            {"container_name": "mendix-app", "status": "RUNNING", "message": "starting up"},
        ]
        resp = client.get("/apps/myapp/health", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        assert body["service_status"] == "RUNNING"

    def test_no_containers_not_ready(self, client, fake_sf, fake_registry, make_record, role_headers):
        # No rows at all (e.g. a suspended service) must never read as "ready".
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        fake_sf.containers[record.service_name] = []
        resp = client.get("/apps/myapp/health", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        assert body["containers"] == []
        assert body["service_status"] == "SUSPENDED"

    def test_missing_service_degrades_gracefully(self, client, fake_sf, fake_registry, make_record, role_headers):
        # Both show_service_status and show_service_containers swallow their own
        # Snowflake errors and return None/[] respectively (a deleted/never-
        # created service looks like "the query failed" to the underlying
        # DESCRIBE/SHOW) - simulated here the same way, by leaving the service
        # out of fake_sf's tables entirely except an explicit None status. The
        # health endpoint must not 500; it degrades to "nothing to show".
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = None
        fake_sf.containers[record.service_name] = []
        resp = client.get("/apps/myapp/health", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"service_status": None, "containers": [], "ready": False}

    def test_privileged_reads_ok(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/health", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200

    def test_stranger_gets_404_not_403(self, client, fake_registry, make_record, role_headers):
        fake_registry.add(make_record(name="myapp", owner_role="OWNER_ROLE"))
        resp = client.get("/apps/myapp/health", headers=role_headers("OTHER_ROLE"))
        assert resp.status_code == 404

    def test_nonexistent_app_404(self, client, fake_registry, role_headers):
        resp = client.get("/apps/ghost/health", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404
