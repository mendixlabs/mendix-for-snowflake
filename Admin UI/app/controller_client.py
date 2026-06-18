"""Thin httpx wrapper for the Mendix deployment controller's REST API."""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 30.0


class ControllerError(Exception):
    """Raised when the controller returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"controller returned {status_code}: {message}")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text or ""
    if isinstance(body, dict):
        return str(body.get("detail", body))
    return str(body)


class ControllerClient:
    def __init__(self, base_url: str, operator: str):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_TIMEOUT,
            headers={"X-Operator": operator},
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = self._client.request(method, path, **kwargs)
        if r.status_code >= 400:
            raise ControllerError(r.status_code, _detail(r))
        return r

    def list_apps(self) -> list[dict]:
        return self._request("GET", "/apps").json()

    def get_app(self, name: str) -> dict:
        return self._request("GET", f"/apps/{name}").json()

    def create_app(self, payload: dict) -> dict:
        return self._request("POST", "/apps", json=payload).json()

    def get_logs(self, name: str, lines: int = 200) -> str:
        return self._request("GET", f"/apps/{name}/logs", params={"lines": lines}).json().get("logs", "")

    def trigger_deploy(self, name: str) -> dict:
        return self._request("POST", f"/apps/{name}/trigger-deploy").json()

    def deploy_pad(self, name: str, uploaded_file) -> dict:
        """Forward a Streamlit UploadedFile to the controller's deploy endpoint."""
        files = {
            "pad_file": (
                uploaded_file.name,
                uploaded_file.getvalue(),
                "application/zip",
            )
        }
        r = self._client.post(
            f"/apps/{name}/deploy",
            files=files,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
        )
        if r.status_code >= 400:
            raise ControllerError(r.status_code, _detail(r))
        return r.json()

    def update_constants(self, name: str, constants: dict) -> dict:
        return self._request("PUT", f"/apps/{name}/constants", json={"constants": constants}).json()

    def update_spec(self, name: str, payload: dict) -> dict:
        return self._request("PUT", f"/apps/{name}/spec", json=payload).json()

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
