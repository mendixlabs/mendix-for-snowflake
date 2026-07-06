"""Detect and approve the app's request for extended caller-token validity.

The app requests SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800 for its own services
via an app specification (setup_script.sql, section 3b) rather than the old
account-level ALTER ACCOUNT workaround. The consumer approves the request once
(Snowsight permissions tab, or ALTER APPLICATION ... APPROVE SPECIFICATION);
approval cascades to every service the app owns, present and future.

Mirrors setup_checks.py: open the caller session, run SQL, close in finally,
catch per-operation errors and return a dataclass rather than raising.
"""
from __future__ import annotations

from dataclasses import dataclass

from auth import open_caller_session
from setup_checks import _find, _rows

_SPEC_NAME = "caller_token_spec"
_SETTING_NAME = "SERVICE_CALLER_TOKEN_VALIDITY_SECS"


@dataclass
class SpecStatus:
    exists: bool
    state: str | None
    sequence_number: str | None
    detail: str


def get_caller_token_spec_status(app_name: str) -> SpecStatus:
    """Look up the caller_token_spec app specification's approval state.

    Opens a caller-rights session and runs SHOW SPECIFICATIONS. Returns
    exists=False when no caller session is available, when the spec has not
    been requested yet (app not upgraded to the patch that creates it), or
    when the query itself fails.
    """
    conn = open_caller_session()
    if conn is None:
        return SpecStatus(
            False, None, None,
            "no caller session - service not running with executeAsCaller?",
        )
    try:
        cur = conn.cursor()
        cur.execute(f"SHOW SPECIFICATIONS IN APPLICATION {app_name}")
        rows = _rows(cur)
        row = None
        for r in rows:
            identifier = _find(r, "name", "setting") or ""
            if _SPEC_NAME in identifier.lower() or _SETTING_NAME in identifier.upper():
                row = r
                break
        if row is None:
            return SpecStatus(False, None, None, "no pending or approved request found")
        # SHOW SPECIFICATIONS names the approval-state column "status" (not "state");
        # accept either. Sequence lives in "sequence_number".
        state = _find(row, "status", "state")
        sequence = _find(row, "sequence")
        return SpecStatus(True, state, sequence, f"status = {state}")
    except Exception as e:  # insufficient privilege, app not found, etc.
        return SpecStatus(False, None, None, f"check failed: {e}")
    finally:
        conn.close()


def approve_caller_token_spec(app_name: str, sequence_number: int) -> tuple[bool, str]:
    """Approve the caller_token_spec app specification.

    Coerces sequence_number to int before interpolation (guards the DDL).
    Returns (True, "approved") on success, or (False, error message) if the
    operator lacks MANAGE APPLICATION SPECIFICATIONS or self-approval is
    blocked by the engine.
    """
    conn = open_caller_session()
    if conn is None:
        return False, "no caller session"
    try:
        n = int(sequence_number)
        cur = conn.cursor()
        cur.execute(
            f"ALTER APPLICATION {app_name} APPROVE SPECIFICATION {_SPEC_NAME} "
            f"SEQUENCE_NUMBER = {n}"
        )
        return True, "approved"
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()
