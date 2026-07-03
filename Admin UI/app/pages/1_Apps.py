"""Apps page: list, inspect, and act on registered Mendix apps."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Streamlit's pages/ entry runs with the parent on sys.path automatically only
# for the entry script. Push the app/ dir on for sibling imports here too.
sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from branding import apply_branding
from controller_client import ControllerError
from data import list_apps

st.set_page_config(page_title="Apps", layout="wide")
apply_branding()
st.title("Apps")

_TRANSIENT = {"DEPLOYING", "SUSPENDING", "RESUMING"}


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
    n_ok = sum(1 for r in results if r["result"] == "OK")
    n_fail = len(results) - n_ok
    label = f"Last bulk {last['action']}: {n_ok} succeeded, {n_fail} failed."
    if n_fail == 0:
        st.success(label + " Click Refresh above to update statuses.")
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
            results.append({"app": n, "result": "OK", "error": ""})
        except ControllerError as e:
            results.append({"app": n, "result": "FAILED", "error": str(e)})
        except Exception as e:
            results.append({"app": n, "result": "FAILED", "error": str(e)})
        progress.progress((i + 1) / len(names), text=f"{action} {i+1}/{len(names)}")
    _refresh_now()
    st.session_state["bulk-last-result"] = {"action": action, "results": results}


if st.button("Refresh"):
    _refresh_now()
    st.rerun()
st.caption("Status is fetched on page load and after each action. Click Refresh to re-poll.")

try:
    apps = list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

if not apps:
    st.info("No apps registered yet. Use the Register page to add one.")
    st.stop()

table_rows = [
    {
        "name": a["name"],
        "service_status": a.get("service_status") or "",
        "last_deploy_status": a.get("last_deploy_status") or "",
        "endpoint_url": a.get("endpoint_url") or "",
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
        "endpoint_url": st.column_config.LinkColumn("endpoint_url"),
    },
    key="apps-dataframe",
)

selected_rows = selection.selection.rows if selection and selection.selection else []


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
    c1.metric("Service status", svc_status)
    c2.metric("Deploy status", deploy_status)
    c3.metric("Resource tier", record.get("resource_tier") or "")

    if record.get("endpoint_url"):
        st.write(f"Endpoint: {record['endpoint_url']}")
    st.write(f"Database: `{record.get('pg_database')}`  |  "
             f"Caller rights: `{record.get('use_caller_rights')}`  |  "
             f"Last deployed: `{record.get('last_deployed_at') or '—'}`")

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
        if st.button("Suspend", key=f"suspend-{selected_name}",
                     disabled=svc_status == "SUSPENDED" or deploy_status in _TRANSIENT,
                     use_container_width=True):
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

        current_mapping = record.get("role_mapping") or {}
        rolemap_key = f"rolemap-{selected_name}"
        # Same seed-once pattern as the Constants editor above: seed session_state
        # directly rather than passing value= to a keyed widget in this fragment.
        if rolemap_key not in st.session_state:
            st.session_state[rolemap_key] = json.dumps(current_mapping, indent=2)
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
                st.error(str(e))

        if current_mapping:
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


@st.fragment
def _bulk_panel(names: list[str]) -> None:
    selected_apps = [a for a in apps if a["name"] in names]
    st.subheader(f"Bulk actions — {len(names)} apps selected")
    st.write("Selected: " + ", ".join(f"`{n}`" for n in names))

    action_cols = st.columns(3)

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

    last_result = st.session_state.get("bulk-last-result")
    if last_result:
        _render_bulk_result(last_result)


if not selected_rows:
    st.caption(
        "Select one row for a detail panel. Select two or more for bulk actions."
    )
    last_result = st.session_state.get("bulk-last-result")
    if last_result:
        _render_bulk_result(last_result)
    st.stop()

st.divider()

if len(selected_rows) == 1:
    selected_name = table_rows[selected_rows[0]]["name"]
    _detail_panel(selected_name)
else:
    selected_names = [table_rows[i]["name"] for i in selected_rows]
    _bulk_panel(selected_names)
