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


class FakeCursor:
    """Scripted cursor: an ordered list of (sql_substring_matcher, description_cols, rows).

    execute(sql) picks the first script whose matcher is found in sql (a plain
    substring, or a callable(sql) -> bool). description/fetchall then reflect
    that script until the next execute() call.
    """

    def __init__(self, scripts):
        self._scripts = scripts
        self._current = None
        self.executed: list[str] = []

    def execute(self, sql, *params):
        self.executed.append(sql)
        for matcher, cols, rows in self._scripts:
            matches = matcher(sql) if callable(matcher) else matcher in sql
            if matches:
                self._current = (cols, rows)
                return
        self._current = ([], [])

    @property
    def description(self):
        cols = self._current[0] if self._current else []
        return [(c,) + (None,) * 6 for c in cols]

    def fetchall(self):
        return self._current[1] if self._current else []

    def fetchone(self):
        rows = self._current[1] if self._current else []
        return rows[0] if rows else None


@pytest.fixture
def fake_cursor():
    return FakeCursor


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self, *_a, **_kw):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn():
    return FakeConn
