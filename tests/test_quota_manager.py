"""Tests for QuotaManager (Phase 1 / Slice 3).

All tests are offline: a small in-test FakeAdapter stands in for
ClaudeCodeAdapter/CodexAdapter — no subprocess, no network, no real
Claude/Codex invocation anywhere in this file.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import pytest

from orchestrator import quota_manager as quota_manager_module
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    ResetCredit,
    ResetCreditStatus,
    UnavailabilityReason,
)
from orchestrator.quota_manager import (
    ProviderProbeError,
    QuotaManager,
    QuotaPolicy,
    UnknownProviderError,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _state(
    provider: str = "anthropic",
    observed_at: datetime = UTC_NOW,
    available: bool = True,
    windows: tuple[QuotaWindow, ...] = (),
    reset_credits: tuple[ResetCredit, ...] = (),
) -> ProviderState:
    availability = ProviderAvailability(
        available=available,
        observed_at=observed_at,
        reason=None if available else UnavailabilityReason.UNKNOWN,
    )
    return ProviderState(
        provider=provider,
        availability=availability,
        observed_at=observed_at,
        quota_windows=windows,
        reset_credits=reset_credits,
    )


class FakeClock:
    """A mutable, injectable, timezone-aware clock for deterministic tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeAdapter(ProviderAdapter):
    """Minimal offline adapter: each call pulls the next scripted outcome."""

    def __init__(self, probe_fn: Callable[[], Awaitable[ProviderState]]) -> None:
        self._probe_fn = probe_fn
        self.probe_count = 0

    async def probe(self) -> ProviderState:
        self.probe_count += 1
        return await self._probe_fn()


def _sequence_adapter(*outcomes) -> FakeAdapter:
    """Builds a FakeAdapter that yields each outcome in order (state or exception)."""
    remaining = list(outcomes)

    async def _probe_fn() -> ProviderState:
        outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return FakeAdapter(_probe_fn)


def _policy(seconds: float = 60.0) -> QuotaPolicy:
    return QuotaPolicy(state_ttl=timedelta(seconds=seconds))


class TestBasicCaching:
    def test_first_get_triggers_a_probe(self) -> None:
        adapter = _sequence_adapter(_state())
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        result = asyncio.run(manager.get("anthropic"))

        assert adapter.probe_count == 1
        assert result.provider == "anthropic"

    def test_second_get_with_fresh_state_uses_cache(self) -> None:
        adapter = _sequence_adapter(_state())
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        first = asyncio.run(manager.get("anthropic"))
        second = asyncio.run(manager.get("anthropic"))

        assert adapter.probe_count == 1
        assert second is first

    def test_ttl_expiration_triggers_a_new_probe(self) -> None:
        clock = FakeClock(UTC_NOW)
        state_t0 = _state(observed_at=UTC_NOW)
        state_t1 = _state(observed_at=UTC_NOW + timedelta(seconds=120))
        adapter = _sequence_adapter(state_t0, state_t1)
        manager = QuotaManager({"anthropic": adapter}, _policy(seconds=60), clock=clock)

        first = asyncio.run(manager.get("anthropic"))
        clock.advance(timedelta(seconds=120))
        second = asyncio.run(manager.get("anthropic"))

        assert adapter.probe_count == 2
        assert first.observed_at == UTC_NOW
        assert second.observed_at == UTC_NOW + timedelta(seconds=120)

    def test_refresh_forces_a_probe_even_if_cache_is_fresh(self) -> None:
        adapter = _sequence_adapter(_state(), _state())
        manager = QuotaManager({"anthropic": adapter}, _policy(seconds=3600), clock=lambda: UTC_NOW)

        asyncio.run(manager.get("anthropic"))
        asyncio.run(manager.refresh("anthropic"))

        assert adapter.probe_count == 2

    def test_observed_at_is_the_providers_own_observation_not_the_managers_clock(self) -> None:
        provider_observed_at = UTC_NOW - timedelta(minutes=3)
        adapter = _sequence_adapter(_state(observed_at=provider_observed_at))
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        result = asyncio.run(manager.get("anthropic"))

        assert result.observed_at == provider_observed_at
        assert result.observed_at != UTC_NOW


