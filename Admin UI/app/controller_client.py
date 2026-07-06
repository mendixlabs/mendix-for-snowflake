"""Thin httpx wrapper for the Mendix deployment controller's REST API."""
from __future__ import annotations

import os

import httpx

DEFAULT_TIMEOUT = 30.0


class ControllerError(Exception):
    """Raised when the controller returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        self.status_code = status_code
        self.message = message
        self.body = body or {}
        super().__init__(f"controller returned {status_code}: {message}")

    def missing_constants(self) -> list[str]:
        """Constant names the controller reported as required-but-unset (422 deploy).

        The controller raises HTTPException(detail={"detail": ..., "missing": [...]}),
        which FastAPI wraps as {"detail": {"detail": ..., "missing": [...]}}.
        """
        detail = self.body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("missing"), list):
            return [str(m) for m in detail["missing"]]
        return []


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text or ""
    if isinstance(body, dict):
        return str(body.get("detail", body))
    return str(body)


class ControllerClient:
    def __init__(self, base_url: str, operator: str, roles: tuple[str, ...] = ()):
        headers = {"X-Operator": operator}
        if roles:
            headers["X-Operator-Roles"] = ",".join(roles)
        # Shared internal-hop token so the controller trusts the operator-role headers
        # above (the controller endpoint is public; see Controller/app/auth.py). Set on
        # both services by setup_script.sql; absent only on installs predating it.
        internal_token = os.environ.get("INTERNAL_AUTH_TOKEN")
        if internal_token:
            headers["X-Internal-Auth"] = internal_token
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_TIMEOUT,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.RequestError as e:
            raise ControllerError(503, f"Controller unreachable: {e}") from e
        if r.status_code >= 400:
            body = None
            try:
                body = r.json()
            except Exception:
                body = None
            raise ControllerError(r.status_code, _detail(r), body if isinstance(body, dict) else None)
        return r

    def list_apps(self) -> list[dict]:
        return self._request("GET", "/apps").json()

    def get_app(self, name: str) -> dict:
        return self._request("GET", f"/apps/{name}").json()

    def create_app(self, payload: dict) -> dict:
        return self._request("POST", "/apps", json=payload).json()

    def get_logs(self, name: str, lines: int = 200) -> str:
        return self._request("GET", f"/apps/{name}/logs", params={"lines": lines}).json().get("logs", "")

    def get_system_logs(self, target: str, lines: int = 200) -> str:
        """Logs for an infrastructure service ('controller' or 'admin-ui'). Privileged only."""
        return self._request("GET", f"/system/logs/{target}", params={"lines": lines}).json().get("logs", "")

    def trigger_deploy(self, name: str) -> dict:
        return self._request("POST", f"/apps/{name}/trigger-deploy").json()

    def update_constants(self, name: str, constants: dict) -> dict:
        return self._request("PUT", f"/apps/{name}/constants", json={"constants": constants}).json()

    def update_spec(self, name: str, payload: dict) -> dict:
        return self._request("PUT", f"/apps/{name}/spec", json=payload).json()

    def update_license(self, name: str, license_id: str, license_key: str) -> dict:
        return self._request(
            "PUT", f"/apps/{name}/license",
            json={"license_id": license_id, "license_key": license_key},
        ).json()

    def delete_license(self, name: str) -> dict:
        return self._request("DELETE", f"/apps/{name}/license").json()

    def update_role_mapping(self, name: str, role_mapping: dict) -> dict:
        return self._request(
            "PUT", f"/apps/{name}/role-mapping",
            json={"role_mapping": role_mapping},
        ).json()

    def delete_role_mapping(self, name: str) -> dict:
        return self._request("DELETE", f"/apps/{name}/role-mapping").json()

    def list_activity(self, app: str | None = None, operator: str | None = None,
                      limit: int = 100) -> list[dict]:
        params: dict = {"limit": limit}
        if app:
            params["app"] = app
        if operator:
            params["operator"] = operator
        return self._request("GET", "/activity", params=params).json()

    def suspend(self, name: str) -> dict:
        return self._request("POST", f"/apps/{name}/suspend").json()

    def resume(self, name: str) -> dict:
        return self._request("POST", f"/apps/{name}/resume").json()

    def delete_app(self, name: str) -> None:
        self._request("DELETE", f"/apps/{name}")

    def get_compute_pool(self) -> dict:
        return self._request("GET", "/system/compute-pool").json()

    def update_compute_pool(
        self,
        *,
        min_nodes: int | None = None,
        max_nodes: int | None = None,
        auto_suspend_secs: int | None = None,
    ) -> dict:
        body: dict = {}
        if min_nodes is not None:
            body["min_nodes"] = min_nodes
        if max_nodes is not None:
            body["max_nodes"] = max_nodes
        if auto_suspend_secs is not None:
            body["auto_suspend_secs"] = auto_suspend_secs
        return self._request("PATCH", "/system/compute-pool", json=body).json()
