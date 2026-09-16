from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import egress_watch

# Captured before any fixture runs: conftest's autouse _noop_egress_loop
# monkeypatches egress_watch.run_loop to a harmless stub for every other test
# (so the background task started by main.py's lifespan can't race a route
# test's fake_sf state). TestRunLoop below exists specifically to test the
# real implementation, so it must call the function object captured here
# rather than the (by-then-patched) `egress_watch.run_loop` attribute.
_REAL_RUN_LOOP = egress_watch.run_loop


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class TestParseRanges:
    def test_keeps_well_formed_entries(self):
        raw = [
            {"ipv4_prefix": "1.2.3.0/24", "effective": "2026-01-01T00:00:00Z", "expires": "2026-09-07T00:00:00Z"},
        ]
        assert egress_watch.parse_ranges(raw) == raw

    def test_drops_non_dict_entries(self):
        raw = ["not-a-dict", {"expires": "2026-09-07T00:00:00Z"}]
        parsed = egress_watch.parse_ranges(raw)
        assert len(parsed) == 1
        assert parsed[0]["expires"] == "2026-09-07T00:00:00Z"

    def test_drops_entries_missing_expires(self):
        raw = [{"ipv4_prefix": "1.2.3.0/24", "effective": "2026-01-01T00:00:00Z"}]
        assert egress_watch.parse_ranges(raw) == []

    def test_drops_entries_with_unparseable_expires(self):
        raw = [{"expires": "not-a-date"}]
        assert egress_watch.parse_ranges(raw) == []

    def test_empty_input_returns_empty_list(self):
        assert egress_watch.parse_ranges([]) == []
        assert egress_watch.parse_ranges(None) == []


class TestParseTsNaive:
    def test_bare_date_without_offset_treated_as_utc(self):
        # No trailing 'Z' or explicit offset - _parse_ts must still tag it UTC
        # rather than leaving it an unaware datetime (which would break any
        # later subtraction against an aware `now`).
        ts = egress_watch._parse_ts("2026-09-07T00:00:00")
        assert ts.tzinfo is not None
        assert ts.utcoffset().total_seconds() == 0


class TestMinExpiry:
    def test_returns_earliest_expiry(self):
        ranges = [
            {"expires": "2026-09-10T00:00:00Z"},
            {"expires": "2026-09-07T00:00:00Z"},
            {"expires": "2026-09-15T00:00:00Z"},
        ]
        result = egress_watch.min_expiry(ranges)
        assert result == _dt("2026-09-07T00:00:00+00:00").isoformat()

    def test_empty_ranges_returns_none(self):
        assert egress_watch.min_expiry([]) is None

    def test_malformed_entry_among_ranges_is_skipped(self):
        ranges = [{"expires": "not-a-date"}, {"expires": "2026-09-07T00:00:00Z"}]
        assert egress_watch.min_expiry(ranges) == _dt("2026-09-07T00:00:00+00:00").isoformat()


class TestDaysRemaining:
    def test_computes_whole_days(self):
        now = _dt("2026-08-25T00:00:00+00:00")
        assert egress_watch.days_remaining("2026-09-07T00:00:00Z", now=now) == 13

    def test_returns_none_when_no_expiry_recorded(self):
        assert egress_watch.days_remaining(None) is None

    def test_returns_none_on_unparseable_expiry(self):
        assert egress_watch.days_remaining("garbage") is None

    def test_negative_when_already_expired(self):
        now = _dt("2026-09-10T00:00:00+00:00")
        assert egress_watch.days_remaining("2026-09-07T00:00:00Z", now=now) < 0


class TestIsAcknowledged:
    def test_acked_when_expiry_on_or_before_ack_date(self):
        assert egress_watch.is_acknowledged("2026-09-07T12:00:00Z", "2026-09-07") is True
        assert egress_watch.is_acknowledged("2026-09-07T12:00:00Z", "2026-09-08") is True

    def test_not_acked_when_expiry_after_ack_date(self):
        assert egress_watch.is_acknowledged("2026-09-07T12:00:00Z", "2026-09-06") is False

    def test_not_acked_when_no_ack(self):
        assert egress_watch.is_acknowledged("2026-09-07T12:00:00Z", None) is False

    def test_not_acked_when_no_expiry(self):
        assert egress_watch.is_acknowledged(None, "2026-09-07") is False

    def test_not_acked_on_unparseable_ack(self):
        assert egress_watch.is_acknowledged("2026-09-07T12:00:00Z", "not-a-date") is False


