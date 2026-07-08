"""Pure logic for the Logs page's 502 handling: telling a genuine container
restart apart from a large `lines` request that simply timed out.

The controller wraps every exception from SYSTEM$GET_SERVICE_LOGS in a 502
(see Controller/app/main.py::get_logs), whether the service is actually
mid-restart or the request just outran SPCS ingress's short timeout window
(roughly 60-120s, see memory `feedback-async-spcs-endpoints`) while fetching
a lot of lines. This module picks the likelier explanation without an extra
controller round trip, reusing the service status the Logs page already has.
"""
from __future__ import annotations

# Reproduced live at 1300+ lines against a confirmed-RUNNING service (see
# PLAN-native-app-packaging.md item O10); warn well below that point.
LARGE_LINES_THRESHOLD = 500

# SYSTEM$GET_SERVICE_LOGS's numLines argument errors outright above this value
# ("Invalid `tail lines`... Allowed values are in range [1, 1000]"); it is a hard
# ceiling, not a performance suggestion like LARGE_LINES_THRESHOLD above.
LOG_LINES_HARD_CAP = 1000


def classify_log_fetch_failure(service_status: str | None, lines: int) -> tuple[str, str]:
    """Map a 502 log-fetch failure to a (severity, message) pair.

    `severity` names the Streamlit call to use (`"info"` or `"warning"`). A
    `service_status` other than "RUNNING" is direct evidence of a real
    restart, so that takes priority. Otherwise a large `lines` value is the
    likelier cause, since the log query has to run and return within the
    ingress timeout window. `service_status` is `None` when it isn't known
    (system logs, or the status lookup itself failed); only the `lines`
    heuristic is available then.
    """
    if service_status is not None and service_status != "RUNNING":
        return "info", f"Container is restarting (status: {service_status}); retrying..."
    if lines > LARGE_LINES_THRESHOLD:
        return "warning", (
            f"Log fetch failed, most likely because {lines} lines took longer than the "
            "request timeout window allows. The app itself keeps running - try a smaller "
            "number of lines."
        )
    return "info", "Log fetch failed transiently; retrying..."
