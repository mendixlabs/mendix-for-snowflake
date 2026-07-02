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
        fake_httpx = types.SimpleNamespace(
            Client=lambda **kw: real_httpx.Client(transport=real_httpx.MockTransport(handler), **kw)
        )
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
        result = client.list_apps()
        assert result == [{"name": "myapp"}]
        assert handler.requests[0].method == "GET"
        assert handler.requests[0].url.path == "/apps"

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

    def test_update_compute_pool_omits_none_fields(self, mock_controller, recording_handler):
        handler = recording_handler(_ok({"name": "TEST_POOL"}))
        client = mock_controller(handler)
        client.update_compute_pool(min_nodes=2)
        import json
        req = handler.requests[0]
        assert req.method == "PATCH"
        assert json.loads(req.content) == {"min_nodes": 2}