class TestRunIteration:
    def test_persists_min_expiry_and_ranges(self, fake_sf):
        fake_sf.egress_ranges = [
            {"ipv4_prefix": "1.2.3.0/24", "effective": "2026-01-01T00:00:00Z", "expires": "2026-09-07T00:00:00Z"},
        ]
        egress_watch.run_iteration()
        assert fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] == _dt("2026-09-07T00:00:00+00:00").isoformat()
        stored_ranges = json.loads(fake_sf.config[egress_watch.CONFIG_RANGES])
        assert stored_ranges == fake_sf.egress_ranges

    def test_malformed_entries_skipped_not_fatal(self, fake_sf):
        fake_sf.egress_ranges = ["garbage", {"expires": "2026-09-07T00:00:00Z"}]
        egress_watch.run_iteration()
        assert fake_sf.config[egress_watch.CONFIG_MIN_EXPIRY] == _dt("2026-09-07T00:00:00+00:00").isoformat()

    def test_fetch_failure_leaves_config_untouched(self, fake_sf):
        fake_sf.raise_on["get_egress_ip_ranges"] = RuntimeError("boom")
        egress_watch.run_iteration()
        assert egress_watch.CONFIG_MIN_EXPIRY not in fake_sf.config

    def test_no_email_when_unconfigured(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert fake_sf.sent_emails == []

    def test_email_sent_when_configured_and_within_threshold(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert len(fake_sf.sent_emails) == 1
        integration, recipients, subject, body = fake_sf.sent_emails[0]
        assert integration == "MY_INT"
        assert recipients == "a@example.com"
        assert "13 day" in subject
        assert fake_sf.config[egress_watch.CONFIG_LAST_ALERT_SENT] == "2026-08-25"

    def test_no_email_outside_threshold(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-12-01T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert fake_sf.sent_emails == []

    def test_no_email_when_already_acknowledged(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        fake_sf.config[egress_watch.CONFIG_ACK_THROUGH] = "2026-09-07"
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert fake_sf.sent_emails == []

    def test_no_duplicate_email_same_day(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        now = _dt("2026-08-25T00:00:00+00:00")
        egress_watch.run_iteration(now=now)
        egress_watch.run_iteration(now=now)
        assert len(fake_sf.sent_emails) == 1

    def test_no_email_when_recipients_json_malformed(self, fake_sf):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = "not-json"
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert fake_sf.sent_emails == []

    def test_no_email_when_min_expiry_never_recorded(self, fake_sf):
        # Configured (integration + recipients) but the fetch itself found no
        # ranges at all - nothing to warn about yet.
        fake_sf.egress_ranges = []
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))
        assert fake_sf.sent_emails == []

    def test_persist_failure_does_not_block_email_step(self, fake_sf, monkeypatch):
        fake_sf.egress_ranges = [{"expires": "2026-09-07T00:00:00Z"}]
        fake_sf.config[egress_watch.CONFIG_ALERT_INTEGRATION] = "MY_INT"
        fake_sf.config[egress_watch.CONFIG_ALERT_RECIPIENTS] = json.dumps(["a@example.com"])
        fake_sf.raise_on["set_config"] = RuntimeError("persist boom")
        # Must not raise, and must not prevent the (independent) alert attempt
        # from at least being reached - the email step reads via get_config,
        # which is unaffected by set_config raising.
        egress_watch.run_iteration(now=_dt("2026-08-25T00:00:00+00:00"))


class TestRunLoop:
    def test_survives_iteration_exception_and_reaches_sleep(self, monkeypatch):
        calls: list[str] = []

        def bad_iteration(**kwargs):
            calls.append("iteration")
            raise RuntimeError("boom")

        async def fake_sleep(secs):
            calls.append("slept")
            raise asyncio.CancelledError()  # stop the infinite loop after one tick

        monkeypatch.setattr(egress_watch, "run_iteration", bad_iteration)
        monkeypatch.setattr(egress_watch.asyncio, "sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_REAL_RUN_LOOP())

        assert calls == ["iteration", "slept"]

    def test_runs_iteration_before_first_sleep(self, monkeypatch):
        calls: list[str] = []

        def ok_iteration(**kwargs):
            calls.append("iteration")

        async def fake_sleep(secs):
            calls.append("slept")
            raise asyncio.CancelledError()

        monkeypatch.setattr(egress_watch, "run_iteration", ok_iteration)
        monkeypatch.setattr(egress_watch.asyncio, "sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_REAL_RUN_LOOP())

        assert calls == ["iteration", "slept"]
