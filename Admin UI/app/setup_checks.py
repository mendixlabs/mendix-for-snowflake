"""Existence/readiness checks for the consumer-owned prerequisites the Native App
cannot create itself: the Snowflake-managed Postgres instance, the egress EAI, and
the PG credential secret bound as a reference.

The checks run in a caller-rights session (auth.open_caller_session), so they
reflect exactly what the operator's own roles can see. References themselves are
app-scoped and cannot be probed from the operator session; their binding is
implied by this admin UI being reachable at all, because the services start only
after both references bind (setup_script.sql::maybe_start_services).
"""
from __future__ import annotations

from dataclasses import dataclass

from auth import open_caller_session


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str


def _rows(cur) -> list[dict]:
    """Fetch the current result set as a list of column-name-keyed dicts."""
    cols = [c[0].lower() for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _find(row: dict, *needles: str) -> str | None:
    """Return the first value whose column name contains any of the needles."""
    for key, value in row.items():
        if any(n in key for n in needles):
            return str(value)
    return None


def _check_pg_instance(cur, instance: str) -> CheckResult:
    label = f"Postgres instance `{instance}`"
    cur.execute(f"SHOW POSTGRES INSTANCES LIKE '{instance}'")
    rows = _rows(cur)
    if not rows:
        return CheckResult(label, False, "not found (create it as ACCOUNTADMIN, step 1)")
    state = _find(rows[0], "state", "status") or "unknown"
    ok = "READY" in state.upper() or "ACTIVE" in state.upper()
    return CheckResult(label, ok, f"state = {state}")


def _check_eai(cur, eai: str) -> CheckResult:
    label = f"External access integration `{eai}`"
    cur.execute(f"SHOW EXTERNAL ACCESS INTEGRATIONS LIKE '{eai}'")
    rows = _rows(cur)
    if not rows:
        return CheckResult(label, False, "not found (create it as ACCOUNTADMIN, step 2)")
    enabled = (_find(rows[0], "enabled") or "").lower()
    ok = enabled in ("true", "")  # some columns omit enabled; presence is enough
    return CheckResult(label, ok, f"enabled = {enabled or 'present'}")


def _check_secret(cur, secret_fqn: str) -> CheckResult:
    label = f"PG credential secret `{secret_fqn}`"
    parts = secret_fqn.split(".")
    name = parts[-1]
    if len(parts) == 3:
        scope = f" IN SCHEMA {parts[0]}.{parts[1]}"
    elif len(parts) == 2:
        scope = f" IN SCHEMA {parts[0]}"
    else:
        scope = ""
    cur.execute(f"SHOW SECRETS LIKE '{name}'{scope}")
    rows = _rows(cur)
    if not rows:
        return CheckResult(label, False, "not found (create it as ACCOUNTADMIN, step 4)")
    kind = _find(rows[0], "secret_type", "type") or "present"
    return CheckResult(label, True, f"type = {kind}")


def run_checks(instance: str, eai: str, secret_fqn: str) -> list[CheckResult]:
    """Verify the three consumer-owned prerequisites exist and look healthy.

    Returns one CheckResult per prerequisite. If no caller-rights session is
    available, returns a single failing result explaining why.
    """
    conn = open_caller_session()
    if conn is None:
        return [CheckResult(
            "Caller-rights session",
            False,
            "no caller token available - is this service running with executeAsCaller?",
        )]
    try:
        cur = conn.cursor()
        results: list[CheckResult] = []
        for fn, arg in (
            (_check_pg_instance, instance),
            (_check_eai, eai),
            (_check_secret, secret_fqn),
        ):
            try:
                results.append(fn(cur, arg))
            except Exception as e:  # insufficient privilege, object scope, etc.
                results.append(CheckResult(fn.__name__, False, f"check failed: {e}"))
        return results
    finally:
        conn.close()
