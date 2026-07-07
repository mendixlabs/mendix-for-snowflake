"""Cached data loaders shared across admin UI pages."""
from __future__ import annotations

from pathlib import PurePosixPath

import streamlit as st

from auth import client, current_operator, operator_roles


@st.cache_data(ttl=60)
def _list_apps_cached(operator: str, roles: tuple[str, ...]) -> list[dict]:
    # Keyed by (operator, roles): the controller filters by role membership, so
    # the visible set depends on both. Keeping roles in the key keeps it aligned
    # with get_client's cache key.
    return client().list_apps()


def list_apps() -> list[dict]:
    return _list_apps_cached(current_operator(), operator_roles())


def pad_filename(pad_stage_path: str | None) -> str:
    """The operator-uploaded PAD filename from a stage-relative path like
    apps/{name}/MyReleasePad_20260707.zip. The controller no longer renames
    staged PADs to current.zip (see PLAN item O8), so this is a stable,
    human-readable build identifier operators can use on the Apps page to
    confirm what's actually deployed. "" (never None) when no PAD has been
    deployed yet, so callers can display it directly without an extra check."""
    return PurePosixPath(pad_stage_path).name if pad_stage_path else ""
