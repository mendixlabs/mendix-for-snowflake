"""Apps page: list, inspect, and act on registered Mendix apps."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Streamlit's pages/ entry runs with the parent on sys.path automatically only
# for the entry script. Push the app/ dir on for sibling imports here too.
sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client, operator_roles, operator_roles_error
from controller_client import ControllerError
from data import apps_status_unavailable, egress_warning, list_apps, pad_filename

# apply_branding() runs once in streamlit_app.py, before st.navigation()/pg.run(),
# so it (and the persistent sidebar it builds) applies to every page already.
st.set_page_config(page_title="Apps", layout="wide")
st.title("Apps")

# Cheap, unprivileged, cached (data.egress_warning) signal - never fetches the
# raw ranges or alert config, and safe for every operator regardless of role.
_egress = egress_warning()
if _egress.get("warn"):
    st.warning(
        f"The SPCS egress IP whitelist expires in {_egress.get('days_remaining')} day(s). "
        "See the Infrastructure page's 'Egress IP expiry' section for the fix-up SQL.",
        icon="⚠️",
    )

_TRANSIENT = {"DEPLOYING", "SUSPENDING", "RESUMING"}

# Secondary visual scan aid alongside the status text (additive - text is never
# removed). Covers both service_status (Snowflake's live container state) and
# last_deploy_status (our own last-requested-action record); unrecognized or
# blank values fall back to no icon rather than a misleading one.
_STATUS_ICON = {
    "RUNNING": "🟢",
    "READY": "🟢",
    "SUSPENDED": "⚪",
    "NOT_DEPLOYED": "⚪",
    "PENDING": "🟡",
    "DEPLOYING": "🟡",
    "SUSPENDING": "🟡",
    "RESUMING": "🟡",
    "FAILED": "🔴",
    "INTERNAL_ERROR": "🔴",
    "DELETING": "🔴",
}


def _status_badge(status: str | None) -> str:
    """`status` with a leading color icon, for display only - never feed the
    result back into disable/comparison logic keyed on the raw status string."""
    if not status:
        return status or ""
    icon = _STATUS_ICON.get(status.upper())
    return f"{icon} {status}" if icon else status


def _platform_badge(update_available: bool) -> str:
    return "🆙 Update available" if update_available else ""


def _health_badge(health: dict | None) -> str:
    """Compact health line from a GET .../health response - same icon vocabulary
    as _STATUS_ICON. `health` is None when it hasn't been fetched (fleet table)
    or the fetch failed; both render as "" rather than a misleading icon.

    A container's own status reaches READY only once its readinessProbe passes,
    so "RUNNING but not ready" (containers up, Mendix not yet serving) is
    distinguished from a genuine container error - anything reporting neither
    READY nor RUNNING/PENDING is treated as the latter.
    """
    if not health:
        return ""
    if health.get("ready"):
        return "🟢 Healthy"
    containers = health.get("containers") or []
    errored = [c for c in containers if (c.get("status") or "").upper() not in ("READY", "RUNNING", "PENDING")]
    if errored:
        return "🔴 Container error"
    if (health.get("service_status") or "").upper() == "RUNNING":
        return "🟡 Starting"
    return ""


def _refresh_now() -> None:
    st.cache_data.clear()


def _diff_constants(old: dict, new: dict) -> list[str]:
    """Unified-diff-style lines for changed / added / removed constants."""
    lines: list[str] = []
    for k in sorted(set(old) | set(new)):
        if k not in new:
            lines.append(f"- {k}: {json.dumps(old[k])}")
        elif k not in old:
            lines.append(f"+ {k}: {json.dumps(new[k])}")
        elif old[k] != new[k]:
            lines.append(f"- {k}: {json.dumps(old[k])}")
            lines.append(f"+ {k}: {json.dumps(new[k])}")
    return lines


def _render_bulk_result(last: dict) -> None:
    results = last["results"]
    n_ok = sum(1 for r in results if r["result"] == "ACCEPTED")
    n_fail = len(results) - n_ok
    label = f"Last bulk {last['action']}: {n_ok} accepted, {n_fail} rejected."
    if n_fail == 0:
        st.success(label + " These are async jobs - refresh to see final status.")
    else:
        st.error(label)
    with st.expander("Details"):
        st.dataframe(results, use_container_width=True, hide_index=True)


def _run_bulk(names: list[str], action: str, fn) -> None:
    progress = st.progress(0.0, text=f"{action} 0/{len(names)}")
    results: list[dict] = []
    for i, n in enumerate(names):
        try:
            fn(n)
            results.append({"app": n, "result": "ACCEPTED", "error": ""})
        except ControllerError as e:
            results.append({"app": n, "result": "REJECTED", "error": str(e)})
        except Exception as e:
            results.append({"app": n, "result": "REJECTED", "error": str(e)})
        progress.progress((i + 1) / len(names), text=f"{action} {i+1}/{len(names)}")
    _refresh_now()
    st.session_state["bulk-last-result"] = {"action": action, "results": results}


cols = st.columns([1, 1, 1, 3])
with cols[0]:
    if st.button("Refresh"):
        _refresh_now()
        st.rerun()
with cols[1]:
    auto = st.toggle(
        "Auto-refresh", value=False,
        help="Refresh every 10 seconds. Also refreshes automatically (regardless of "
             "this toggle) while any app has a deploy/suspend/resume operation in flight.",
    )
with cols[2]:
    if st.button("Fleet health", help="Fetch container-level health for every listed app once. "
                                       "Not polled automatically - click again to refresh."):
        try:
            apps_now = list_apps()
        except ControllerError as e:
            st.error(f"Failed to load apps: {e}")
            apps_now = []
        if apps_now:
            progress = st.progress(0.0, text=f"Fleet health 0/{len(apps_now)}")
            health_map: dict[str, dict | None] = {}
            for i, a in enumerate(apps_now):
                try:
                    health_map[a["name"]] = client().get_health(a["name"])
                except ControllerError:
                    health_map[a["name"]] = None
                progress.progress((i + 1) / len(apps_now), text=f"Fleet health {i+1}/{len(apps_now)}")
            st.session_state["fleet-health"] = health_map
        st.rerun()
st.caption("Status is fetched on page load and after each action. Click Refresh to re-poll.")

# Read at decoration time: this drives run_every below, which Streamlit fixes for
# the lifetime of the fragment until the next full script rerun. _apps_table
# recomputes the current value each time it runs and calls st.rerun() (a full
# rerun, not fragment-scoped) whenever it flips, so this cadence catches up
# within one tick instead of waiting for the next full page load.
_has_transient_prior = st.session_state.get("apps-has-transient", False)


@st.fragment(run_every=10 if (auto or _has_transient_prior) else None)
def _apps_table() -> None:
    if auto or _has_transient_prior:
        _refresh_now()
    try:
        apps = list_apps()
    except ControllerError as e:
        st.error(f"Failed to load apps: {e}")
        st.stop()

    prev_transient = st.session_state.get("apps-has-transient", False)
    has_transient_now = any((a.get("last_deploy_status") or "") in _TRANSIENT for a in apps)
    st.session_state["apps-has-transient"] = has_transient_now
    if has_transient_now != prev_transient:
        # Guarded by the != check above: this only fires on an actual flip, so it
        # cannot loop (the next run sees prev_transient already matching).
        st.rerun()

    if apps_status_unavailable():
        st.warning(
            "Could not fetch live service statuses from Snowflake - the statuses "
            "below may be blank or stale even for a healthy fleet. Try Refresh."
        )

    if not apps:
        st.info("No apps registered yet. Use the Register page to add one.")
        st.stop()

    # Health is never fetched for every row on a normal list render (would be an
    # N-query fan-out on every 10s auto-refresh tick) - only shown once the
    # operator has clicked "Fleet health" above, and only reflects that click's
    # snapshot (not re-fetched on the next auto-refresh tick).
    fleet_health = st.session_state.get("fleet-health")

    table_rows = [
        {
            "name": a["name"],
            "service_status": _status_badge(a.get("service_status")),
            "last_deploy_status": _status_badge(a.get("last_deploy_status")),
            "platform": _platform_badge(bool(a.get("platform_update_available"))),
            **({"health": _health_badge(fleet_health.get(a["name"]))} if fleet_health is not None else {}),
            "endpoint_url": a.get("endpoint_url") or "",
            "pad_file": pad_filename(a.get("pad_stage_path")),
            "last_deployed_at": a.get("last_deployed_at") or "",
        }
        for a in apps
    ]

    st.caption(
        "Tick the checkbox at the left edge of a row to open its detail panel. "
        "Tick several to enable bulk actions (suspend / resume / delete)."
    )
    selection = st.dataframe(
        table_rows,
        use_container_width=True,
        hide_index=True,
        selection_mode="multi-row",
        on_select="rerun",
        column_config={
            "endpoint_url": st.column_config.LinkColumn("endpoint_url", width="large"),
            "platform": st.column_config.TextColumn(
                "platform",
                help="Flags an app whose service was last respec'd against an "
                     "older platform image than the one currently running.",
            ),
            "health": st.column_config.TextColumn(
                "health",
                help="Container-level readiness as of the last 'Fleet health' click - "
                     "not live, and absent until that button has been clicked once.",
            ),
            "pad_file": st.column_config.TextColumn(
                "pad_file",
                help="PAD = Portable Application Deployment Archive, Mendix's "
                     "exported deployment package format.",
            ),
        },
        key="apps-dataframe",
    )
    selected_rows = selection.selection.rows if selection and selection.selection else []

    # Dispatch the detail/bulk panel from inside this same fragment. The dataframe's
    # on_select="rerun" only triggers a fragment-scoped rerun (this function re-running
    # in isolation), never a full top-to-bottom script rerun - code that consumed
    # selected_rows/table_rows from module scope below this fragment never saw a fresh
    # selection until some OUTER widget (Refresh, Auto-refresh) forced a full rerun.
    if not selected_rows:
        st.caption(
            "Select one row for a detail panel. Select two or more for bulk actions."
        )
        last_result = st.session_state.get("bulk-last-result")
        if last_result:
            _render_bulk_result(last_result)
        return

    st.divider()

    if len(selected_rows) == 1:
        selected_name = table_rows[selected_rows[0]]["name"]
        _detail_panel(selected_name)
    else:
        selected_names = [table_rows[i]["name"] for i in selected_rows]
        _bulk_panel(apps, selected_names)


@st.fragment(run_every=3)
def _progress_caption(selected_name: str) -> None:
    """Cheap (no warehouse query) live phase text while a background lifecycle
    task is in flight. Its own short-interval fragment so it can tick without
    dragging the whole detail panel - and the main Apps fetch - along with it.
    The caller re-checks the transient status on its own next refresh and simply
    stops calling this once the app is no longer transient."""
    try:
        text = client().get_progress(selected_name).get("progress")
    except ControllerError:
        return
    if text:
        st.caption(f"In progress: {text}")


@st.fragment
def _detail_panel(selected_name: str) -> None:
    try:
        live = client().get_app(selected_name)
    except ControllerError as e:
        st.error(f"Failed to refresh {selected_name}: {e}")
        return

    record = live["app"]
    svc_status = live.get("service_status") or "(unknown)"
    deploy_status = record.get("last_deploy_status") or "(none)"

    st.subheader(selected_name)
    c1, c2, c3 = st.columns(3)
    # Badge the displayed text only - svc_status/deploy_status themselves keep
    # driving the disabled= checks on the action buttons below.
    c1.metric("Service status", _status_badge(svc_status))
    c2.metric("Deploy status", _status_badge(deploy_status))
    c3.metric("Resource tier", record.get("resource_tier") or "")
    st.caption(
        "Service status is Snowflake's live container state - it can show `PENDING` "
        "briefly during any restart, including ones this app didn't initiate. "
        "Deploy status is our own record of the last action requested here and only "
        "changes when you trigger one."
    )
    if deploy_status == "FAILED":
        st.caption(
            f"Failed during {record.get('failed_operation') or 'an unrecorded operation'}: "
            f"{record.get('status_detail') or 'no further detail was recorded.'}"
        )
    if deploy_status in _TRANSIENT:
        _progress_caption(selected_name)

    # Eager fetch for the one selected app only (a single SHOW SERVICE CONTAINERS
    # call) - never for every row of the table, see the Fleet health button above.
    try:
        health = client().get_health(selected_name)
    except ControllerError:
        health = None
    health_badge = _health_badge(health)
    if health_badge:
        st.caption(f"{health_badge} — container-level health (readinessProbe), distinct from service status above.")

    if record.get("endpoint_url"):
        st.write(f"Endpoint: {record['endpoint_url']}")
    st.write(f"Database: `{record.get('pg_database')}`  |  "
             f"Caller rights: `{record.get('use_caller_rights')}`  |  "
             f"Last deployed: `{record.get('last_deployed_at') or '—'}`")
    st.write(f"Deployed PAD: `{record.get('pad_stage_path') or '(none deployed yet)'}`")

    action_cols = st.columns(4)
    with action_cols[0]:
        if st.button("Redeploy", key=f"redeploy-{selected_name}",
                     disabled=deploy_status in _TRANSIENT, use_container_width=True):
            try:
                client().trigger_deploy(selected_name)
                _refresh_now()
                st.rerun()
            except ControllerError as e:
                missing = e.missing_constants()
                if e.status_code == 422 and missing:
                    # The PAD declares constants with no value yet. Prefill them into
                    # the Constants editor (keeping any values already set) and open it.
                    current = record.get("constants") or {}
                    merged = {**{m: "" for m in missing}, **current}
                    st.session_state[f"constants-{selected_name}"] = json.dumps(merged, indent=2)
                    st.session_state[f"open-constants-{selected_name}"] = True
                    st.session_state[f"constants-missing-{selected_name}"] = missing
                    st.rerun()
                else:
                    st.error(str(e))
    with action_cols[1]:
        # Suspend causes real downtime, so it gets the same warn-then-confirm
        # popover as bulk suspend below, instead of firing on a single click.
        with st.popover("Suspend", use_container_width=True,
                         disabled=svc_status == "SUSPENDED" or deploy_status in _TRANSIENT):
            st.warning(
                f"This will suspend `{selected_name}`. Its endpoint "
                f"({record.get('endpoint_url') or '(none)'}) will become unreachable "
                "until resumed."
            )
            if st.button("Confirm suspend", key=f"suspend-go-{selected_name}", type="primary"):
                try:
                    client().suspend(selected_name)
                    _refresh_now()
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))
    with action_cols[2]:
        if st.button("Resume", key=f"resume-{selected_name}",
                     disabled=svc_status == "RUNNING" or deploy_status in _TRANSIENT,
                     use_container_width=True):
            try:
                client().resume(selected_name)
                _refresh_now()
                st.rerun()
            except ControllerError as e:
                st.error(str(e))
    with action_cols[3]:
        with st.popover("Delete", use_container_width=True):
            st.warning(f"This will DROP service `{record.get('service_name')}`, "
                       "remove the registry entry, and delete the app's schema with its "
                       "secrets and **uploaded files** (filestorage stage). "
                       "The PG database is NOT deleted.")
            typed = st.text_input(
                f"Type `{selected_name}` to confirm:",
                key=f"delete-confirm-{selected_name}",
            )
            if st.button("Delete permanently", key=f"delete-go-{selected_name}",
                         type="primary", disabled=(typed != selected_name)):
                try:
                    client().delete_app(selected_name)
                    _refresh_now()
                    st.success(f"Deleted {selected_name}.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    if record.get("platform_update_available"):
        with st.popover("Apply platform update", use_container_width=True):
            st.warning(
                f"This will respec `{selected_name}` onto the current platform image with no "
                "other change. The service restarts and active end-user sessions are dropped."
            )
            if st.button("Confirm platform update", key=f"platform-update-go-{selected_name}",
                         type="primary", disabled=deploy_status in _TRANSIENT):
                try:
                    client().apply_platform_update(selected_name)
                    _refresh_now()
                    st.success("Platform update triggered. Service is restarting.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    with st.expander("Constants", expanded=st.session_state.get(f"open-constants-{selected_name}", False)):
        missing_hint = st.session_state.get(f"constants-missing-{selected_name}")
        if missing_hint:
            st.warning(
                "This PAD requires values for the constants below (prefilled with empty "
                "values). Set each value, click **Save constants**, then **Redeploy**:\n"
                + "\n".join(f"- `{m}`" for m in missing_hint)
            )
        st.caption(
            "Values are stored as Snowflake secrets and load here masked as "
            "`<HIDDEN>`. Leave a value as `<HIDDEN>` to keep it; overwrite it "
            "to change it. (`<HIDDEN>` is reserved and cannot be a real value.)"
        )
        current = record.get("constants") or {}
        constants_key = f"constants-{selected_name}"
        # Seed the editor from the stored constants exactly once, then let the widget
        # own its value via `key` alone. Passing `value=` to a keyed widget inside this
        # fragment reset the field on the first blur; seeding session_state directly
        # avoids that. A prefill written by the Redeploy 422 handler lands in the same
        # slot, so the guard preserves it too.
        if constants_key not in st.session_state:
            st.session_state[constants_key] = json.dumps(current, indent=2)
        edited = st.text_area(
            "Constants (JSON object: name -> value)",
            height=250,
            key=constants_key,
        )

        parsed: dict | None = None
        parse_error: str | None = None
        try:
            candidate = json.loads(edited)
            if isinstance(candidate, dict):
                parsed = candidate
            else:
                parse_error = "Constants must be a JSON object."
        except json.JSONDecodeError as e:
            parse_error = f"Invalid JSON: {e}"

        diff_lines: list[str] = []
        if parse_error:
            st.error(parse_error)
        elif parsed is not None:
            diff_lines = _diff_constants(current, parsed)
            if diff_lines:
                st.caption("Pending changes:")
                st.code("\n".join(diff_lines), language="diff")
            else:
                st.caption("No changes to apply.")

        save_disabled = (
            deploy_status in _TRANSIENT
            or parsed is None
            or not diff_lines
        )
        if st.button("Save constants", key=f"save-constants-{selected_name}",
                     disabled=save_disabled):
            try:
                client().update_constants(selected_name, parsed)
                _refresh_now()
                # Clear the prefill/open hints now that values are saved.
                st.session_state.pop(f"open-constants-{selected_name}", None)
                st.session_state.pop(f"constants-missing-{selected_name}", None)
                st.success("Constants saved. Click Redeploy to deploy the PAD with them.")
                st.rerun()
            except ControllerError as e:
                st.error(str(e))

    with st.expander("License"):
        if record.get("licensed"):
            st.caption(f"Licensed — ID `{record.get('license_id')}`.")
        else:
            st.caption(
                "Trial: max 6 concurrent users, unlimited named users; the runtime "
                "restarts every 2-4 hours."
            )
        st.warning(
            "Saving or removing a license restarts the service — the runtime only "
            "checks the license at startup."
        )
        new_license_id = st.text_input(
            "License ID",
            value=record.get("license_id") or "",
            key=f"license-id-{selected_name}",
        )
        new_license_key = st.text_input(
            "License key",
            type="password",
            value="",
            key=f"license-key-{selected_name}",
            help="Write-only: never prefilled or read back, even after saving.",
        )
        if st.button(
            "Save license",
            key=f"license-save-{selected_name}",
            disabled=(deploy_status in _TRANSIENT or not new_license_id or not new_license_key),
        ):
            try:
                client().update_license(selected_name, new_license_id, new_license_key)
                _refresh_now()
                st.success("License saved. Service is restarting.")
                st.rerun()
            except ControllerError as e:
                st.error(str(e))

        if record.get("license_id"):
            remove_confirm = st.checkbox(
                "Confirm removal (app reverts to trial after restart)",
                key=f"license-remove-confirm-{selected_name}",
            )
            if st.button(
                "Remove license",
                key=f"license-remove-{selected_name}",
                disabled=(deploy_status in _TRANSIENT or not remove_confirm),
            ):
                try:
                    client().delete_license(selected_name)
                    _refresh_now()
                    st.success("License removed. Service is restarting.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    with st.expander("End-user role mapping"):
        if not record.get("use_caller_rights"):
            st.warning(
                "Role mapping requires caller's rights (executeAsCaller), which is "
                "currently OFF for this app. End-users always get the default "
                "userrole until it is enabled in the Service spec expander below. "
                "The mapping is still saved and takes effect once caller's rights "
                "is turned on."
            )

        ur = record.get("user_roles") or []
        if ur:
            st.caption("Userroles detected in the deployed PAD: " + ", ".join(f"`{r}`" for r in ur))
        else:
            st.caption("No PAD deployed yet; mapping values cannot be validated against detected userroles.")

        st.caption(
            "Keys are Snowflake account role names (stored uppercase). A user "
            "holding several mapped roles gets all of the mapped userroles; a "
            "user holding none gets the default userrole. Changes apply at the "
            "user's next login. Saving or removing the mapping restarts the service."
        )

        available_roles = sorted(operator_roles())
        if available_roles:
            shown_roles = available_roles[:10]
            roles_line = "Your available Snowflake account roles: " + ", ".join(f"`{r}`" for r in shown_roles)
            if len(available_roles) > len(shown_roles):
                roles_line += f" (+{len(available_roles) - len(shown_roles)} more)"
            st.caption(roles_line)
        else:
            _roles_err = operator_roles_error()
            if _roles_err:
                st.caption(
                    "Could not resolve your available Snowflake roles - this usually means "
                    "Setup step 5b's caller-token specification has not been approved yet."
                )
                with st.expander("Why?"):
                    st.code(_roles_err)

        current_mapping = record.get("role_mapping") or {}
        rolemap_key = f"rolemap-{selected_name}"
        # Same seed-once pattern as the Constants editor above: seed session_state
        # directly rather than passing value= to a keyed widget in this fragment.
        if rolemap_key not in st.session_state:
            if not current_mapping and ur:
                seed = {f"<SNOWFLAKE_ROLE_FOR_{role.upper().replace(' ', '_')}>": role for role in ur}
            else:
                seed = current_mapping
            st.session_state[rolemap_key] = json.dumps(seed, indent=2)
        edited_mapping = st.text_area(
            "Role mapping (JSON object: Snowflake account role -> Mendix userrole)",
            height=200,
            key=rolemap_key,
        )

        parsed_mapping: dict | None = None
        mapping_parse_error: str | None = None
        try:
            candidate = json.loads(edited_mapping)
            if isinstance(candidate, dict):
                parsed_mapping = candidate
            else:
                mapping_parse_error = "Role mapping must be a JSON object."
        except json.JSONDecodeError as e:
            mapping_parse_error = f"Invalid JSON: {e}"

        mapping_diff_lines: list[str] = []
        if mapping_parse_error:
            st.error(mapping_parse_error)
        elif parsed_mapping is not None:
            if any(k.startswith("<SNOWFLAKE_ROLE_FOR_") for k in parsed_mapping):
                st.warning(
                    "One or more `<SNOWFLAKE_ROLE_FOR_...>` placeholder keys are still present. "
                    "Saving is not blocked, but a placeholder never matches a real Snowflake role, "
                    "so that userrole stays unmapped - affected users get the app's default "
                    "userrole instead. Either replace the placeholder with a real account role "
                    "name to map it, or delete that line entirely if you don't want to map it yet "
                    "- both have the same effect."
                )
            mapping_diff_lines = _diff_constants(current_mapping, parsed_mapping)
            if mapping_diff_lines:
                st.caption("Pending changes:")
                st.code("\n".join(mapping_diff_lines), language="diff")
            else:
                st.caption("No changes to apply.")
            if ur:
                unmapped_targets = sorted(set(parsed_mapping.values()) - set(ur))
                if unmapped_targets:
                    st.warning(
                        "These target userroles are not in the detected list and "
                        "will be rejected by the server: " + ", ".join(f"`{r}`" for r in unmapped_targets)
                    )

        mapping_save_disabled = (
            deploy_status in _TRANSIENT
            or parsed_mapping is None
            or not mapping_diff_lines
        )
        if st.button("Save role mapping", key=f"rolemap-save-{selected_name}",
                     disabled=mapping_save_disabled):
            try:
                resp = client().update_role_mapping(selected_name, parsed_mapping)
                for w in resp.get("warnings") or []:
                    st.warning(w)
                _refresh_now()
                st.success("Role mapping saved. Service is restarting.")
                st.rerun()
            except ControllerError as e:
                unknown, detected = e.unknown_userroles()
                if unknown:
                    st.error(
                        "Mapping targets userroles not present in the deployed PAD: "
                        + ", ".join(f"`{r}`" for r in unknown)
                        + ". Detected userroles: "
                        + (", ".join(f"`{r}`" for r in detected) if detected else "(none)")
                    )
                else:
                    st.error(str(e))

        if current_mapping:
            st.caption(
                "Removing clears the entire mapping (every entry at once), not just one role - "
                "to drop a single role, delete its line in the JSON above and click **Save role "
                "mapping** instead. Either way, changes are not retroactive: a user already "
                "logged in keeps their current session's role until they log in again."
            )
            rolemap_remove_confirm = st.checkbox(
                "Confirm removal (end-users revert to the default userrole after restart)",
                key=f"rolemap-remove-confirm-{selected_name}",
            )
            if st.button(
                "Remove role mapping",
                key=f"rolemap-remove-{selected_name}",
                disabled=(deploy_status in _TRANSIENT or not rolemap_remove_confirm),
            ):
                try:
                    client().delete_role_mapping(selected_name)
                    _refresh_now()
                    st.success("Role mapping removed. Service is restarting.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    with st.expander("Service spec"):
        st.warning(
            "Editing the spec restarts the service. Active end-user sessions on "
            "this app will be dropped when the restart happens."
        )

        tier_options = ["small", "medium", "large"]
        current_tier = record.get("resource_tier") or "medium"
        new_tier = st.selectbox(
            "Resource tier",
            options=tier_options,
            index=tier_options.index(current_tier) if current_tier in tier_options else 1,
            key=f"spec-tier-{selected_name}",
        )

        current_caller = bool(record.get("use_caller_rights", False))
        new_caller = st.checkbox(
            "Enable caller's rights (executeAsCaller)",
            value=current_caller,
            key=f"spec-caller-{selected_name}",
            help="When on, the service receives the operator's OAuth token on each request.",
        )

        tier_changed = new_tier != current_tier
        caller_changed = new_caller != current_caller
        spec_changed = tier_changed or caller_changed

        if not spec_changed:
            st.caption("No changes.")
        else:
            spec_diff: list[str] = []
            if tier_changed:
                spec_diff.append(f"- resource_tier: {current_tier}")
                spec_diff.append(f"+ resource_tier: {new_tier}")
            if caller_changed:
                spec_diff.append(f"- use_caller_rights: {current_caller}")
                spec_diff.append(f"+ use_caller_rights: {new_caller}")
            st.caption("Pending changes:")
            st.code("\n".join(spec_diff), language="diff")

            if new_caller and not current_caller:
                st.warning(
                    "Turning caller's rights ON requires that operators' Snowflake "
                    "roles hold the appropriate grants. Without them, in-app queries fail."
                )

            spec_confirm = st.text_input(
                f"Type `{selected_name}` to confirm:",
                key=f"spec-confirm-{selected_name}",
            )
            if st.button(
                "Apply spec changes",
                key=f"spec-apply-{selected_name}",
                type="primary",
                disabled=(spec_confirm != selected_name) or (deploy_status in _TRANSIENT),
            ):
                payload: dict = {}
                if tier_changed:
                    payload["resource_tier"] = new_tier
                if caller_changed:
                    payload["use_caller_rights"] = new_caller
                try:
                    client().update_spec(selected_name, payload)
                    _refresh_now()
                    st.success("Spec update triggered. Service will restart.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    with st.expander("External access"):
        try:
            eai_slots = client().get_external_access_slots()
        except ControllerError as e:
            st.error(f"Failed to load external access slots: {e}")
            eai_slots = []

        if not eai_slots:
            st.caption("No external access slots configured.")
        else:
            st.warning(
                "Changing external access restarts the service. Active end-user "
                "sessions on this app will be dropped when the restart happens."
            )
            current_slots = set(record.get("external_access") or [])
            bound_keys = {s["key"] for s in eai_slots if s.get("bound")}
            stale = current_slots - bound_keys
            if stale:
                st.warning(
                    "This app is recorded as attached to slot(s) that are no longer "
                    "bound at the account level: " + ", ".join(f"`{s}`" for s in sorted(stale)) +
                    ". They stay attached to the running service until the next "
                    "external-access save or rollback, which drops them silently."
                )

            new_selection: list[str] = []
            for slot in eai_slots:
                key = slot["key"]
                checked = st.checkbox(
                    slot.get("label") or key,
                    value=key in current_slots,
                    disabled=not slot.get("bound"),
                    help=None if slot.get("bound") else "not bound yet - see Setup page",
                    key=f"eai-{selected_name}-{key}",
                )
                if checked:
                    new_selection.append(key)

            added = sorted(set(new_selection) - current_slots)
            removed = sorted(current_slots - set(new_selection))
            eai_diff = [f"- {k}" for k in removed] + [f"+ {k}" for k in added]

            if not eai_diff:
                st.caption("No changes.")
            else:
                st.caption("Pending changes:")
                st.code("\n".join(eai_diff), language="diff")

                eai_confirm = st.text_input(
                    f"Type `{selected_name}` to confirm:",
                    key=f"eai-confirm-{selected_name}",
                )
                if st.button(
                    "Apply external access changes",
                    key=f"eai-apply-{selected_name}",
                    type="primary",
                    disabled=(eai_confirm != selected_name) or (deploy_status in _TRANSIENT),
                ):
                    try:
                        client().update_external_access(selected_name, new_selection)
                        _refresh_now()
                        st.success("External access update triggered. Service will restart.")
                        st.rerun()
                    except ControllerError as e:
                        st.error(str(e))

    with st.expander("History"):
        try:
            history = client().list_history(selected_name)
        except ControllerError as e:
            st.error(f"Failed to load history: {e}")
            history = []

        if not history:
            st.caption("No deploy history recorded yet.")
        else:
            st.caption(
                "Newest first. Each row is a snapshot of the configuration a "
                "deploy/constants/spec/license/role-mapping/platform-update/rollback "
                "attempt applied - not a log of every field, just this app's PAD, "
                "resource tier, caller rights, license, and role mapping at the time."
            )
            history_rows = [
                {
                    "ts": h.get("ts") or "",
                    "operation": h.get("operation") or "",
                    "status": h.get("status") or "",
                    "pad_file": pad_filename(h.get("pad_stage_path")),
                    "detail": h.get("detail") or "",
                }
                for h in history
            ]
            st.dataframe(history_rows, use_container_width=True, hide_index=True)

            # Newest READY row - list_for_app is already newest-first, so the first
            # match here is exactly what the controller's own last_success() targets.
            rollback_target = next((h for h in history if h.get("status") == "READY"), None)
            if rollback_target is None:
                st.caption("No successful deployment recorded yet; nothing to roll back to.")
            else:
                target_label = f"{rollback_target.get('ts') or '?'} ({pad_filename(rollback_target.get('pad_stage_path')) or '(no PAD)'})"
                with st.popover(f"Roll back to {target_label}", use_container_width=True):
                    st.warning(
                        f"This will roll `{selected_name}` back to the deployment configuration "
                        f"(PAD, resource tier, caller rights, license, role mapping) recorded at "
                        f"{rollback_target.get('ts') or '?'}. The service restarts and active "
                        "end-user sessions are dropped."
                    )
                    st.caption(
                        "Constant values are never restored: the app keeps its CURRENT "
                        "constant values, since only constant names (not values) are ever "
                        "recorded in history."
                    )
                    rollback_confirm = st.text_input(
                        f"Type `{selected_name}` to confirm:",
                        key=f"rollback-confirm-{selected_name}",
                    )
                    if st.button(
                        "Confirm rollback",
                        key=f"rollback-go-{selected_name}",
                        type="primary",
                        disabled=(rollback_confirm != selected_name) or (deploy_status in _TRANSIENT),
                    ):
                        try:
                            client().rollback(selected_name)
                            _refresh_now()
                            st.success("Rollback triggered. Service is restarting.")
                            st.rerun()
                        except ControllerError as e:
                            st.error(str(e))

            # Per-entry rollback: every READY row (not just the newest one above)
            # gets its own action, so an operator can target an older-but-still-good
            # configuration directly instead of only the latest success.
            ready_entries = [h for h in history if h.get("status") == "READY"]
            if ready_entries:
                st.caption("Or roll back to a specific entry:")
                for h in ready_entries:
                    entry_id = h.get("id")
                    entry_label = f"{h.get('ts') or '?'} ({pad_filename(h.get('pad_stage_path')) or '(no PAD)'})"
                    with st.popover(f"Roll back to this entry — {entry_label}", use_container_width=True):
                        st.warning(
                            f"This will roll `{selected_name}` back to the deployment configuration "
                            f"(PAD, resource tier, caller rights, license, role mapping) recorded at "
                            f"{h.get('ts') or '?'}. The service restarts and active end-user "
                            "sessions are dropped."
                        )
                        st.caption(
                            "Constant values are never restored: the app keeps its CURRENT "
                            "constant values, since only constant names (not values) are ever "
                            "recorded in history."
                        )
                        entry_confirm = st.text_input(
                            f"Type `{selected_name}` to confirm:",
                            key=f"rollback-entry-confirm-{selected_name}-{entry_id}",
                        )
                        if st.button(
                            "Confirm rollback",
                            key=f"rollback-entry-go-{selected_name}-{entry_id}",
                            type="primary",
                            disabled=(entry_confirm != selected_name) or (deploy_status in _TRANSIENT),
                        ):
                            try:
                                client().rollback(selected_name, entry_id=entry_id)
                                _refresh_now()
                                st.success("Rollback triggered. Service is restarting.")
                                st.rerun()
                            except ControllerError as e:
                                st.error(str(e))


@st.fragment
def _bulk_panel(apps: list[dict], names: list[str]) -> None:
    selected_apps = [a for a in apps if a["name"] in names]
    flagged_names = [a["name"] for a in selected_apps if a.get("platform_update_available")]
    st.subheader(f"Bulk actions — {len(names)} apps selected")
    st.write("Selected: " + ", ".join(f"`{n}`" for n in names))

    action_cols = st.columns(4)

    with action_cols[0]:
        with st.popover(f"Suspend ({len(names)})", use_container_width=True):
            st.warning(
                f"These {len(names)} services will be suspended. "
                "Their endpoints will become unreachable until resumed."
            )
            for a in selected_apps:
                ep = a.get("endpoint_url") or "(none)"
                st.write(f"- `{a['name']}` → {ep}")
            if st.button("Confirm suspend", key="bulk-suspend-go", type="primary"):
                _run_bulk(names, "suspend", lambda n: client().suspend(n))
                st.rerun()

    with action_cols[1]:
        with st.popover(f"Resume ({len(names)})", use_container_width=True):
            st.info(f"These {len(names)} services will be resumed.")
            for a in selected_apps:
                st.write(f"- `{a['name']}`")
            if st.button("Confirm resume", key="bulk-resume-go", type="primary"):
                _run_bulk(names, "resume", lambda n: client().resume(n))
                st.rerun()

    with action_cols[2]:
        with st.popover(f"Delete ({len(names)})", use_container_width=True):
            st.warning(
                f"These {len(names)} services will be DROPPED and removed from the "
                "registry, and each app's schema is deleted with its secrets and "
                "**uploaded files** (filestorage stage). PG databases are NOT deleted."
            )
            for a in selected_apps:
                st.write(f"- `{a['name']}` (service: `{a['service_name']}`)")
            expected = f"delete {len(names)}"
            confirm_text = st.text_input(
                f"Type `{expected}` to confirm:",
                key="bulk-delete-confirm-text",
            )
            if st.button("Delete permanently", key="bulk-delete-go",
                         type="primary",
                         disabled=(confirm_text != expected)):
                _run_bulk(names, "delete", lambda n: client().delete_app(n))
                st.rerun()

    with action_cols[3]:
        with st.popover(f"Apply platform update ({len(flagged_names)})", use_container_width=True,
                        disabled=not flagged_names):
            st.warning(
                f"These {len(flagged_names)} flagged services will be respec'd onto the current "
                "platform image with no other change and will restart, dropping active sessions. "
                "Apps not flagged for a platform update are skipped."
            )
            for n in flagged_names:
                st.write(f"- `{n}`")
            if st.button("Confirm platform update", key="bulk-platform-update-go", type="primary"):
                _run_bulk(flagged_names, "platform update", lambda n: client().apply_platform_update(n))
                st.rerun()

    last_result = st.session_state.get("bulk-last-result")
    if last_result:
        _render_bulk_result(last_result)


# Called only here, at the true end of the module, after every function it can
# reach (_detail_panel, _bulk_panel, _render_bulk_result) is already defined.
# _apps_table's body calls _detail_panel/_bulk_panel by name at run time - if this
# call sat any earlier in the file (as it briefly did), a full top-to-bottom
# script rerun (e.g. toggling Auto-refresh with a row already selected) would
# reach this line before the script had executed the def statements below it,
# raising NameError. See the live-found bug this fixed.
_apps_table()

