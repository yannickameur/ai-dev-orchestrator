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
    ExecutionProfile,
    NoEligibleWorkerError,
    ProviderSelectionDiagnostic,
    QualityTier,
    ReviewIndependenceError,
    UnknownExecutionProfileError,
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
    return Worker.with_single_profile(**fields)


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
    return Worker.with_single_profile(**fields)


class TestWorkerModel:
    def test_two_workers_may_share_a_provider_with_different_models(self) -> None:
        fast = Worker.with_single_profile(
            worker_id="codex_fast", display_name="Fast", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="low", capabilities=frozenset({"developer"}),
        )
        careful = Worker.with_single_profile(
            worker_id="codex_careful", display_name="Careful", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="high", capabilities=frozenset({"developer"}),
        )
        assert fast.provider == careful.provider
        assert fast.worker_id != careful.worker_id

    def test_reasoning_effort_is_optional(self) -> None:
        worker = Worker.with_single_profile(
            worker_id="claude_dev_01", display_name="Alice", provider="anthropic",
            backend="claude_code", model="sonnet", capabilities=frozenset({"developer"}),
        )
        assert worker.profile().reasoning_effort is None

    def test_empty_worker_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Worker.with_single_profile(
                worker_id="", display_name="Alice", provider="anthropic",
                backend="claude_code", model="sonnet",
            )

    def test_duplicate_worker_ids_are_rejected(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        with pytest.raises(ValueError, match="duplicate"):
            WorkerSelector([_alice(), _alice()], manager)


class TestExecutionProfilesOnWorker:
    def _multi_profile_worker(self, **overrides) -> Worker:
        fields = dict(
            worker_id="victor", display_name="Victor", provider="openai", backend="codex",
            capabilities=frozenset({"developer"}),
            profiles=(
                ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="gpt-5.6-terra", reasoning_effort="low"),
                ExecutionProfile(profile_id="deep", quality_tier=QualityTier.COMPLEX, model="gpt-5.6-terra", reasoning_effort="high"),
            ),
            default_profile_id="economy",
        )
        fields.update(overrides)
        return Worker(**fields)

    def test_worker_requires_at_least_one_profile(self) -> None:
        with pytest.raises(ValueError, match="at least one execution profile"):
            Worker(
                worker_id="w1", display_name="W", provider="openai", backend="codex",
                capabilities=frozenset({"developer"}), profiles=(),
            )

    def test_single_profile_default_is_auto_resolved(self) -> None:
        worker = Worker.with_single_profile(
            worker_id="w1", display_name="W", provider="openai", backend="codex", model="gpt-5.6-terra",
        )
        assert worker.default_profile_id == "default"
        assert worker.profile().model == "gpt-5.6-terra"

    def test_multiple_profiles_require_explicit_default(self) -> None:
        with pytest.raises(ValueError, match="default_profile_id"):
            self._multi_profile_worker(default_profile_id=None)

    def test_duplicate_profile_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate profile_id"):
            Worker(
                worker_id="w1", display_name="W", provider="openai", backend="codex",
                capabilities=frozenset({"developer"}),
                profiles=(
                    ExecutionProfile(profile_id="p", quality_tier=QualityTier.SIMPLE, model="m1"),
                    ExecutionProfile(profile_id="p", quality_tier=QualityTier.STANDARD, model="m2"),
                ),
                default_profile_id="p",
            )

    def test_default_profile_id_must_reference_a_real_profile(self) -> None:
        with pytest.raises(ValueError, match="default_profile_id"):
            self._multi_profile_worker(default_profile_id="nonexistent")

    def test_estimator_profile_id_must_reference_a_real_profile(self) -> None:
        with pytest.raises(ValueError, match="estimator_profile_id"):
            self._multi_profile_worker(estimator_profile_id="nonexistent")

    def test_estimator_profile_id_is_optional(self) -> None:
        worker = self._multi_profile_worker()
        assert worker.estimator_profile_id is None

    def test_profile_lookup_by_id(self) -> None:
        worker = self._multi_profile_worker()
        assert worker.profile("deep").quality_tier is QualityTier.COMPLEX
        assert worker.profile("deep").reasoning_effort == "high"

    def test_profile_lookup_defaults_to_default_profile_id(self) -> None:
        worker = self._multi_profile_worker()
        assert worker.profile().profile_id == "economy"

    def test_unknown_profile_id_raises(self) -> None:
        worker = self._multi_profile_worker()
        with pytest.raises(UnknownExecutionProfileError):
            worker.profile("nope")

    def test_profiles_and_worker_are_immutable(self) -> None:
        worker = self._multi_profile_worker()
        assert isinstance(worker.profiles, tuple)
        with pytest.raises(AttributeError):
            worker.profiles = ()
        with pytest.raises(AttributeError):
            worker.profile("deep").model = "changed"

    def test_enabled_defaults_true_and_is_settable(self) -> None:
        assert self._multi_profile_worker().enabled is True
        assert self._multi_profile_worker(enabled=False).enabled is False

    def test_invalid_quality_tier_type_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            ExecutionProfile(profile_id="p", quality_tier=3, model="m")

    def test_reasoning_effort_none_is_supported_on_profile(self) -> None:
        profile = ExecutionProfile(profile_id="p", quality_tier=QualityTier.STANDARD, model="m")
        assert profile.reasoning_effort is None

    def test_reasoning_effort_value_is_supported_on_profile(self) -> None:
        profile = ExecutionProfile(profile_id="p", quality_tier=QualityTier.STANDARD, model="m", reasoning_effort="high")
        assert profile.reasoning_effort == "high"


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
        fast = Worker.with_single_profile(
            worker_id="codex_fast", display_name="Fast", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="low", capabilities=frozenset({"developer"}),
            priority=10,
        )
        careful = Worker.with_single_profile(
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


class TestEnabledFiltering:
    def test_disabled_worker_is_never_selected(self) -> None:
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector([_alice(enabled=False)], manager)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

    def test_enabled_worker_is_selected_even_alongside_a_disabled_one(self) -> None:
        manager = _quota_manager(
            {"anthropic": FakeAdapter(_state("anthropic")), "openai": FakeAdapter(_state("openai"))}
        )
        selector = WorkerSelector([_alice(enabled=False), _victor()], manager)

        selected = asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert selected.worker_id == "codex_dev_01"

    def test_disabled_worker_is_still_a_known_worker_for_author_resolution(self) -> None:
        # enabled=False only removes a worker as a *candidate*; it must
        # still be resolvable by worker_id (e.g. as a historical author
        # reference), never simply absent from the registry.
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        selector = WorkerSelector([_alice(enabled=False)], manager)
        assert selector._resolve_author("claude_dev_01").worker_id == "claude_dev_01"


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
        second_claude = Worker.with_single_profile(
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
        second_claude = Worker.with_single_profile(
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
        second_claude = Worker.with_single_profile(
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
        second_claude = Worker.with_single_profile(
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


class TestSelectionFailureDiagnostics:
    """Slice 11: structured, read-only diagnostics attached to a selection
    failure — never used by WorkerSelector itself to wait/retry/rank.
    """

    def test_no_eligible_worker_diagnostic_reports_quota_exhausted_with_reset(self, tmp_path) -> None:
        reset_at = UTC_NOW + timedelta(hours=3)
        window = QuotaWindow(
            window_type="five_hour", source="claude_stream_json", observed_at=UTC_NOW,
            utilization=1.0, reset_at=reset_at,
        )
        manager = _quota_manager(
            {
                "anthropic": FakeAdapter(
                    _state("anthropic", available=False, reason=UnavailabilityReason.QUOTA_EXHAUSTED, windows=(window,))
                )
            }
        )
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError) as exc_info:
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

        diagnostics = exc_info.value.diagnostics
        assert len(diagnostics) == 1
        assert diagnostics[0] == ProviderSelectionDiagnostic(
            provider="anthropic", available=False, reason="quota_exhausted", reset_at=(reset_at,)
        )

    def test_no_eligible_worker_diagnostic_reports_probe_error(self, tmp_path) -> None:
        boom = RuntimeError("provider unreachable")
        manager = _quota_manager({"anthropic": FakeAdapter(boom)})
        selector = WorkerSelector([_alice()], manager)

        with pytest.raises(NoEligibleWorkerError) as exc_info:
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )

        diagnostics = exc_info.value.diagnostics
        assert diagnostics[0].provider == "anthropic"
        assert diagnostics[0].available is False
        assert diagnostics[0].reason == "probe_error"
        assert diagnostics[0].reset_at == ()

    def test_no_eligible_worker_diagnostic_empty_when_no_candidates_at_all(self) -> None:
        manager = _quota_manager({})
        selector = WorkerSelector([_alice(capabilities=frozenset({"developer"}))], manager)

        with pytest.raises(NoEligibleWorkerError) as exc_info:
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"security_reviewer"})))
            )

        assert exc_info.value.diagnostics == ()

    def test_review_independence_diagnostic_reports_all_candidate_providers(self, tmp_path) -> None:
        alice = _alice()
        second_claude = Worker.with_single_profile(
            worker_id="claude_dev_02", display_name="Bob", provider="anthropic",
            backend="claude_code", model="haiku", capabilities=frozenset({"developer", "reviewer"}),
            priority=50,
        )
        manager = _quota_manager({"anthropic": FakeAdapter(_state("anthropic"))})
        policy = WorkerSelectionPolicy(require_distinct_provider_for_review=True)
        selector = WorkerSelector([alice, second_claude], manager, policy)

        with pytest.raises(ReviewIndependenceError) as exc_info:
            asyncio.run(
                selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({"reviewer"}),
                        author_worker_id="claude_dev_01",
                    )
                )
            )

        diagnostics = exc_info.value.diagnostics
        assert len(diagnostics) == 1
        assert diagnostics[0].provider == "anthropic"
        assert diagnostics[0].available is True

    def test_no_eligible_worker_default_diagnostics_when_constructed_directly(self) -> None:
        # Backward compatible: existing callers construct this error without
        # diagnostics (e.g. tests injecting a fake WorkerSelector failure).
        error = NoEligibleWorkerError(WorkerSelectionRequest(required_capabilities=frozenset()))
        assert error.diagnostics == ()


