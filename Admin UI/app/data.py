"""Cached data loaders shared across admin UI pages."""
from __future__ import annotations

from pathlib import PurePosixPath

import streamlit as st

from auth import client, current_operator, operator_roles
from controller_client import ControllerError


@st.cache_data(ttl=60)
def _list_apps_cached(operator: str, roles: tuple[str, ...]) -> list[dict]:
    # Keyed by (operator, roles): the controller filters by role membership, so
    # the visible set depends on both. Keeping roles in the key keeps it aligned
    # with get_client's cache key.
    apps, status_unavailable = client().list_apps()
    st.session_state["apps_status_unavailable"] = status_unavailable
    return apps


def list_apps() -> list[dict]:
    return _list_apps_cached(current_operator(), operator_roles())


def apps_status_unavailable() -> bool:
    """True when the service-status query behind the last apps fetch (possibly
    a cached copy) failed outright - every app's service_status reads None in
    that response regardless of whether the fleet itself is healthy."""
    return bool(st.session_state.get("apps_status_unavailable"))


@st.cache_data(ttl=300)
def _egress_warning_cached(operator: str, roles: tuple[str, ...]) -> dict:
    # GET /apps returns a bare list, not an object, so there's nowhere to carry
    # an extra top-level "egress is expiring" field without changing every
    # existing caller of it - a dedicated GET /system/egress-warning is
    # cheaper to add than reshaping that response. The value itself only
    # changes once a day (egress_watch's own loop cadence), so a 300s TTL
    # keeps this off the Apps page's 10s auto-refresh tick entirely, unlike
    # _list_apps_cached's 60s (apps can change status far more often).
    try:
        return client().get_egress_warning()
    except ControllerError:
        return {"warn": False, "days_remaining": None}


def egress_warning() -> dict:
    return _egress_warning_cached(current_operator(), operator_roles())


def pad_filename(pad_stage_path: str | None) -> str:
    """The operator-uploaded PAD filename from a stage-relative path like
    apps/{name}/MyReleasePad_20260707.zip. The controller no longer renames
    staged PADs to current.zip (see PLAN item O8), so this is a stable,
    human-readable build identifier operators can use on the Apps page to
    confirm what's actually deployed. "" (never None) when no PAD has been
    deployed yet, so callers can display it directly without an extra check."""
    return PurePosixPath(pad_stage_path).name if pad_stage_path else ""
