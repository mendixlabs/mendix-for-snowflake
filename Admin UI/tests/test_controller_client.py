from __future__ import annotations

import httpx
import pytest

from controller_client import ControllerError


def _ok(json_body=None, status=200):
    return lambda request: httpx.Response(status, json=json_body if json_body is not None else {})


class TestHeaderConstruction:
    def test_x_operator_always_present(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler, operator="bob")
        client.list_apps()
        assert handler.requests[0].headers["x-operator"] == "bob"

    def test_roles_joined_with_commas_when_present(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler, roles=("ROLE_A", "ROLE_B"))
        client.list_apps()
        assert handler.requests[0].headers["x-operator-roles"] == "ROLE_A,ROLE_B"

    def test_roles_header_absent_when_empty(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler, roles=())
        client.list_apps()
        assert "x-operator-roles" not in handler.requests[0].headers

    def test_internal_auth_present_iff_env_set(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler, internal_token="secret-token")
        client.list_apps()
        assert handler.requests[0].headers["x-internal-auth"] == "secret-token"

    def test_internal_auth_absent_when_unset(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler, internal_token=None)
        client.list_apps()
        assert "x-internal-auth" not in handler.requests[0].headers

    def test_base_url_trailing_slash_stripped(self, monkeypatch, recording_handler):
        import types
        import httpx as real_httpx
        import controller_client as cc_module
        from controller_client import ControllerClient

        handler = recording_handler(_ok([]))
        monkeypatch.delenv("INTERNAL_AUTH_TOKEN", raising=False)
        fake_httpx = types.SimpleNamespace(**vars(real_httpx))
        fake_httpx.Client = lambda **kw: real_httpx.Client(transport=real_httpx.MockTransport(handler), **kw)
        monkeypatch.setattr(cc_module, "httpx", fake_httpx)
        client = ControllerClient("http://controller.test/", operator="bob")
        client.list_apps()
        assert str(handler.requests[0].url) == "http://controller.test/apps"


class TestRequestErrorHandling:
    def test_raises_controller_error_on_4xx(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(404, json={"detail": "not found"}))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("ghost")
        err = exc_info.value
        assert err.status_code == 404
        assert err.message == "not found"
        assert err.body == {"detail": "not found"}

    def test_raises_controller_error_on_5xx(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(500, json={"detail": "boom"}))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert exc_info.value.status_code == 500

    def test_non_json_error_body_falls_back_to_text(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(502, text="upstream error"))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        err = exc_info.value
        assert err.message == "upstream error"
        assert err.body == {}

    def test_connect_error_becomes_503_controller_error(self, mock_controller, recording_handler):
        def _raise_connect_error(request):
            raise httpx.ConnectError("connection refused", request=request)

        handler = recording_handler(_raise_connect_error)
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.list_apps()
        err = exc_info.value
        assert err.status_code == 503
        assert "Controller unreachable" in err.message

    def test_read_timeout_becomes_503_controller_error(self, mock_controller, recording_handler):
        def _raise_read_timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        handler = recording_handler(_raise_read_timeout)
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert exc_info.value.status_code == 503


class TestMissingConstants:
    def test_nested_detail_extracts_missing_list(self):
        err = ControllerError(422, "x", {"detail": {"detail": "New constants", "missing": ["A", "B"]}})
        assert err.missing_constants() == ["A", "B"]

    def test_string_detail_returns_empty(self):
        err = ControllerError(422, "x", {"detail": "plain string"})
        assert err.missing_constants() == []

    def test_missing_key_returns_empty(self):
        err = ControllerError(422, "x", {"detail": {"detail": "New constants"}})
        assert err.missing_constants() == []

    def test_non_list_missing_returns_empty(self):
        err = ControllerError(422, "x", {"detail": {"missing": "not-a-list"}})
        assert err.missing_constants() == []


class TestUnknownUserroles:
    def test_nested_detail_extracts_both_lists(self):
        err = ControllerError(422, "x", {"detail": {
            "detail": "Mapping targets userroles not present in the deployed PAD",
            "unknown_userroles": ["Ghost"],
            "detected_userroles": ["User", "Administrator"],
        }})
        assert err.unknown_userroles() == (["Ghost"], ["User", "Administrator"])

    def test_missing_detected_key_returns_empty_list_not_error(self):
        err = ControllerError(422, "x", {"detail": {"unknown_userroles": ["Ghost"]}})
        assert err.unknown_userroles() == (["Ghost"], [])

    def test_string_detail_returns_empty(self):
        err = ControllerError(422, "x", {"detail": "plain string"})
        assert err.unknown_userroles() == ([], [])

    def test_non_list_unknown_returns_empty(self):
        err = ControllerError(422, "x", {"detail": {"unknown_userroles": "not-a-list"}})
        assert err.unknown_userroles() == ([], [])