def _milo(**overrides) -> Worker:
    fields = dict(
        worker_id="mistral_dev_01",
        display_name="Milo",
        provider="mistral",
        backend="vibe",
        model="vibe-default",
        capabilities=frozenset({"developer"}),
        priority=60,
    )
    fields.update(overrides)
    return Worker.with_single_profile(**fields)


def _juno(**overrides) -> Worker:
    fields = dict(
        worker_id="mistral_dev_02",
        display_name="Juno",
        provider="mistral",
        backend="vibe",
        model="vibe-default",
        capabilities=frozenset({"developer"}),
        priority=60,
    )
    fields.update(overrides)
    return Worker.with_single_profile(**fields)


class TestMistralProviderIntegration:
    """Mistral (Vibe, post-MVP 0.1 — see docs/VIBE_SPIKE.md) is a third
    provider added purely by configuration: none of these tests require
    WorkerSelector to know the string "mistral" exists — the same generic
    capability/governance/quota pipeline as Anthropic/OpenAI is exercised
    unchanged, using ``milo``/``juno`` fixtures shaped exactly like the
    real ``config/workers.yaml`` entries."""

    def test_provider_mistral_goes_through_normal_availability_diagnosis(self) -> None:
        milo = _milo()
        manager = _quota_manager({"mistral": FakeAdapter(_state("mistral", available=False))})
        selector = WorkerSelector([milo], manager)

        with pytest.raises(NoEligibleWorkerError) as exc_info:
            asyncio.run(
                selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
            )
        assert exc_info.value.diagnostics == (
            ProviderSelectionDiagnostic(provider="mistral", available=False, reason="unknown"),
        )

    def test_milo_and_juno_are_distinct_worker_ids(self) -> None:
        assert _milo().worker_id != _juno().worker_id

    def test_dev_b_can_use_second_mistral_worker_when_only_mistral_available(self) -> None:
        milo, juno = _milo(), _juno()
        manager = _quota_manager({"mistral": FakeAdapter(_state("mistral"))})
        selector = WorkerSelector([milo, juno], manager)

        chosen = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"developer"}), author_worker_id="mistral_dev_01",
                )
            )
        )

        assert chosen.worker_id == "mistral_dev_02"
        assert chosen.worker_id != milo.worker_id

    def test_provider_diversity_still_preferred_when_anthropic_and_mistral_both_available(self) -> None:
        alice, milo = _alice(), _milo()
        manager = _quota_manager({
            "anthropic": FakeAdapter(_state("anthropic")),
            "mistral": FakeAdapter(_state("mistral")),
        })
        selector = WorkerSelector([alice, milo], manager)

        chosen = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"developer"}), author_worker_id="claude_dev_01",
                )
            )
        )

        assert chosen.provider == "mistral"  # only cross-provider candidate

    def test_same_provider_mistral_fallback_when_no_other_provider_available(self) -> None:
        milo, juno = _milo(), _juno()
        victor = _victor(capabilities=frozenset({"developer"}))
        manager = _quota_manager({
            "mistral": FakeAdapter(_state("mistral")),
            "openai": FakeAdapter(_state("openai", available=False, reason=UnavailabilityReason.QUOTA_EXHAUSTED)),
        })
        selector = WorkerSelector([milo, juno, victor], manager)

        chosen = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"developer"}), author_worker_id="mistral_dev_01",
                )
            )
        )

        assert chosen.worker_id == "mistral_dev_02"  # same-provider fallback, never a WAIT-inducing failure

    def test_no_wait_inducing_failure_solely_because_dev_b_lacks_a_different_provider(self) -> None:
        """Mirrors the existing cross-provider fallback guarantee: with two
        independent Mistral workers, excluding the author never raises
        NoEligibleWorkerError merely because no *other* provider exists."""
        milo, juno = _milo(), _juno()
        manager = _quota_manager({"mistral": FakeAdapter(_state("mistral"))})
        selector = WorkerSelector([milo, juno], manager)

        chosen = asyncio.run(
            selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({"developer"}), author_worker_id="mistral_dev_01",
                )
            )
        )
        assert chosen.worker_id == "mistral_dev_02"

    def test_mistral_provider_state_is_shared_across_both_workers(self) -> None:
        """A single QuotaManager.get("mistral") probe covers both milo and
        juno — never an independent quota per worker (see
        MistralVibeAdapter's own EXECUTION_PROBE_ONLY docstring)."""
        milo, juno = _milo(), _juno()
        adapter = FakeAdapter(_state("mistral"))
        manager = _quota_manager({"mistral": adapter})
        selector = WorkerSelector([milo, juno], manager)

        asyncio.run(
            selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"developer"})))
        )

        assert adapter.probe_count == 1  # one shared probe, not one per worker

    def test_worker_selector_source_never_names_mistral_or_vibe(self) -> None:
        """Extends TestNoHardcodedProvider to the newly added provider:
        WorkerSelector's own logic must never special-case "mistral"/"vibe"
        any more than it does "anthropic"/"openai"/"claude"/"codex"."""
        source = inspect.getsource(worker_selector_module)
        for needle in ("mistral", "vibe", "milo", "juno"):
            assert needle not in source.lower()
