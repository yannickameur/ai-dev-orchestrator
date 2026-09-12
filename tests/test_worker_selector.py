"""Tests for WorkerSelector (Phase 1 / Slice 4).

All tests are offline: FakeAdapter + the real QuotaManager stand in for
ClaudeCodeAdapter/CodexAdapter — no subprocess, no network, no real
Claude/Codex/Ralph invocation anywhere in this file.

Worker fixtures are named Alice/Victor for readability only (per the task
brief) — the selector itself must never special-case those names or their
underlying providers; see ``TestNoHardcodedProvider``.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import worker_selector as worker_selector_module
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    ResetCredit,
    ResetCreditStatus,
    UnavailabilityReason,
)
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ReviewIndependenceError,
    UnknownWorkerError,
    Worker,
    WorkerSelectionPolicy,
    WorkerSelectionRequest,
    WorkerSelector,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _availability(available: bool, reason: UnavailabilityReason | None = None) -> ProviderAvailability:
    if not available and reason is None:
        reason = UnavailabilityReason.UNKNOWN
    return ProviderAvailability(available=available, observed_at=UTC_NOW, reason=reason)


def _state(
    provider: str,
    *,
    available: bool = True,
    reason: UnavailabilityReason | None = None,
    windows: tuple[QuotaWindow, ...] = (),
    reset_credits: tuple[ResetCredit, ...] = (),
) -> ProviderState:
    return ProviderState(
        provider=provider,
        availability=_availability(available, reason),
        observed_at=UTC_NOW,
        quota_windows=windows,
        reset_credits=reset_credits,
    )


class FakeAdapter(ProviderAdapter):
    """Always yields the same scripted outcome (a ProviderState or an exception)."""

    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.probe_count = 0

    async def probe(self) -> ProviderState:
        self.probe_count += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _quota_manager(adapters: dict[str, ProviderAdapter]) -> QuotaManager:
    return QuotaManager(
        adapters, QuotaPolicy(state_ttl=timedelta(seconds=3600)), clock=lambda: UTC_NOW
    )


def _alice(**overrides) -> Worker:
    fields = dict(
        worker_id="claude_dev_01",
        display_name="Alice",
        provider="anthropic",
        backend="claude_code",
        model="sonnet",
        capabilities=frozenset({"developer", "reviewer"}),
        priority=100,
    )
    fields.update(overrides)
    return Worker(**fields)


def _victor(**overrides) -> Worker:
    fields = dict(
        worker_id="codex_dev_01",
        display_name="Victor",
        provider="openai",
        backend="codex",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        capabilities=frozenset({"developer", "reviewer"}),
        priority=90,
    )
    fields.update(overrides)
    return Worker(**fields)


class TestWorkerModel:
    def test_two_workers_may_share_a_provider_with_different_models(self) -> None:
        fast = Worker(
            worker_id="codex_fast", display_name="Fast", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="low", capabilities=frozenset({"developer"}),
        )
        careful = Worker(
            worker_id="codex_careful", display_name="Careful", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="high", capabilities=frozenset({"developer"}),
        )
        assert fast.provider == careful.provider
        assert fast.worker_id != careful.worker_id

    def test_reasoning_effort_is_optional(self) -> None:
        worker = Worker(
            worker_id="claude_dev_01", display_name="Alice", provider="anthropic",
            backend="claude_code", model="sonnet", capabilities=frozenset({"developer"}),
        )
        assert worker.reasoning_effort is None

    def test_empty_worker_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Worker(
                worker_id="", display_name="Alice", provider="anthropic",
                backend="claude_code", model="sonnet",
            )

    def test_duplicate_worker_ids_are_rejected(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        with pytest.raises(ValueError, match="duplicate"):
            WorkerSelector([_alice(), _alice()], manager)


class TestBasicSelection:
    def test_single_compatible_available_worker_is_selected(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector([_alice()], manager)

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "claude_dev_01"

    def test_provider_unavailable_excludes_worker(self) -> None:
        manager = _quota_manager(
            {"anthropic": FakeAdapter(_state("anthropic", available=False, reason=UnavailabilityReason.QUOTA_EXHAUSTED))}
        )
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_provider_unknown_excludes_worker(self) -> None:
        manager = _quota_manager(
            {"anthropic": FakeAdapter(_state("anthropic", available=False, reason=UnavailabilityReason.UNKNOWN))}
        )
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_missing_capability_excludes_worker(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector(
            [_alice(capabilities=frozenset({"developer"}))], manager
        )

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(
                    WorkerSelectionRequest(required_capabilities=frozenset({"security_reviewer"}))
                )
            )

    def test_incompatible_role_excludes_worker(self) -> None:
        # Role is modeled as a capability string: a worker without
        # "reviewer" cannot be selected for a reviewer request.
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector(
            [_alice(capabilities=frozenset({"developer"}))], manager
        )

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"reviewer"})))
            )


class TestPriorityAndTieBreak:
    def test_higher_priority_worker_is_selected_deterministically(self) -> None:
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        selector = WorkerSelector(
            [_alice(priority=100), _victor(priority=90)], manager
        )

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "claude_dev_01"

    def test_equal_priority_ties_break_on_worker_id_lexical_order(self) -> None:
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        selector = WorkerSelector(
            [_alice(priority=50), _victor(priority=50)], manager
        )

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        # "claude_dev_01" < "codex_dev_01" lexically.
        assert selected.worker_id == "claude_dev_01"

    def test_two_workers_on_the_same_provider_use_only_priority_no_provider_logic(self) -> None:
        adapter = FakeAdapter(_state("openai"))
        manager = _quota_manager({"openai": adapter})
        fast = Worker(
            worker_id="codex_fast", display_name="Fast", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="low", capabilities=frozenset({"developer"}),
            priority=10,
        )
        careful = Worker(
            worker_id="codex_careful", display_name="Careful", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="high", capabilities=frozenset({"developer"}),
            priority=20,
        )
        selector = WorkerSelector([fast, careful], manager)

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "codex_careful"
        assert adapter.probe_count == 1  # single provider probed once, not per worker


class TestNoHardcodedProvider:
    def test_selector_class_logic_never_names_a_concrete_provider(self) -> None:
        # The module docstring may reference Claude/Codex as examples for
        # human readers; the actual selection algorithm (the class body)
        # must not special-case any concrete provider/backend name.
        source = inspect.getsource(WorkerSelector)
        for forbidden in (
            "claude", "Claude", "codex", "Codex", "anthropic", "openai",
            "ClaudeCodeAdapter", "CodexAdapter",
        ):
            assert forbidden not in source

    def test_module_never_imports_concrete_provider_adapters(self) -> None:
        source = inspect.getsource(worker_selector_module)
        for forbidden in ("ClaudeCodeAdapter", "CodexAdapter", "import subprocess"):
            assert forbidden not in source


class TestAuthorReviewerIndependence:
    def test_claude_author_codex_reviewer_available_selects_codex(self) -> None:
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        selector = WorkerSelector([_alice(), _victor()], manager)

        selected = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"reviewer"}),
                    author_worker_id="claude_dev_01",
                )
            )
        )

        assert selected.worker_id == "codex_dev_01"

    def test_codex_author_claude_reviewer_available_selects_claude(self) -> None:
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        selector = WorkerSelector([_alice(), _victor()], manager)

        selected = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"reviewer"}),
                    author_worker_id="codex_dev_01",
                )
            )
        )

        assert selected.worker_id == "claude_dev_01"

    def test_reviewer_can_never_be_the_author_worker(self) -> None:
        # Only one worker exists at all: it authored, so it cannot review.
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({"reviewer"}),
                        author_worker_id="claude_dev_01",
                    )
                )
            )

    def test_require_distinct_provider_with_only_same_provider_reviewer_raises(self) -> None:
        alice = _alice()
        second_claude = Worker(
            worker_id="claude_dev_02", display_name="Bob", provider="anthropic",
            backend="claude_code", model="haiku", capabilities=frozenset({"developer", "reviewer"}),
            priority=50,
        )
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        policy = WorkerSelectionPolicy(require_distinct_provider_for_review=True)
        selector = WorkerSelector([alice, second_claude], manager, policy)

        with pytest.raises(ReviewIndependenceError):
            asyncio.run(
                selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({"reviewer"}),
                        author_worker_id="claude_dev_01",
                    )
                )
            )

    def test_prefer_distinct_provider_picks_cross_provider_when_available(self) -> None:
        alice = _alice()
        victor = _victor()
        second_claude = Worker(
            worker_id="claude_dev_02", display_name="Bob", provider="anthropic",
            backend="claude_code", model="haiku", capabilities=frozenset({"developer", "reviewer"}),
            priority=999,  # would win on priority alone, but same provider as author
        )
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        policy = WorkerSelectionPolicy(prefer_distinct_provider_for_review=True)
        selector = WorkerSelector([alice, victor, second_claude], manager, policy)

        selected = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"reviewer"}),
                    author_worker_id="claude_dev_01",
                )
            )
        )

        assert selected.worker_id == "codex_dev_01"

    def test_prefer_distinct_provider_falls_back_to_same_provider_when_necessary(self) -> None:
        alice = _alice()
        second_claude = Worker(
            worker_id="claude_dev_02", display_name="Bob", provider="anthropic",
            backend="claude_code", model="haiku", capabilities=frozenset({"developer", "reviewer"}),
            priority=50,
        )
        # No openai worker registered at all: cross-provider is impossible.
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        policy = WorkerSelectionPolicy(
            prefer_distinct_provider_for_review=True, require_distinct_provider_for_review=False
        )
        selector = WorkerSelector([alice, second_claude], manager, policy)

        selected = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"reviewer"}),
                    author_worker_id="claude_dev_01",
                )
            )
        )

        assert selected.worker_id == "claude_dev_02"

    def test_require_distinct_provider_worker_id_flag_cannot_be_disabled(self) -> None:
        with pytest.raises(ValueError, match="require_distinct_worker_for_review must be True"):
            WorkerSelectionPolicy(require_distinct_worker_for_review=False)

    def test_cross_provider_reviewer_unavailable_falls_back_per_prefer_policy(self) -> None:
        # openai worker exists but its provider is unavailable: prefer
        # policy must fall back to the same-provider reviewer rather than
        # raising, since require_distinct_provider_for_review is False.
        alice = _alice()
        victor = _victor()
        second_claude = Worker(
            worker_id="claude_dev_02", display_name="Bob", provider="anthropic",
            backend="claude_code", model="haiku", capabilities=frozenset({"developer", "reviewer"}),
            priority=50,
        )
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic")),
                "openai": FakeAdapter(_state("openai", available=False, reason=UnavailabilityReason.QUOTA_EXHAUSTED)),
            }
        )
        policy = WorkerSelectionPolicy(prefer_distinct_provider_for_review=True)
        selector = WorkerSelector([alice, victor, second_claude], manager, policy)

        selected = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"reviewer"}),
                    author_worker_id="claude_dev_01",
                )
            )
        )

        assert selected.worker_id == "claude_dev_02"

    def test_unknown_author_worker_id_raises(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(UnknownWorkerError):
            asyncio.run(
                selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({"reviewer"}),
                        author_worker_id="does-not-exist",
                    )
                )
            )


class TestQuotaManagerErrorHandling:
    """EXPECTED PROVIDER FAILURE != PROGRAMMING FAILURE.

    A ``ProviderProbeError`` (QuotaManager's own domain error for a failed
    adapter probe) is an expected provider-level failure: the provider is
    treated as unavailable for this selection, other providers are still
    evaluated. Any other, unexpected exception must propagate unchanged —
    it must never be swallowed or reinterpreted as "not available".
    """

    def test_provider_probe_error_on_unique_provider_raises_no_eligible_worker(self) -> None:
        boom = RuntimeError("provider unreachable")
        manager = _quota_manager({"anthropic": FakeAdapter(boom)})
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_provider_probe_error_on_one_provider_still_allows_selecting_another(self) -> None:
        boom = RuntimeError("provider unreachable")
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(boom),
                "openai": FakeAdapter(_state("openai")),
            }
        )
        selector = WorkerSelector([_alice(), _victor()], manager)

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "codex_dev_01"

    def test_unexpected_exception_from_quota_manager_propagates_unchanged(self) -> None:
        class _ExplodingQuotaManager:
            async def get(self, provider: str) -> ProviderState:
                raise TypeError("unexpected programming failure, not a provider failure")

        selector = WorkerSelector([_alice()], _ExplodingQuotaManager())

        with pytest.raises(TypeError, match="unexpected programming failure"):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_unexpected_exception_on_one_provider_is_not_masked_by_another_providers_success(
        self,
    ) -> None:
        # Even when another provider would have yielded an eligible worker,
        # a genuine programming failure must still surface, not be hidden.
        class _PartiallyExplodingQuotaManager:
            def __init__(self, real: QuotaManager) -> None:
                self._real = real

            async def get(self, provider: str) -> ProviderState:
                if provider == "anthropic":
                    raise RuntimeError("boom: not a ProviderProbeError")
                return await self._real.get(provider)

        real_manager = _quota_manager({"openai": FakeAdapter(_state("openai"))})
        selector = WorkerSelector(
            [_alice(), _victor()], _PartiallyExplodingQuotaManager(real_manager)
        )

        with pytest.raises(RuntimeError, match="boom: not a ProviderProbeError"):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )


class TestResetCreditsAndQuotaNumbersIgnored:
    def test_reset_credit_presence_does_not_make_unavailable_provider_selectable(self) -> None:
        credit = ResetCredit(title="Full reset (Weekly + 5 hr)", status=ResetCreditStatus.AVAILABLE, available_count=1)
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(
                    _state(
                        "anthropic",
                        available=False,
                        reason=UnavailabilityReason.QUOTA_EXHAUSTED,
                        reset_credits=(credit,),
                    )
                )
            }
        )
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_none_utilization_window_does_not_break_selection(self) -> None:
        window = QuotaWindow(window_type="five_hour", source="claude_stream_json", observed_at=UTC_NOW)
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic", windows=(window,)))})
        selector = WorkerSelector([_alice()], manager)

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "claude_dev_01"

    def test_quota_window_utilization_never_influences_ranking(self) -> None:
        # Alice has a much higher observed utilization than Victor, but
        # equal priority: the tie-break must stay lexical on worker_id,
        # never favor the "less used" provider absent an explicit policy.
        heavy_use = QuotaWindow(
            window_type="five_hour", source="claude_stream_json", observed_at=UTC_NOW, utilization=0.99
        )
        light_use = QuotaWindow(
            window_type="primary_5h", source="codex_app_server", observed_at=UTC_NOW, utilization=0.01
        )
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(_state("anthropic", windows=(heavy_use,))),
                "openai": FakeAdapter(_state("openai", windows=(light_use,))),
            }
        )
        selector = WorkerSelector(
            [_alice(priority=50), _victor(priority=50)], manager
        )

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "claude_dev_01"
