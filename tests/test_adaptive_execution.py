"""Tests for adaptive Worker/Profile selection for development/rework
(Phase 1 / Slice 17).

All tests are offline: fake WorkerSelector/RalphExecutionEngine stand in
for the real ones — no subprocess, no network, no real
Claude/Codex/Ralph invocation anywhere in this file. No quota, no reset
credit is ever really consumed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.adaptive_execution import (
    AdaptiveExecutionDecision,
    AdaptiveExecutionDecisionStore,
    AdaptiveExecutionSelector,
    NoCapableProfileError,
    resolve_profile,
)
from orchestrator.complexity_estimation import (
    ComplexityEstimationRequest,
    ExecutionRecommendation,
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
)
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent
from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.worker_selector import (
    ExecutionProfile,
    NoEligibleWorkerError,
    QualityTier,
    Worker,
    WorkerSelectionRequest,
    WorkerSelector,
)

UTC_NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)


def _counting_id_factory(prefix: str = "id"):
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return id_factory


def _profile(profile_id: str, tier: QualityTier, *, model: str = "m", reasoning_effort: str | None = None, cost_rank: int = 0) -> ExecutionProfile:
    return ExecutionProfile(profile_id=profile_id, quality_tier=tier, model=model, reasoning_effort=reasoning_effort, cost_rank=cost_rank)


def _worker(worker_id: str, profiles: tuple[ExecutionProfile, ...], **overrides) -> Worker:
    fields = dict(
        worker_id=worker_id, display_name=worker_id.title(), provider="anthropic", backend="claude_code",
        capabilities=frozenset({"development"}), profiles=profiles, default_profile_id=profiles[0].profile_id,
    )
    fields.update(overrides)
    return Worker(**fields)


class TestResolveProfileTierFilter:
    def test_simple_tier_admits_simple_profile(self) -> None:
        w = _worker("w1", (_profile("economy", QualityTier.SIMPLE),))
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.SIMPLE, recommended_reasoning=None)
        assert profile.profile_id == "economy"

    def test_standard_tier_never_picks_simple(self) -> None:
        w = _worker("w1", (_profile("economy", QualityTier.SIMPLE), _profile("standard", QualityTier.STANDARD)))
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.quality_tier is QualityTier.STANDARD

    def test_complex_tier_never_picks_simple_or_standard(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("economy", QualityTier.SIMPLE), _profile("standard", QualityTier.STANDARD),
                _profile("deep", QualityTier.COMPLEX),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.COMPLEX, recommended_reasoning=None)
        assert profile.quality_tier is QualityTier.COMPLEX

    def test_critical_tier_only_picks_critical_or_above(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("deep", QualityTier.COMPLEX), _profile("max", QualityTier.CRITICAL),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.CRITICAL, recommended_reasoning=None)
        assert profile.quality_tier is QualityTier.CRITICAL

    def test_profile_above_minimum_is_admissible_when_no_exact_tier_exists(self) -> None:
        w = _worker("w1", (_profile("deep", QualityTier.COMPLEX),))
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "deep"

    def test_no_capable_profile_raises(self) -> None:
        w = _worker("w1", (_profile("economy", QualityTier.SIMPLE),))
        with pytest.raises(NoCapableProfileError):
            resolve_profile(w, minimum_quality_tier=QualityTier.CRITICAL, recommended_reasoning=None)


class TestResolveProfileCostAndTierProximity:
    def test_cheapest_sufficient_profile_is_chosen(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("standard", QualityTier.STANDARD, cost_rank=20),
                _profile("deep", QualityTier.COMPLEX, cost_rank=30),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "standard"

    def test_cost_rank_is_deterministic(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("deep", QualityTier.COMPLEX, cost_rank=30),
                _profile("critical", QualityTier.CRITICAL, cost_rank=10),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.COMPLEX, recommended_reasoning=None)
        assert profile.profile_id == "critical"  # lower cost_rank wins even though tier is higher

    def test_equal_cost_rank_breaks_on_tier_then_profile_id(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("bravo", QualityTier.COMPLEX, cost_rank=10),
                _profile("alpha", QualityTier.STANDARD, cost_rank=10),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "alpha"  # same cost_rank, closer tier wins

    def test_equal_cost_rank_and_tier_breaks_on_profile_id(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("zzz", QualityTier.STANDARD, cost_rank=10),
                _profile("aaa", QualityTier.STANDARD, cost_rank=10),
            ),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "aaa"

    def test_default_cost_rank_zero_falls_back_to_tier_proximity(self) -> None:
        w = _worker(
            "w1",
            (_profile("standard", QualityTier.STANDARD), _profile("deep", QualityTier.COMPLEX)),
        )
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "standard"  # never over-provisions by default


class TestResolveProfileReasoningHint:
    def test_exact_reasoning_match_is_preferred(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("low", QualityTier.STANDARD, reasoning_effort="low", cost_rank=10),
                _profile("high", QualityTier.STANDARD, reasoning_effort="high", cost_rank=10),
            ),
        )
        profile, rationale = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning="high")
        assert profile.profile_id == "high"
        assert "honored exactly" in rationale

    def test_reasoning_hint_absent_is_supported(self) -> None:
        w = _worker("w1", (_profile("standard", QualityTier.STANDARD),))
        profile, _ = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning=None)
        assert profile.profile_id == "standard"

    def test_backend_without_reasoning_effort_is_supported(self) -> None:
        # Claude-style profiles with reasoning_effort=None must never be
        # excluded just because the hint can't be matched.
        w = _worker("w1", (_profile("standard", QualityTier.STANDARD, reasoning_effort=None),))
        profile, rationale = resolve_profile(w, minimum_quality_tier=QualityTier.STANDARD, recommended_reasoning="high")
        assert profile.profile_id == "standard"
        assert "could not be matched exactly" in rationale

    def test_unsatisfiable_hint_never_downgrades_tier(self) -> None:
        w = _worker(
            "w1",
            (
                _profile("standard", QualityTier.STANDARD, reasoning_effort="low"),
                _profile("deep", QualityTier.COMPLEX, reasoning_effort=None),
            ),
        )
        profile, rationale = resolve_profile(w, minimum_quality_tier=QualityTier.COMPLEX, recommended_reasoning="high")
        assert profile.quality_tier is QualityTier.COMPLEX
        assert profile.profile_id == "deep"


class TestWorkerSelectorTierFiltering:
    def _adapter(self, available: bool = True):
        class _FakeAdapter(ProviderAdapter):
            async def probe(self_inner) -> ProviderState:
                return ProviderState(
                    provider="anthropic",
                    availability=ProviderAvailability(
                        available=available, observed_at=UTC_NOW,
                        reason=None if available else UnavailabilityReason.QUOTA_EXHAUSTED,
                    ),
                    observed_at=UTC_NOW,
                )
        return _FakeAdapter()

    def test_worker_without_capable_profile_is_excluded(self) -> None:
        weak = _worker("weak", (_profile("economy", QualityTier.SIMPLE),))
        manager = QuotaManager({"anthropic": self._adapter()}, QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW)
        selector = WorkerSelector([weak], manager)
        with pytest.raises(NoEligibleWorkerError) as excinfo:
            asyncio.run(selector.select(WorkerSelectionRequest(
                required_capabilities=frozenset({"development"}), minimum_quality_tier=QualityTier.COMPLEX,
            )))
        assert excinfo.value.diagnostics == ()  # never diagnosed: filtered before quota probing

    def test_worker_with_capable_profile_is_selected(self) -> None:
        strong = _worker("strong", (_profile("deep", QualityTier.COMPLEX),))
        manager = QuotaManager({"anthropic": self._adapter()}, QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW)
        selector = WorkerSelector([strong], manager)
        selected = asyncio.run(selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), minimum_quality_tier=QualityTier.COMPLEX,
        )))
        assert selected.worker_id == "strong"

    def test_disabled_capable_worker_is_still_excluded(self) -> None:
        strong = _worker("strong", (_profile("deep", QualityTier.COMPLEX),), enabled=False)
        manager = QuotaManager({"anthropic": self._adapter()}, QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW)
        selector = WorkerSelector([strong], manager)
        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(selector.select(WorkerSelectionRequest(
                required_capabilities=frozenset({"development"}), minimum_quality_tier=QualityTier.COMPLEX,
            )))

    def test_quota_exhausted_capable_worker_excluded_other_provider_selected(self) -> None:
        victor = _worker("victor", (_profile("deep", QualityTier.COMPLEX),), provider="openai", backend="codex")
        alice = _worker("alice", (_profile("deep", QualityTier.COMPLEX),), provider="anthropic")
        manager = QuotaManager(
            {"openai": self._adapter(available=False), "anthropic": self._adapter(available=True)},
            QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW,
        )
        selector = WorkerSelector([victor, alice], manager)
        selected = asyncio.run(selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), minimum_quality_tier=QualityTier.COMPLEX,
        )))
        assert selected.worker_id == "alice"

    def test_no_capable_worker_anywhere_yields_empty_diagnostics(self) -> None:
        weak1 = _worker("weak1", (_profile("economy", QualityTier.SIMPLE),), provider="anthropic")
        weak2 = _worker("weak2", (_profile("economy", QualityTier.SIMPLE),), provider="openai", backend="codex")
        manager = QuotaManager(
            {"anthropic": self._adapter(available=False), "openai": self._adapter(available=False)},
            QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW,
        )
        selector = WorkerSelector([weak1, weak2], manager)
        with pytest.raises(NoEligibleWorkerError) as excinfo:
            asyncio.run(selector.select(WorkerSelectionRequest(
                required_capabilities=frozenset({"development"}), minimum_quality_tier=QualityTier.CRITICAL,
            )))
        # Never diagnosable as quota: these workers were never even capable.
        assert excinfo.value.diagnostics == ()


# --- AdaptiveExecutionSelector (service-level) -------------------------------


class FakeEstimatorAndDevSelector:
    """A WorkerSelector-shaped fake used only to unit-test
    AdaptiveExecutionSelector.select() in isolation from the real
    WorkerSelector (already covered above)."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker
        self.requests: list[WorkerSelectionRequest] = []

    async def select(self, request: WorkerSelectionRequest) -> Worker:
        self.requests.append(request)
        return self._worker