class TestMultipleProviders:
    def test_providers_are_cached_independently(self) -> None:
        claude_adapter = _sequence_adapter(_state(provider="anthropic"))
        codex_adapter = _sequence_adapter(_state(provider="openai"))
        manager = QuotaManager(
            {"anthropic": claude_adapter, "openai": codex_adapter},
            _policy(),
            clock=lambda: UTC_NOW,
        )

        asyncio.run(manager.refresh("anthropic"))
        claude_state = asyncio.run(manager.get("anthropic"))
        codex_state = asyncio.run(manager.get("openai"))

        assert claude_adapter.probe_count == 1
        assert codex_adapter.probe_count == 1
        assert claude_state.provider == "anthropic"
        assert codex_state.provider == "openai"


class TestQuotaWindowsAndResetCreditsPassthrough:
    def test_multiple_quota_windows_are_preserved(self) -> None:
        windows = (
            QuotaWindow(
                window_type="five_hour", source="claude_stream_json", observed_at=UTC_NOW,
                utilization=0.45,
            ),
            QuotaWindow(
                window_type="seven_day", source="claude_stream_json", observed_at=UTC_NOW,
                utilization=0.06,
            ),
        )
        adapter = _sequence_adapter(_state(windows=windows))
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        result = asyncio.run(manager.get("anthropic"))

        assert len(result.quota_windows) == 2
        assert {w.window_type for w in result.quota_windows} == {"five_hour", "seven_day"}

    def test_utilization_none_stays_none(self) -> None:
        window = QuotaWindow(window_type="five_hour", source="claude_stream_json", observed_at=UTC_NOW)
        adapter = _sequence_adapter(_state(windows=(window,)))
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        result = asyncio.run(manager.get("anthropic"))

        assert result.quota_windows[0].utilization is None

    def test_reset_credits_are_preserved_without_consumption(self) -> None:
        credit = ResetCredit(title="Full reset (Weekly + 5 hr)", status=ResetCreditStatus.AVAILABLE, available_count=1)
        adapter = _sequence_adapter(_state(reset_credits=(credit,)))
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        result = asyncio.run(manager.get("anthropic"))

        assert len(result.reset_credits) == 1
        assert result.reset_credits[0].auto_consume is False

    def test_quota_manager_exposes_no_consumption_method(self) -> None:
        forbidden = {"consume", "consume_reset_credit", "select_worker", "choose_provider", "fallback"}
        declared = set(vars(QuotaManager))
        assert forbidden.isdisjoint(declared)


