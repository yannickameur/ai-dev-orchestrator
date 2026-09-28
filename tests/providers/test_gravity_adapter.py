"""Tests for GravityAdapter (P19 — ROADMAP.md §13).

All tests are offline: no network call, no real `agy` invocation anywhere
in this file except the one real subprocess-timeout test, which spawns the
local `sleep` binary directly (not `agy`, no network) — the same pattern
already used by ``tests/providers/test_mistral_vibe_adapter.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.providers.contracts import UnavailabilityReason
from orchestrator.providers.gravity_adapter import (
    DEFAULT_PROBE_PROMPT,
    GravityAdapter,
    GravityProbeTimeout,
    _classify_failure_reason,
    _default_subprocess_runner,
)
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy

UTC_NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)


class TestProbeSuccess:
    def test_usable_probe_yields_available_provider_state(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b'{"status": "SUCCESS", "response": "OK\\n"}', b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.provider == "gravity"
        assert state.availability.available is True
        assert state.availability.reason is None
        assert state.observed_at == UTC_NOW

    def test_no_quota_window_is_ever_fabricated_on_success(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b"{}", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows == ()

    def test_probe_builds_expected_command_line(self) -> None:
        captured_args = {}

        async def fake_runner(args, timeout):
            captured_args["args"] = list(args)
            return 0, b"{}", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        asyncio.run(adapter.probe())

        assert captured_args["args"] == [
            "agy",
            "-p",
            DEFAULT_PROBE_PROMPT,
            "--mode=accept-edits",
            "--output-format",
            "json",
        ]
        # STANDARD only — a probe must never require a permission bypass.
        assert "--dangerously-skip-permissions" not in captured_args["args"]


class TestProbeFailureClassification:
    def test_no_quota_window_is_ever_fabricated_on_failure(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"some generic error"

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows == ()
        assert state.availability.available is False

    def test_unrecognized_failure_stays_unknown_never_guessed(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"something went wrong"

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.reason is UnavailabilityReason.UNKNOWN

    def test_auth_like_stderr_is_classified_as_auth_error(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"Error: not authenticated, run agy install"

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.AUTH_ERROR

    def test_rate_limit_like_stderr_is_classified_as_quota_exhausted(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"HTTP 429: rate limit exceeded, please retry later"

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_classify_failure_reason_is_a_pure_best_effort_function(self) -> None:
        # Directly exercises the documented-as-fragile classifier so its
        # exact keyword set stays an explicit, reviewable contract.
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
            return 0, b"{}", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"gravity": adapter}, QuotaPolicy(state_ttl=timedelta(minutes=5)), clock=lambda: UTC_NOW
        )

        asyncio.run(manager.get("gravity"))
        asyncio.run(manager.get("gravity"))

        assert call_count["n"] == 1  # cached, never one probe per call

    def test_refresh_forces_a_new_real_probe_even_if_cache_is_fresh(self) -> None:
        call_count = {"n": 0}

        async def fake_runner(args, timeout):
            call_count["n"] += 1
            return 0, b"{}", b""

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"gravity": adapter}, QuotaPolicy(state_ttl=timedelta(minutes=5)), clock=lambda: UTC_NOW
        )

        asyncio.run(manager.get("gravity"))
        asyncio.run(manager.refresh("gravity"))

        assert call_count["n"] == 2

    def test_expired_cache_with_failing_probe_never_returns_stale_available(self) -> None:
        # Same STALE != USABLE FOR ROUTING invariant already proven for
        # Claude/Codex/Mistral, now exercised for Gravity.
        clock_box = {"now": UTC_NOW}
        outcomes = iter([(0, b"{}", b""), None])

        async def fake_runner(args, timeout):
            outcome = next(outcomes)
            if outcome is None:
                raise GravityProbeTimeout("timed out")
            return outcome

        adapter = GravityAdapter(subprocess_runner=fake_runner, clock=lambda: clock_box["now"])
        manager = QuotaManager(
            {"gravity": adapter}, QuotaPolicy(state_ttl=timedelta(seconds=60)), clock=lambda: clock_box["now"]
        )

        first = asyncio.run(manager.get("gravity"))
        assert first.availability.available is True

        clock_box["now"] = UTC_NOW + timedelta(seconds=120)  # cache now expired

        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.get("gravity"))