class FakeRecommendationService:
    def __init__(self, recommendation: ExecutionRecommendation) -> None:
        self._recommendation = recommendation
        self.calls: list[tuple] = []

    async def estimate(self, request: ComplexityEstimationRequest, *, force_refresh: bool = False):
        self.calls.append((request, force_refresh))
        return self._recommendation


def _recommendation(**overrides) -> ExecutionRecommendation:
    fields = dict(
        recommendation_id="rec-1", project_id="proj-1", role="developer", estimator_worker_id="alice",
        estimator_execution_id="exec-est-1", estimator_profile_id="economy", task_fingerprint="fp-1",
        minimum_quality_tier=QualityTier.COMPLEX, reasons=("cross-module",), created_at=UTC_NOW,
    )
    fields.update(overrides)
    return ExecutionRecommendation(**fields)


def _estimation_request(tmp_path: Path, **overrides) -> ComplexityEstimationRequest:
    fields = dict(project_id="proj-1", role="developer", workspace=tmp_path, objective="Implement the thing")
    fields.update(overrides)
    return ComplexityEstimationRequest(**fields)


def _run(coro):
    return asyncio.run(coro)


class TestAdaptiveExecutionSelectorService:
    def test_select_persists_and_returns_decision(self, tmp_path: Path) -> None:
        store = AdaptiveExecutionDecisionStore(tmp_path / "decisions.sqlite3", clock=lambda: UTC_NOW)
        worker = _worker("victor", (_profile("deep", QualityTier.COMPLEX, cost_rank=30),), provider="openai", backend="codex")
        dev_selector = FakeEstimatorAndDevSelector(worker)
        rec_service = FakeRecommendationService(_recommendation())
        service = AdaptiveExecutionSelector(
            store, rec_service, dev_selector, clock=lambda: UTC_NOW, id_factory=_counting_id_factory()
        )

        selection = _run(service.select(
            estimation_request=_estimation_request(tmp_path),
            required_capabilities=frozenset({"development"}),
        ))

        assert selection.worker.worker_id == "victor"
        assert selection.decision.profile_id == "deep"
        assert selection.decision.quality_tier is QualityTier.COMPLEX
        assert store.get(selection.decision.decision_id) == selection.decision

    def test_recommendation_id_is_conserved_in_decision(self, tmp_path: Path) -> None:
        store = AdaptiveExecutionDecisionStore(tmp_path / "decisions.sqlite3", clock=lambda: UTC_NOW)
        worker = _worker("alice", (_profile("deep", QualityTier.COMPLEX),))
        dev_selector = FakeEstimatorAndDevSelector(worker)
        rec_service = FakeRecommendationService(_recommendation(recommendation_id="rec-xyz"))
        service = AdaptiveExecutionSelector(store, rec_service, dev_selector, clock=lambda: UTC_NOW, id_factory=_counting_id_factory())

        selection = _run(service.select(estimation_request=_estimation_request(tmp_path), required_capabilities=frozenset({"development"})))

        assert selection.decision.recommendation_id == "rec-xyz"

    def test_execution_request_fields_match_decision(self, tmp_path: Path) -> None:
        store = AdaptiveExecutionDecisionStore(tmp_path / "decisions.sqlite3", clock=lambda: UTC_NOW)
        worker = _worker("victor", (_profile("deep", QualityTier.COMPLEX, model="gpt-5.6-terra", reasoning_effort="high"),), provider="openai", backend="codex")
        dev_selector = FakeEstimatorAndDevSelector(worker)
        rec_service = FakeRecommendationService(_recommendation(recommended_reasoning="high"))
        service = AdaptiveExecutionSelector(store, rec_service, dev_selector, clock=lambda: UTC_NOW, id_factory=_counting_id_factory())

        selection = _run(service.select(estimation_request=_estimation_request(tmp_path), required_capabilities=frozenset({"development"})))

        assert selection.decision.model == "gpt-5.6-terra"
        assert selection.decision.reasoning_effort == "high"

    def test_decision_readable_after_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "decisions.sqlite3"
        store = AdaptiveExecutionDecisionStore(db_path, clock=lambda: UTC_NOW)
        worker = _worker("alice", (_profile("deep", QualityTier.COMPLEX),))
        dev_selector = FakeEstimatorAndDevSelector(worker)
        rec_service = FakeRecommendationService(_recommendation())
        service = AdaptiveExecutionSelector(store, rec_service, dev_selector, clock=lambda: UTC_NOW, id_factory=_counting_id_factory())
        selection = _run(service.select(estimation_request=_estimation_request(tmp_path), required_capabilities=frozenset({"development"})))
        store.close()

        reopened = AdaptiveExecutionDecisionStore(db_path)
        assert reopened.get(selection.decision.decision_id) == selection.decision

    def test_multiple_decisions_for_same_work_item_are_all_kept(self, tmp_path: Path) -> None:
        store = AdaptiveExecutionDecisionStore(tmp_path / "decisions.sqlite3", clock=lambda: UTC_NOW)
        victor = _worker("victor", (_profile("deep", QualityTier.COMPLEX),), provider="openai", backend="codex")
        alice = _worker("alice", (_profile("deep", QualityTier.COMPLEX),))

        service1 = AdaptiveExecutionSelector(
            store, FakeRecommendationService(_recommendation(recommendation_id="rec-1")),
            FakeEstimatorAndDevSelector(victor), clock=lambda: UTC_NOW, id_factory=_counting_id_factory("d"),
        )
        first = _run(service1.select(
            estimation_request=_estimation_request(tmp_path, work_item_id="wi-1"),
            required_capabilities=frozenset({"development"}),
        ))

        service2 = AdaptiveExecutionSelector(
            store, FakeRecommendationService(_recommendation(recommendation_id="rec-2")),
            FakeEstimatorAndDevSelector(alice), clock=lambda: UTC_NOW, id_factory=_counting_id_factory("e"),
        )
        second = _run(service2.select(
            estimation_request=_estimation_request(tmp_path, work_item_id="wi-1"),
            required_capabilities=frozenset({"development"}),
        ))

        decisions = store.list_for_work_item("wi-1")
        assert {d.decision_id for d in decisions} == {first.decision.decision_id, second.decision.decision_id}
        assert first.decision.worker_id == "victor"
        assert second.decision.worker_id == "alice"

    def test_estimator_can_differ_from_developer(self, tmp_path: Path) -> None:
        store = AdaptiveExecutionDecisionStore(tmp_path / "decisions.sqlite3", clock=lambda: UTC_NOW)
        developer = _worker("victor", (_profile("deep", QualityTier.COMPLEX),), provider="openai", backend="codex")
        dev_selector = FakeEstimatorAndDevSelector(developer)
        # The recommendation says the *estimator* was alice/economy — the
        # developer selected is victor, a totally different worker.
        rec_service = FakeRecommendationService(_recommendation(estimator_worker_id="alice", estimator_profile_id="economy"))
        service = AdaptiveExecutionSelector(store, rec_service, dev_selector, clock=lambda: UTC_NOW, id_factory=_counting_id_factory())

        selection = _run(service.select(estimation_request=_estimation_request(tmp_path), required_capabilities=frozenset({"development"})))

        assert selection.decision.worker_id == "victor"
        assert selection.decision.recommendation_id  # traceable back to alice's estimation, never conflated
