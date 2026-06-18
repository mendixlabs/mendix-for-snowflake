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
from controller_client import ControllerError

st.set_page_config(page_title="Apps", layout="wide")
st.title("Apps")

_TRANSIENT = {"DEPLOYING", "SUSPENDING", "RESUMING", "STARTING"}


def _refresh_now() -> None:
    st.cache_data.clear()


@st.cache_data(ttl=5)
def _load_apps() -> list[dict]:
    return client().list_apps()


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
    apps = _load_apps()
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
            st.warning(f"This will DROP service `{record.get('service_name')}` "
                       "and remove the registry entry. The PG database and stage are NOT deleted.")
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

    with st.expander("Constants"):
        current = record.get("constants") or {}
        edited = st.text_area(
            "Constants (JSON object: name -> value)",
            value=json.dumps(current, indent=2),
            height=250,
            key=f"constants-{selected_name}",
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
                st.success("Constants update triggered. Service will restart.")
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
                f"These {len(names)} services will be DROPPED and removed "
                "from the registry. PG databases and stages are NOT deleted."
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
