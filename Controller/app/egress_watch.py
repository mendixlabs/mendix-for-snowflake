"""Daily watch over the account's SPCS egress IP-range whitelist.

SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES() returns each CIDR's effective/expires
timestamps; Snowflake never pushes a rotation notice, so without this nothing
in the app would ever notice the whitelist going stale until deployed Mendix
apps' Postgres egress (the consumer's network policy, keyed off the same
CIDRs - see the Setup page's step 1) starts failing outright.

run_loop is started once from main.py's lifespan as a single background
asyncio task (the controller is a single-worker process, so there is no
multi-worker coordination to do). Each iteration is independent and cheap
(one metadata query plus a couple of tiny key/value reads/writes), so running
it once at startup and then every 24h is enough; run_iteration is also safe to
call more than once a day (e.g. across a controller restart) since the actual
email send is separately deduplicated per calendar day.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

from . import snowflake_client as sf

logger = logging.getLogger(__name__)

INTERVAL_SECS = 24 * 60 * 60

# Email fires once the whitelist expiry is within this many days (and stays
# silent once acknowledged past that expiry - see is_acknowledged).
ALERT_THRESHOLD_DAYS = 30
# The Apps-page banner fires on a tighter threshold than the email: the email
# is an early heads-up, the banner is "handle this now".
WARNING_THRESHOLD_DAYS = 14

# internal_config keys (see snowflake_client.get_config/set_config).
CONFIG_MIN_EXPIRY = "egress_min_expiry"
CONFIG_RANGES = "egress_ranges_json"
CONFIG_ALERT_INTEGRATION = "egress_alert_integration"
CONFIG_ALERT_RECIPIENTS = "egress_alert_recipients"
CONFIG_ACK_THROUGH = "egress_ack_through"
# Not part of the plan's original three keys, but needed to actually honor
# "once per day": the loop's own 24h cadence is not a guarantee (a controller
# restart re-runs the first iteration immediately), so the last-sent date is
# persisted rather than kept in memory.
CONFIG_LAST_ALERT_SENT = "egress_alert_last_sent"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 timestamp (Snowflake's egress-range JSON, or our own
    stored min-expiry), tolerating a trailing 'Z' and a bare date string.
    Raises ValueError/TypeError on anything else - callers decide whether to
    skip or propagate."""
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_ranges(raw: list) -> list[dict]:
    """Defensively normalize SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES() rows to
    {ipv4_prefix, effective, expires} dicts, dropping (not raising on) any
    entry that isn't a dict or whose `expires` is missing/unparseable - such
    an entry can't contribute to the min-expiry computation anyway, and one
    malformed row must never take down the whole iteration."""
    parsed: list[dict] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        expires = entry.get("expires")
        if not expires:
            continue
        try:
            _parse_ts(expires)
        except (ValueError, TypeError):
            continue
        parsed.append({
            "ipv4_prefix": entry.get("ipv4_prefix"),
            "effective": entry.get("effective"),
            "expires": expires,
        })
    return parsed


def min_expiry(ranges: list[dict]) -> str | None:
    """ISO timestamp of the earliest `expires` among already-parsed ranges, or
    None when there are none (an empty or entirely-malformed fetch)."""
    timestamps = []
    for r in ranges:
        try:
            timestamps.append(_parse_ts(r["expires"]))
        except (ValueError, TypeError, KeyError):
            continue
    return min(timestamps).isoformat() if timestamps else None


def days_remaining(min_expiry_iso: str | None, *, now: datetime | None = None) -> int | None:
    """Whole days between `now` and `min_expiry_iso`, or None when there's no
    recorded expiry yet (the loop hasn't run, or every fetch so far failed).
    Negative once the expiry has already passed."""
    if not min_expiry_iso:
        return None
    try:
        expiry = _parse_ts(min_expiry_iso)
    except (ValueError, TypeError):
        return None
    return (expiry - (now or _now())).days


