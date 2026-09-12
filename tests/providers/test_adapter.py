"""Tests for the ProviderAdapter contract (Phase 1 / Slice 0).

No concrete provider adapter exists yet (ClaudeCodeAdapter/CodexAdapter are
later slices) — these tests only check the abstract interface itself,
using a minimal in-test fake.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


class FakeAdapter(ProviderAdapter):
    """Minimal adapter used only to exercise the probe() contract."""

    def __init__(self, state: ProviderState) -> None:
        self._state = state

    async def probe(self) -> ProviderState:
        return self._state


def _state() -> ProviderState:
    return ProviderState(
        provider="fake",
        availability=ProviderAvailability(available=True, observed_at=UTC_NOW),
        observed_at=UTC_NOW,
    )


def test_provider_adapter_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ProviderAdapter()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    class IncompleteAdapter(ProviderAdapter):
        pass

    with pytest.raises(TypeError):
        IncompleteAdapter()  # type: ignore[abstract]


def test_probe_returns_a_provider_state() -> None:
    expected = _state()
    adapter = FakeAdapter(expected)

    result = asyncio.run(adapter.probe())

    assert isinstance(result, ProviderState)
    assert result is expected


def test_probe_is_a_coroutine_function() -> None:
    adapter = FakeAdapter(_state())
    coroutine = adapter.probe()
    try:
        assert asyncio.iscoroutine(coroutine)
    finally:
        coroutine.close()


def test_adapter_interface_has_no_execution_methods() -> None:
    # This interface is a read-only probe, never an execution engine.
    forbidden = {"execute", "run", "complete", "review", "fallback"}
    declared = set(vars(ProviderAdapter))
    assert forbidden.isdisjoint(declared)
