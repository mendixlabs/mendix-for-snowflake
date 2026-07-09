from __future__ import annotations

import pytest
from starlette.requests import Request

from app import auth


def _make_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Pure half
# ---------------------------------------------------------------------------

class TestAuthorize:
    def test_privileged_role_short_circuits(self):
        assert auth.authorize("SOME_OTHER_ROLE", {"PRIV_ROLE"}) is True

    def test_exact_owner_match(self):
        assert auth.authorize("owner_role", {"OWNER_ROLE"}) is True

    def test_stranger_denied(self):
        assert auth.authorize("OWNER_ROLE", {"OTHER_ROLE"}) is False

    def test_owner_role_none_denied(self):
        assert auth.authorize(None, {"OTHER_ROLE"}) is False

    def test_owner_role_empty_denied(self):
        assert auth.authorize("", {"OTHER_ROLE"}) is False


class TestPrivilegedRolesFn:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("PRIVILEGED_ROLES", raising=False)
        assert auth._privileged_roles() == frozenset({"MENDIX_DEPLOY_CONTROLLER_ROLE"})

    def test_parses_comma_list_with_whitespace_and_case_folds(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", " role_a ,Role_B ")
        assert auth._privileged_roles() == frozenset({"ROLE_A", "ROLE_B"})

    def test_empty_entries_dropped(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", "ROLE_A,,ROLE_B,")
        assert auth._privileged_roles() == frozenset({"ROLE_A", "ROLE_B"})


# ---------------------------------------------------------------------------
# Request-path half
# ---------------------------------------------------------------------------

class TestResolveCallerInternalPath:
    def test_valid_internal_auth_parses_roles_and_user(self):
        req = _make_request({
            "X-Internal-Auth": "test-internal-token",
            "X-Operator": "bob",
            "X-Operator-Roles": " role_a , role_b ",
        })
        identity = auth.resolve_caller(req)
        assert identity.user == "bob"
        assert identity.roles == {"ROLE_A", "ROLE_B"}

    def test_wrong_token_denies_roles(self):
        req = _make_request({
            "X-Internal-Auth": "wrong-token",
            "X-Operator": "bob",
            "X-Operator-Roles": "role_a",
        })
        identity = auth.resolve_caller(req)
        assert identity.roles == set()
        assert identity.user is None

    def test_missing_token_header_denies_roles(self):
        req = _make_request({"X-Operator": "bob", "X-Operator-Roles": "role_a"})
        identity = auth.resolve_caller(req)
        assert identity.roles == set()

    def test_internal_auth_token_unset_denies_roles(self, monkeypatch):
        monkeypatch.setattr(auth, "_INTERNAL_AUTH_TOKEN", None)
        req = _make_request({"X-Operator": "bob", "X-Operator-Roles": "ACCOUNTADMIN"})
        identity = auth.resolve_caller(req)
        assert identity.roles == set()
        assert identity.user is None

    def test_internal_auth_token_unset_denies_even_with_internal_header(self, monkeypatch):
        monkeypatch.setattr(auth, "_INTERNAL_AUTH_TOKEN", None)
        req = _make_request({
            "X-Internal-Auth": "anything",
            "X-Operator": "bob",
            "X-Operator-Roles": "ACCOUNTADMIN",
        })
        identity = auth.resolve_caller(req)
        assert identity.roles == set()
        assert identity.user is None


class TestResolveCallerTokenPath:
    def test_caller_token_resolves_identity(self, monkeypatch):
        calls = []

        def fake_identity(token):
            calls.append(token)
            return auth.CallerIdentity(user="ALICE", roles={"ROLE_X"})

        monkeypatch.setattr(auth, "_identity_from_snowflake", fake_identity)
        req = _make_request({
            "Sf-Context-Current-User-Token": "caller-tok",
            "X-Operator-Roles": "should-be-ignored",
        })
        identity = auth.resolve_caller(req)
        assert identity.user == "ALICE"
        assert identity.roles == {"ROLE_X"}
        assert calls == ["caller-tok"]

    def test_caller_token_resolution_failure_denies_roles(self, monkeypatch):
        def raiser(token):
            raise RuntimeError("boom")

        monkeypatch.setattr(auth, "_identity_from_snowflake", raiser)
        req = _make_request({"Sf-Context-Current-User-Token": "caller-tok"})
        identity = auth.resolve_caller(req)
        assert identity.roles == set()
        assert identity.user is None


class TestIdentityCache:
    def test_cache_hit_within_ttl(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
        calls = []

        def fake_identity(token):
            calls.append(token)
            return auth.CallerIdentity(user="U", roles={"R"})

        monkeypatch.setattr(auth, "_identity_from_snowflake", fake_identity)
        auth._cached_identity("tok1")
        auth._cached_identity("tok1")
        assert len(calls) == 1

    def test_cache_expires_after_ttl(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
        calls = []

        def fake_identity(token):
            calls.append(token)
            return auth.CallerIdentity(user="U", roles={"R"})

        monkeypatch.setattr(auth, "_identity_from_snowflake", fake_identity)
        auth._cached_identity("tok1")
        clock["t"] += 61
        auth._cached_identity("tok1")
        assert len(calls) == 2

    def test_different_tokens_separate_entries(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
        calls = []

        def fake_identity(token):
            calls.append(token)
            return auth.CallerIdentity(user="U", roles={"R"})

        monkeypatch.setattr(auth, "_identity_from_snowflake", fake_identity)
        auth._cached_identity("tok1")
        auth._cached_identity("tok2")
        assert len(calls) == 2

    def test_expired_entries_pruned_on_insert(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(auth, "_identity_from_snowflake",
                             lambda token: auth.CallerIdentity(user="U", roles={"R"}))
        auth._cached_identity("tok1")
        clock["t"] += 61
        auth._cached_identity("tok2")
        assert "tok1" not in {k for k in auth._identity_cache}
        import hashlib
        old_key = hashlib.sha256(b"tok1").hexdigest()
        assert old_key not in auth._identity_cache


class TestIdentityFromSnowflake:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_service_token", lambda: "svc-tok")
        captured = {}

        class FakeCursor:
            def execute(self, sql):
                pass

            def fetchone(self):
                return ("ALICE", '["r1","R2"]')

        class FakeConn:
            def __init__(self):
                self.closed = False

            def cursor(self):
                return FakeCursor()

            def close(self):
                self.closed = True

        fake_conn = FakeConn()

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return fake_conn

        monkeypatch.setattr(auth.snowflake.connector, "connect", fake_connect)
        identity = auth._identity_from_snowflake("caller-tok")
        assert captured["token"] == "svc-tok.caller-tok"
        assert identity.user == "ALICE"
        assert identity.roles == {"R1", "R2"}
        assert fake_conn.closed is True

    def test_closes_connection_even_on_cursor_error(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_service_token", lambda: "svc-tok")

        class FakeCursor:
            def execute(self, sql):
                raise RuntimeError("boom")

        class FakeConn:
            def __init__(self):
                self.closed = False

            def cursor(self):
                return FakeCursor()

            def close(self):
                self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr(auth.snowflake.connector, "connect", lambda **kw: fake_conn)
        with pytest.raises(RuntimeError):
            auth._identity_from_snowflake("caller-tok")
        assert fake_conn.closed is True
