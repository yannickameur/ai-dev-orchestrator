"""Tests for MistralVibeAdapter (post-MVP 0.1 — see docs/VIBE_SPIKE.md).

All tests are offline: no network call, no real Vibe invocation anywhere in
this file except the one real subprocess-timeout test, which spawns the
local `sleep` binary directly (not Vibe, no network) — the same pattern
already used by ``tests/providers/test_claude_code_adapter.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.providers.contracts import UnavailabilityReason
from orchestrator.providers.mistral_vibe_adapter import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PROBE_PROMPT,
    MistralVibeAdapter,
    VibeProbeTimeout,
    _classify_failure_reason,
    _default_subprocess_runner,
)
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy

UTC_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


class TestProbeSuccess:
    def test_usable_probe_yields_available_provider_state(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b'[{"type": "message", "content": "OK"}]', b""

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.provider == "mistral"
        assert state.availability.available is True
        assert state.availability.reason is None
        assert state.observed_at == UTC_NOW

    def test_no_reset_at_is_ever_fabricated_on_success(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b"[]", b""

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows == ()

    def test_probe_builds_expected_command_line(self) -> None:
        captured_args = {}

        async def fake_runner(args, timeout):
            captured_args["args"] = list(args)
            return 0, b"[]", b""

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        asyncio.run(adapter.probe())

        assert captured_args["args"] == [
            "vibe",
            "-p",
            DEFAULT_PROBE_PROMPT,
            "--output",
            "json",
            "--trust",
            "--auto-approve",
            "--max-turns",
            DEFAULT_MAX_TURNS,
        ]


class TestProbeFailureClassification:
    def test_no_reset_at_is_ever_fabricated_on_failure(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"some generic error"

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.quota_windows == ()
        assert state.availability.available is False

    def test_unrecognized_failure_stays_unknown_never_guessed(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"something went wrong"

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.reason is UnavailabilityReason.UNKNOWN

    def test_auth_like_stderr_is_classified_as_auth_error(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"Error: not authenticated, run vibe --setup"

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.AUTH_ERROR

    def test_rate_limit_like_stderr_is_classified_as_quota_exhausted(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"HTTP 429: rate limit exceeded, please retry later"

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

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
        with pytest.raises(VibeProbeTimeout):
            asyncio.run(_default_subprocess_runner(["sleep", "5"], timeout=0.2))

    def test_fast_process_completes_within_timeout(self) -> None:
        exit_code, stdout, stderr = asyncio.run(_default_subprocess_runner(["true"], timeout=5.0))
        assert exit_code == 0

    def test_adapter_propagates_timeout_as_probe_error_via_quota_manager(self) -> None:
        async def timeout_runner(args, timeout):
            raise VibeProbeTimeout("vibe probe timed out after 60.0s")

        adapter = MistralVibeAdapter(subprocess_runner=timeout_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"mistral": adapter}, QuotaPolicy(state_ttl=timedelta(seconds=60)), clock=lambda: UTC_NOW
        )

        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.get("mistral"))


class TestQuotaManagerCachingAndRefreshHonesty:
    def test_second_get_within_ttl_does_not_reprobe(self) -> None:
        call_count = {"n": 0}

        async def fake_runner(args, timeout):
            call_count["n"] += 1
            return 0, b"[]", b""

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"mistral": adapter}, QuotaPolicy(state_ttl=timedelta(minutes=5)), clock=lambda: UTC_NOW
        )

        asyncio.run(manager.get("mistral"))
        asyncio.run(manager.get("mistral"))

        assert call_count["n"] == 1  # cached, never one probe per call

    def test_refresh_forces_a_new_real_probe_even_if_cache_is_fresh(self) -> None:
        call_count = {"n": 0}

        async def fake_runner(args, timeout):
            call_count["n"] += 1
            return 0, b"[]", b""

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)
        manager = QuotaManager(
            {"mistral": adapter}, QuotaPolicy(state_ttl=timedelta(minutes=5)), clock=lambda: UTC_NOW
        )

        asyncio.run(manager.get("mistral"))
        asyncio.run(manager.refresh("mistral"))

        assert call_count["n"] == 2

    def test_expired_cache_with_failing_probe_never_returns_stale_available(self) -> None:
        # Same STALE != USABLE FOR ROUTING invariant already proven for
        # Claude/Codex, now exercised for Mistral.
        clock_box = {"now": UTC_NOW}
        outcomes = iter([(0, b"[]", b""), None])

        async def fake_runner(args, timeout):
            outcome = next(outcomes)
            if outcome is None:
                raise VibeProbeTimeout("timed out")
            return outcome

        adapter = MistralVibeAdapter(subprocess_runner=fake_runner, clock=lambda: clock_box["now"])
        manager = QuotaManager(
            {"mistral": adapter}, QuotaPolicy(state_ttl=timedelta(seconds=60)), clock=lambda: clock_box["now"]
        )

        first = asyncio.run(manager.get("mistral"))
        assert first.availability.available is True

        clock_box["now"] = UTC_NOW + timedelta(seconds=120)  # cache now expired

        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.get("mistral"))