class TestDetailHelper:
    def test_dict_body_with_detail(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(400, json={"detail": "bad request"}))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert exc_info.value.message == "bad request"

    def test_dict_body_without_detail_stringified(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(400, json={"error": "oops"}))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert "oops" in exc_info.value.message

    def test_non_dict_json_stringified(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(400, json=["a", "b"]))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert exc_info.value.message == "['a', 'b']"

    def test_unparseable_body_falls_back_to_text(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(400, text="not json at all {"))
        client = mock_controller(handler)
        with pytest.raises(ControllerError) as exc_info:
            client.get_app("myapp")
        assert exc_info.value.message == "not json at all {"


class TestEndpointSmokeTests:
    def test_list_apps(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([{"name": "myapp"}]))
        client = mock_controller(handler)
        apps, status_unavailable = client.list_apps()
        assert apps == [{"name": "myapp"}]
        assert status_unavailable is False
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/apps"

    def test_list_apps_status_unavailable_header(self, mock_controller, recording_handler):
        handler = recording_handler(
            lambda req: httpx.Response(200, json=[{"name": "myapp"}],
                                       headers={"X-Service-Status-Unavailable": "true"})
        )
        client = mock_controller(handler)
        apps, status_unavailable = client.list_apps()
        assert apps == [{"name": "myapp"}]
        assert status_unavailable is True

    def test_get_app(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"name": "myapp"}))
        client = mock_controller(handler)
        client.get_app("myapp")
        assert handler.requests[0].url.path == "/apps/myapp"

    def test_create_app(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "NOT_DEPLOYED"}, status=201))
        client = mock_controller(handler)
        payload = {"name": "myapp", "pg_database": "db", "admin_password": "pw"}
        client.create_app(payload)
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps"
        import json
        assert json.loads(req.content) == payload

    def test_get_logs_returns_logs_field(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"logs": "log text"}))
        client = mock_controller(handler)
        assert client.get_logs("myapp") == "log text"
        assert handler.requests[0].url.path == "/apps/myapp/logs"

    def test_get_logs_missing_field_defaults_empty(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({}))
        client = mock_controller(handler)
        assert client.get_logs("myapp") == ""

    def test_get_system_logs(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"logs": "sys log"}))
        client = mock_controller(handler)
        assert client.get_system_logs("controller") == "sys log"
        assert handler.requests[0].url.path == "/system/logs/controller"

    def test_start_log_download(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"job_id": "abc123", "status": "PENDING"}, status=202))
        client = mock_controller(handler)
        result = client.start_log_download("myapp")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps/myapp/logs/download"
        assert result == {"job_id": "abc123", "status": "PENDING"}

    def test_get_log_download(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "READY", "logs": "log text", "error": None}))
        client = mock_controller(handler)
        result = client.get_log_download("myapp", "abc123")
        assert handler.requests[0].url.path == "/apps/myapp/logs/download/abc123"
        assert result == {"status": "READY", "logs": "log text", "error": None}

    def test_start_system_log_download(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"job_id": "sys1", "status": "PENDING"}, status=202))
        client = mock_controller(handler)
        client.start_system_log_download("controller")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/system/logs/controller/download"

    def test_get_system_log_download(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "FAILED", "logs": None, "error": "boom"}))
        client = mock_controller(handler)
        result = client.get_system_log_download("controller", "sys1")
        assert handler.requests[0].url.path == "/system/logs/controller/download/sys1"
        assert result == {"status": "FAILED", "logs": None, "error": "boom"}

    def test_close_closes_underlying_client(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler)
        client.close()
        assert client._client.is_closed

    def test_trigger_deploy(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.trigger_deploy("myapp")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps/myapp/trigger-deploy"

    def test_list_history(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"history": [{"operation": "deploy"}]}))
        client = mock_controller(handler)
        result = client.list_history("myapp")
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/apps/myapp/history"
        assert result == [{"operation": "deploy"}]

    def test_list_history_missing_field_defaults_empty(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({}))
        client = mock_controller(handler)
        assert client.list_history("myapp") == []

    def test_rollback(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        result = client.rollback("myapp")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps/myapp/rollback"
        assert req.content == b""
        assert result == {"status": "DEPLOYING"}

    def test_rollback_with_entry_id(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.rollback("myapp", entry_id=7)
        req = handler.requests[0]
        import json
        assert req.url.path == "/apps/myapp/rollback"
        assert json.loads(req.content) == {"entry_id": 7}

    def test_get_health(self, mock_controller, recording_handler):
        body = {"service_status": "RUNNING", "containers": [{"container_name": "mendix-app", "status": "READY",
                                                              "message": None}], "ready": True}
        handler = recording_handler(_ok(body))
        client = mock_controller(handler)
        result = client.get_health("myapp")
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/apps/myapp/health"
        assert result == body

    def test_update_constants_body_shape(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.update_constants("myapp", {"A.B": "v"})
        import json
        req = handler.requests[0]
        assert req.method == "PUT"
        assert json.loads(req.content) == {"constants": {"A.B": "v"}}

    def test_update_spec(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.update_spec("myapp", {"resource_tier": "large"})
        req = handler.requests[0]
        assert req.method == "PUT"
        assert req.url.path == "/apps/myapp/spec"

    def test_apply_platform_update(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.apply_platform_update("myapp")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps/myapp/platform-update"

    def test_update_license(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.update_license("myapp", "LIC-1", "secret-key")
        import json
        req = handler.requests[0]
        assert req.method == "PUT"
        assert req.url.path == "/apps/myapp/license"
        assert json.loads(req.content) == {"license_id": "LIC-1", "license_key": "secret-key"}

    def test_delete_license(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.delete_license("myapp")
        req = handler.requests[0]
        assert req.method == "DELETE"
        assert req.url.path == "/apps/myapp/license"

    def test_update_role_mapping(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING", "warnings": []}, status=202))
        client = mock_controller(handler)
        client.update_role_mapping("myapp", {"ROLE_A": "Administrator"})
        import json
        req = handler.requests[0]
        assert req.method == "PUT"
        assert req.url.path == "/apps/myapp/role-mapping"
        assert json.loads(req.content) == {"role_mapping": {"ROLE_A": "Administrator"}}

    def test_delete_role_mapping(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.delete_role_mapping("myapp")
        req = handler.requests[0]
        assert req.method == "DELETE"
        assert req.url.path == "/apps/myapp/role-mapping"

    def test_list_activity_params_only_set_filters(self, mock_controller, recording_handler):
        handler = recording_handler(_ok([]))
        client = mock_controller(handler)
        client.list_activity(app="myapp")
        req = handler.requests[0]
        assert dict(req.url.params) == {"limit": "100", "app": "myapp"}
        assert "operator" not in req.url.params

    def test_suspend(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "SUSPENDING"}, status=202))
        client = mock_controller(handler)
        client.suspend("myapp")
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/apps/myapp/suspend"

    def test_resume(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "RESUMING"}, status=202))
        client = mock_controller(handler)
        client.resume("myapp")
        assert handler.requests[0].url.path == "/apps/myapp/resume"

    def test_delete_app_returns_none(self, mock_controller, recording_handler):
        handler = recording_handler(lambda req: httpx.Response(204))
        client = mock_controller(handler)
        result = client.delete_app("myapp")
        assert result is None
        req = handler.requests[0]
        assert req.method == "DELETE"
        assert req.url.path == "/apps/myapp"

    def test_get_compute_pool(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"name": "TEST_POOL"}))
        client = mock_controller(handler)
        client.get_compute_pool()
        assert handler.requests[0].url.path == "/system/compute-pool"

    def test_get_pg_info(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"host": "pg.internal", "port": "5432"}))
        client = mock_controller(handler)
        result = client.get_pg_info()
        assert handler.requests[0].url.path == "/system/pg-info"
        assert result == {"host": "pg.internal", "port": "5432"}

    def test_get_external_access_slots(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"slots": [{"key": "app_eai_1", "bound": True}]}))
        client = mock_controller(handler)
        result = client.get_external_access_slots()
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/system/external-access"
        assert result == [{"key": "app_eai_1", "bound": True}]

    def test_get_external_access_slots_missing_field_defaults_empty(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({}))
        client = mock_controller(handler)
        assert client.get_external_access_slots() == []

    def test_update_external_access(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"status": "DEPLOYING"}, status=202))
        client = mock_controller(handler)
        client.update_external_access("myapp", ["app_eai_1"])
        import json
        req = handler.requests[0]
        assert req.method == "PUT"
        assert req.url.path == "/apps/myapp/external-access"
        assert json.loads(req.content) == {"slots": ["app_eai_1"]}

    def test_update_compute_pool_omits_none_fields(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"name": "TEST_POOL"}))
        client = mock_controller(handler)
        client.update_compute_pool(min_nodes=2)
        import json
        req = handler.requests[0]
        assert req.method == "PATCH"
        assert json.loads(req.content) == {"min_nodes": 2}

    def test_get_egress_status(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({
            "min_expiry": "2026-09-07T00:00:00+00:00", "days_remaining": 13, "ranges": [],
            "acknowledged_through": None, "alert_integration": None, "alert_recipients": [],
        }))
        client = mock_controller(handler)
        result = client.get_egress_status()
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/system/egress-status"
        assert result["days_remaining"] == 13

    def test_ack_egress(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"acknowledged_through": "2026-09-10"}))
        client = mock_controller(handler)
        result = client.ack_egress("2026-09-10")
        import json
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/system/egress-ack"
        assert json.loads(req.content) == {"through_date": "2026-09-10"}
        assert result == {"acknowledged_through": "2026-09-10"}

    def test_set_egress_alert_config(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"alert_integration": "MY_INT", "alert_recipients": ["a@example.com"]}))
        client = mock_controller(handler)
        client.set_egress_alert_config("MY_INT", ["a@example.com"])
        import json
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/system/egress-alert-config"
        assert json.loads(req.content) == {"integration_name": "MY_INT", "recipients": ["a@example.com"]}

    def test_get_egress_warning(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"warn": True, "days_remaining": 13}))
        client = mock_controller(handler)
        result = client.get_egress_warning()
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/system/egress-warning"
        assert result == {"warn": True, "days_remaining": 13}