def is_acknowledged(min_expiry_iso: str | None, ack_through_iso: str | None) -> bool:
    """True when the current min_expiry is already covered by a prior
    acknowledgement (POST /system/egress-ack's through_date): compared as
    calendar dates, so acknowledging "through 2026-09-10" covers any expiry
    timestamp that same day regardless of time-of-day. False whenever either
    value is missing or unparseable - the whole point of an explicit
    acknowledgement is that silence never counts as one."""
    if not min_expiry_iso or not ack_through_iso:
        return False
    try:
        expiry_date = _parse_ts(min_expiry_iso).date()
        ack_date = date.fromisoformat(ack_through_iso)
    except (ValueError, TypeError):
        return False
    return expiry_date <= ack_date


def _maybe_send_alert(min_expiry_iso: str | None, *, now: datetime | None = None) -> None:
    """Send the once-daily expiry-warning email, but only when: an
    integration + recipients are both configured, the expiry is within
    ALERT_THRESHOLD_DAYS, it isn't already acknowledged, and today hasn't
    already sent one. Unconfigured (no integration or no recipients) is not
    an error - it's the default, silent state."""
    integration = sf.get_config(CONFIG_ALERT_INTEGRATION)
    recipients_raw = sf.get_config(CONFIG_ALERT_RECIPIENTS)
    if not integration or not recipients_raw:
        return
    try:
        recipients = json.loads(recipients_raw)
    except (json.JSONDecodeError, TypeError):
        recipients = []
    if not recipients:
        return
    if not min_expiry_iso:
        return
    remaining = days_remaining(min_expiry_iso, now=now)
    if remaining is None or remaining > ALERT_THRESHOLD_DAYS:
        return
    ack_through = sf.get_config(CONFIG_ACK_THROUGH)
    if is_acknowledged(min_expiry_iso, ack_through):
        return
    today = (now or _now()).date()
    if sf.get_config(CONFIG_LAST_ALERT_SENT) == today.isoformat():
        return

    subject = f"Snowflake egress IP whitelist expires in {remaining} day(s)"
    body = (
        f"The SPCS egress IP range(s) this app's Postgres connectivity relies on "
        f"expire {min_expiry_iso} ({remaining} day(s) remaining). Refresh the "
        "consumer's Postgres network policy with the current "
        "SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES() output - see the Infrastructure "
        "page's 'Egress IP expiry' section for the regenerated fix-up SQL."
    )
    sf.send_email(integration, ",".join(recipients), subject, body)
    sf.set_config(CONFIG_LAST_ALERT_SENT, today.isoformat())


def run_iteration(*, now: datetime | None = None) -> None:
    """One watch cycle: fetch the current egress ranges, persist the min
    expiry + full range list, then (independently) maybe send the alert
    email. Each step is isolated in its own try/except so a failure in one
    never blocks the others, and the whole function never raises - see
    run_loop, which wraps this call too as a second line of defense."""
    try:
        raw_ranges = sf.get_egress_ip_ranges()
    except Exception:
        logger.exception("egress_watch: failed to fetch egress IP ranges")
        return

    parsed = parse_ranges(raw_ranges)
    expiry = min_expiry(parsed)

    try:
        sf.set_config(CONFIG_MIN_EXPIRY, expiry)
        sf.set_config(CONFIG_RANGES, json.dumps(parsed))
    except Exception:
        logger.exception("egress_watch: failed to persist egress config")

    try:
        _maybe_send_alert(expiry, now=now)
    except Exception:
        logger.exception("egress_watch: failed to send egress alert email")


async def run_loop() -> None:
    """Run run_iteration immediately, then every INTERVAL_SECS, forever.
    Started as a single asyncio task from main.py's lifespan and cancelled on
    shutdown. The try/except here is a second line of defense on top of
    run_iteration's own internal isolation, so a bug that somehow still
    escapes it can never kill the loop."""
    while True:
        try:
            run_iteration()
        except Exception:
            logger.exception("egress_watch: iteration raised unexpectedly")
        await asyncio.sleep(INTERVAL_SECS)
