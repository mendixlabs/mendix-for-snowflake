"""Shared fixtures for the Admin UI test suite (non-Streamlit modules only)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx as real_httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))  # ".../Admin UI/app"

from controller_client import ControllerClient  # noqa: E402


@pytest.fixture
def mock_controller(monkeypatch):
    def _make(handler, *, operator: str = "TEST_USER", roles: tuple[str, ...] = (),
             internal_token: str | None = None) -> ControllerClient:
        if internal_token is None:
            monkeypatch.delenv("INTERNAL_AUTH_TOKEN", raising=False)
        else:
            monkeypatch.setenv("INTERNAL_AUTH_TOKEN", internal_token)

        import controller_client as cc_module

        # Preserve every real httpx attribute (RequestError, ConnectError, etc. -
        # controller_client references these for exception handling) and override
        # only Client, so requests are served by the in-memory MockTransport.
        fake_httpx = types.SimpleNamespace(**vars(real_httpx))
        fake_httpx.Client = lambda **kw: real_httpx.Client(transport=real_httpx.MockTransport(handler), **kw)
        monkeypatch.setattr(cc_module, "httpx", fake_httpx)
        return ControllerClient("http://controller.test", operator=operator, roles=roles)

    return _make


@pytest.fixture
def recording_handler():
    def _make(response_map):
        """response_map: callable(request) -> httpx.Response, or a single
        httpx.Response reused for every request. Records every request seen."""
        requests: list[real_httpx.Request] = []

        def handler(request: real_httpx.Request) -> real_httpx.Response:
            requests.append(request)
            if callable(response_map):
                return response_map(request)
            return response_map

        handler.requests = requests
        return handler

    return _make


