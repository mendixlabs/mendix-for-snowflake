"""Cached data loaders shared across admin UI pages."""
from __future__ import annotations

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