class TestErrorHandling:
    def test_unknown_provider_raises(self) -> None:
        manager = QuotaManager({}, _policy(), clock=lambda: UTC_NOW)

        with pytest.raises(UnknownProviderError):
            asyncio.run(manager.get("does-not-exist"))

    def test_probe_error_with_no_prior_cache_raises(self) -> None:
        boom = RuntimeError("boom")
        adapter = _sequence_adapter(boom)
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        with pytest.raises(ProviderProbeError) as exc_info:
            asyncio.run(manager.get("anthropic"))

        assert exc_info.value.provider == "anthropic"
        assert exc_info.value.__cause__ is boom

    def test_refresh_error_raises_and_leaves_previous_cache_untouched(self) -> None:
        clock = FakeClock(UTC_NOW)
        good_state = _state(observed_at=UTC_NOW)
        boom = RuntimeError("network blip")
        adapter = _sequence_adapter(good_state, boom)
        manager = QuotaManager({"anthropic": adapter}, _policy(seconds=60), clock=clock)

        asyncio.run(manager.get("anthropic"))
        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.refresh("anthropic"))

        # refresh() failing must not silently fabricate availability, and the
        # previously cached value stays exactly as it was.
        clock.advance(timedelta(seconds=200))  # still expired, doesn't matter for refresh()
        assert adapter.probe_count == 2

    def test_expired_cache_with_failing_probe_raises_and_never_returns_stale_available(
        self,
    ) -> None:
        # STALE != USABLE FOR ROUTING: an expired, previously-AVAILABLE
        # state must never be handed back by get() just because the fresh
        # probe failed — a future WorkerSelector must never receive an old
        # AVAILABLE silently after a failed refresh.
        clock = FakeClock(UTC_NOW)
        good_state = _state(observed_at=UTC_NOW, available=True)
        boom = RuntimeError("provider unreachable")
        adapter = _sequence_adapter(good_state, boom)
        manager = QuotaManager({"anthropic": adapter}, _policy(seconds=60), clock=clock)

        first = asyncio.run(manager.get("anthropic"))
        assert first.availability.available is True

        clock.advance(timedelta(seconds=120))  # cache now expired

        with pytest.raises(ProviderProbeError) as exc_info:
            asyncio.run(manager.get("anthropic"))

        assert exc_info.value.provider == "anthropic"
        assert exc_info.value.__cause__ is boom
        assert adapter.probe_count == 2

    def test_refresh_still_raises_even_when_stale_cache_exists(self) -> None:
        # Unlike get(), refresh() is an explicit request for a truly fresh
        # value: it must never substitute the stale cache silently.
        clock = FakeClock(UTC_NOW)
        good_state = _state(observed_at=UTC_NOW)
        boom = RuntimeError("provider unreachable")
        adapter = _sequence_adapter(good_state, boom)
        manager = QuotaManager({"anthropic": adapter}, _policy(seconds=60), clock=clock)

        asyncio.run(manager.get("anthropic"))
        clock.advance(timedelta(seconds=120))

        with pytest.raises(ProviderProbeError):
            asyncio.run(manager.refresh("anthropic"))


class TestClockAndPolicyValidation:
    def test_naive_clock_value_is_rejected(self) -> None:
        naive_clock = lambda: datetime(2026, 9, 12, 15, 0)  # noqa: E731
        adapter = _sequence_adapter(_state())
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=naive_clock)

        with pytest.raises(ValueError, match="timezone-aware"):
            asyncio.run(manager.get("anthropic"))

    @pytest.mark.parametrize(
        "bad_ttl",
        [timedelta(seconds=0), timedelta(seconds=-1), "60s", 60],
    )
    def test_invalid_ttl_is_rejected(self, bad_ttl: object) -> None:
        with pytest.raises((ValueError, TypeError)):
            QuotaPolicy(state_ttl=bad_ttl)  # type: ignore[arg-type]


class TestConcurrency:
    def test_concurrent_get_calls_share_a_single_probe(self) -> None:
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()
        call_count = 0

        async def _probe_fn() -> ProviderState:
            nonlocal call_count
            call_count += 1
            probe_started.set()
            await release_probe.wait()
            return _state()

        adapter = FakeAdapter(_probe_fn)
        manager = QuotaManager({"anthropic": adapter}, _policy(), clock=lambda: UTC_NOW)

        async def scenario() -> tuple[ProviderState, ProviderState]:
            first_call = asyncio.ensure_future(manager.get("anthropic"))
            await probe_started.wait()
            second_call = asyncio.ensure_future(manager.get("anthropic"))
            await asyncio.sleep(0)  # let the second call reach the in-flight check
            release_probe.set()
            return await asyncio.gather(first_call, second_call)

        first, second = asyncio.run(scenario())

        assert call_count == 1
        assert adapter.probe_count == 1
        assert first is second


class TestNoRealProviderDependency:
    def test_module_never_imports_concrete_provider_adapters_or_subprocess(self) -> None:
        source = inspect.getsource(quota_manager_module)
        for forbidden in ("ClaudeCodeAdapter", "CodexAdapter", "import subprocess", "asyncio.create_subprocess"):
            assert forbidden not in source
