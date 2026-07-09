from __future__ import annotations

import streamlit as st

import auth as ui_auth


class TestOperatorRoles:
    def test_success_clears_stashed_error(self, monkeypatch):
        st.session_state.clear()
        monkeypatch.setattr(ui_auth, "list_operator_roles", lambda: ("ROLE_A",))
        assert ui_auth.operator_roles() == ("ROLE_A",)
        assert ui_auth.operator_roles_error() is None

    def test_exception_is_stashed_not_swallowed_silently(self, monkeypatch):
        st.session_state.clear()

        def _raise():
            raise RuntimeError("owning application must have at least one CALLER privilege")

        monkeypatch.setattr(ui_auth, "list_operator_roles", _raise)
        assert ui_auth.operator_roles() == ()
        assert "CALLER privilege" in ui_auth.operator_roles_error()

    def test_cached_after_first_resolution(self, monkeypatch):
        st.session_state.clear()
        calls = []
        monkeypatch.setattr(ui_auth, "list_operator_roles", lambda: calls.append(1) or ("ROLE_A",))
        ui_auth.operator_roles()
        ui_auth.operator_roles()
        assert len(calls) == 1


class TestPrivilegedRolesFn:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("PRIVILEGED_ROLES", raising=False)
        assert ui_auth._privileged_roles() == frozenset({"MENDIX_DEPLOY_CONTROLLER_ROLE"})

    def test_comma_parsing_and_case_folding(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", " role_a ,Role_B ")
        assert ui_auth._privileged_roles() == frozenset({"ROLE_A", "ROLE_B"})


class TestIsPrivilegedOperator:
    def test_true_when_roles_intersect(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", "PRIV")
        monkeypatch.setattr(ui_auth, "operator_roles", lambda: ("PRIV",))
        assert ui_auth.is_privileged_operator() is True

    def test_false_when_disjoint(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", "PRIV")
        monkeypatch.setattr(ui_auth, "operator_roles", lambda: ("OTHER",))
        assert ui_auth.is_privileged_operator() is False


class TestControllerUrl:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTROLLER_URL", "http://custom:9000")
        assert ui_auth.controller_url() == "http://custom:9000"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("CONTROLLER_URL", raising=False)
        assert ui_auth.controller_url() == "http://mendix-deploy-controller:8080"
