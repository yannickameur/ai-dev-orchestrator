"""Tests for GravityAdapter (P19 origin, P20 real `/usage` telemetry).

All tests are offline: no network call, no real `agy` invocation anywhere
in this file except the one real subprocess-timeout test, which spawns the
local `sleep` binary directly (not `agy`, no network) — the same pattern
already used by ``tests/providers/test_mistral_vibe_adapter.py``.

The real `/usage` JSON shape below is copied verbatim from a real,
authenticated `agy -p "/usage" --output-format json` invocation (agy
1.2.12, ROADMAP.md P20) — not invented.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.providers.contracts import UnavailabilityReason
from orchestrator.providers.gravity_adapter import (
    GravityAdapter,
    GravityProbeMalformedResponseError,
    GravityProbeTimeout,
    _classify_failure_reason,
    _default_subprocess_runner,
)
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy

UTC_NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)

REAL_USAGE_RESPONSE = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": (
        "Gemini Models\tWeekly Limit Remaining\t93%\t2026-10-05T14:55:49Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t82%\t2026-10-05T15:13:14Z\n"
    ),
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit.",
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "description": "6 days, 22 hours until refresh.",
                            "window": "weekly",
                            "remaining_fraction": 0.9273071885108948,
                            "reset_time": "2026-10-05T14:55:49Z",
                        }
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "description": "6 days, 23 hours until refresh.",
                            "window": "weekly",
                            "remaining_fraction": 0.8193479776382446,
                            "reset_time": "2026-10-05T15:13:14Z",
                        }
                    ],
                },
            ],
        },
    },
}


def _runner_returning(payload: dict, *, exit_code: int = 0, stderr: bytes = b""):
    async def fake_runner(args, timeout):
        return exit_code, json.dumps(payload).encode("utf-8"), stderr

    return fake_runner


class TestUsageProbeSuccess:
    def test_real_usage_response_normalizes_to_two_quota_windows(self) -> None:
        adapter = GravityAdapter(subprocess_runner=_runner_returning(REAL_USAGE_RESPONSE), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.provider == "gravity"
        assert state.availability.available is True
        assert state.availability.reason is None
        assert len(state.quota_windows) == 2

    def test_remaining_fraction_maps_to_one_minus_utilization(self) -> None:
        adapter = GravityAdapter(subprocess_runner=_runner_returning(REAL_USAGE_RESPONSE), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        by_id = {w.window_type: w for w in state.quota_windows}
        assert by_id["gemini-weekly"].utilization == pytest.approx(1.0 - 0.9273071885108948)
        assert by_id["3p-weekly"].utilization == pytest.approx(1.0 - 0.8193479776382446)

    def test_reset_time_is_parsed_as_aware_datetime(self) -> None:
        adapter = GravityAdapter(subprocess_runner=_runner_returning(REAL_USAGE_RESPONSE), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        by_id = {w.window_type: w for w in state.quota_windows}
        assert by_id["3p-weekly"].reset_at == datetime(2026, 10, 5, 15, 13, 14, tzinfo=timezone.utc)

    def test_window_type_uses_bucket_id_never_the_colliding_bare_window_field(self) -> None:
        adapter = GravityAdapter(subprocess_runner=_runner_returning(REAL_USAGE_RESPONSE), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        window_types = {w.window_type for w in state.quota_windows}
        assert window_types == {"gemini-weekly", "3p-weekly"}
        assert "weekly" not in window_types  # the bare, colliding field is never used directly

    def test_probe_builds_expected_command_line_no_conversational_prompt(self) -> None:
        captured_args = {}

        async def fake_runner(args, timeout):
            captured_args["args"] = list(args)
            return 0, json.dumps(REAL_USAGE_RESPONSE).encode("utf-8"), b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        asyncio.run(adapter.probe())

        assert captured_args["args"] == ["agy", "-p", "/usage", "--output-format", "json"]
        # Read-only slash command: no edit-permission flag needed at all.
        assert "--mode=accept-edits" not in captured_args["args"]
        assert "--dangerously-skip-permissions" not in captured_args["args"]

    def test_probe_consumes_no_model_turn(self) -> None:
        # The real response's own num_turns/usage.*_tokens are all 0 for a
        # recognized /usage slash command (verified, P20) — this adapter
        # must never need to inspect them (it never fabricates a "cost"),
        # but the fixture itself documents the real, verified fact.
        assert REAL_USAGE_RESPONSE["num_turns"] == 0
        assert REAL_USAGE_RESPONSE["usage"]["total_tokens"] == 0


class TestUsageProbeToleratesMissingFields:
    def test_missing_groups_yields_no_quota_windows_never_fabricated(self) -> None:
        payload = {"status": "SUCCESS", "command": {"name": "usage", "data": {}}}
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is True
        assert state.quota_windows == ()

    def test_missing_command_key_entirely_still_reports_available_no_windows(self) -> None:
        payload = {"status": "SUCCESS"}
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is True
        assert state.quota_windows == ()

    def test_missing_remaining_fraction_yields_unknown_utilization_never_zero(self) -> None:
        payload = {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [
                {"name": "G", "buckets": [{"id": "g-weekly", "window": "weekly", "reset_time": "2026-10-05T14:55:49Z"}]}
            ]}},
        }
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert len(state.quota_windows) == 1
        assert state.quota_windows[0].utilization is None  # never fabricated as 0.0

    def test_missing_reset_time_yields_none_never_fabricated(self) -> None:
        payload = {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [
                {"name": "G", "buckets": [{"id": "g-weekly", "window": "weekly", "remaining_fraction": 0.5}]}
            ]}},
        }
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows[0].reset_at is None

    def test_bucket_with_no_id_or_window_is_skipped_never_a_fabricated_label(self) -> None:
        payload = {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [
                {"name": "G", "buckets": [{"remaining_fraction": 0.5}]}
            ]}},
        }
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows == ()

    def test_out_of_range_remaining_fraction_never_fabricates_utilization(self) -> None:
        payload = {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [
                {"name": "G", "buckets": [{"id": "g-weekly", "remaining_fraction": 1.5}]}
            ]}},
        }
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows[0].utilization is None


class TestUsageProbeExhaustedWindow:
    @staticmethod
    def _usage_with_3p_remaining(remaining: float) -> dict:
        payload = json.loads(json.dumps(REAL_USAGE_RESPONSE))
        payload["command"]["data"]["groups"][1]["buckets"][0]["remaining_fraction"] = remaining
        return payload

    def test_exhausted_bucket_makes_provider_unavailable_with_windows_kept(self) -> None:
        adapter = GravityAdapter(
            subprocess_runner=_runner_returning(self._usage_with_3p_remaining(0)), clock=lambda: UTC_NOW,
        )

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED
        windows = {w.window_type: w for w in state.quota_windows}
        assert windows["3p-weekly"].utilization == 1.0
        assert windows["3p-weekly"].reset_at == datetime(2026, 10, 5, 15, 13, 14, tzinfo=timezone.utc)

    def test_nearly_exhausted_bucket_stays_available(self) -> None:
        adapter = GravityAdapter(
            subprocess_runner=_runner_returning(self._usage_with_3p_remaining(0.01)), clock=lambda: UTC_NOW,
        )

        assert asyncio.run(adapter.probe()).availability.available is True


class TestUsageProbeFailure:
    def test_nonzero_exit_is_unavailable(self) -> None:
        adapter = GravityAdapter(subprocess_runner=_runner_returning({}, exit_code=1), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.quota_windows == ()

    def test_status_error_is_unavailable_no_windows(self) -> None:
        payload = {"status": "ERROR", "error": "something went wrong"}
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.quota_windows == ()

    def test_auth_like_error_is_classified_as_auth_error(self) -> None:
        payload = {"status": "ERROR", "error": "not authenticated, run agy install"}
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.reason is UnavailabilityReason.AUTH_ERROR

    def test_rate_limit_like_error_is_classified_as_quota_exhausted(self) -> None:
        payload = {"status": "ERROR", "error": "HTTP 429: rate limit exceeded"}
        adapter = GravityAdapter(subprocess_runner=_runner_returning(payload), clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_malformed_json_raises_probe_error_not_silently_unavailable(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b"not json at all {{{", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        with pytest.raises(GravityProbeMalformedResponseError):
            asyncio.run(adapter.probe())

    def test_valid_json_non_object_raises_probe_error(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b"[1, 2, 3]", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        with pytest.raises(GravityProbeMalformedResponseError):
            asyncio.run(adapter.probe())

    def test_classify_failure_reason_is_a_pure_best_effort_function(self) -> None:
        assert _classify_failure_reason("", "generic failure") is UnavailabilityReason.UNKNOWN
        assert _classify_failure_reason("too many requests", "") is UnavailabilityReason.QUOTA_EXHAUSTED
        assert _classify_failure_reason("401 Unauthorized", "") is UnavailabilityReason.AUTH_ERROR


class TestProbeTimeout:
    def test_timeout_kills_process_and_raises(self) -> None:
        with pytest.raises(GravityProbeTimeout):
            asyncio.run(_default_subprocess_runner(["sleep", "5"], timeout=0.2))

    def test_fast_process_completes_within_timeout(self) -> None:
        exit_code, stdout, stderr = asyncio.run(_default_subprocess_runner(["true"], timeout=5.0))
        assert exit_code == 0

    def test_adapter_propagates_timeout_as_probe_error_via_quota_manager(self) -> None:
        async def timeout_runner(args, timeout):
            raise GravityProbeTimeout("gravity probe timed out after 60.0s")

        adapter = GravityAdapter(subprocess_runner=timeout_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"gravity": adapter}, QuotaPolicy(state_ttl=timedelta(seconds=60)), clock=lambda: UTC_NOW
        )

        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.get("gravity"))


class TestQuotaManagerCachingAndRefreshHonesty:
    def test_second_get_within_ttl_does_not_reprobe(self) -> None:
        call_count = {"n": 0}

        async def fake_runner(args, timeout):
            call_count["n"] += 1
            return 0, json.dumps(REAL_USAGE_RESPONSE).encode("utf-8"), b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"gravity": adapter}, QuotaPolicy(state_ttl=timedelta(minutes=5)), clock=lambda: UTC_NOW
        )

        asyncio.run(manager.get("gravity"))
        asyncio.run(manager.get("gravity"))

        assert call_count["n"] == 1  # cached, never one probe per call — Arthur and Nora share this
