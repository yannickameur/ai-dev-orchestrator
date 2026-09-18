"""Tests for complexity pre-flight + persistent execution recommendations
(Phase 1 / Slice 16).

All tests are offline: fake WorkerSelector/RalphExecutionEngine stand in
for the real ones — no subprocess, no network, no real
Claude/Codex/Ralph invocation anywhere in this file. No complexity
estimation, quota, or reset credit is ever really consumed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.complexity_estimation import (
    DEFAULT_ESTIMATOR_CAPABILITY,
    ESTIMATOR_ROLE,
    PROFILE_RECOMMENDATION_FAILED_TOPIC,
    PROFILE_RECOMMENDED_TOPIC,
    ComplexityEstimationRequest,
    EstimatorProfileNotConfiguredError,
    ExecutionRecommendation,
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
    InvalidRecommendationPayloadError,
    NoReliableRecommendationError,
    ReviewFinding,
    compute_task_fingerprint,
)
from orchestrator import complexity_estimation as complexity_estimation_module
from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.handoff import HandoffRecord
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent, RalphLaunchError
from orchestrator.worker_selector import (
    ExecutionProfile,
    NoEligibleWorkerError,
    QualityTier,
    Worker,
    WorkerSelectionRequest,
)

UTC_NOW = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)


def _counting_id_factory(prefix: str = "id"):
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return id_factory


def _alice(**overrides) -> Worker:
    fields_ = dict(
        worker_id="alice", display_name="Alice", provider="anthropic", backend="claude_code",
        capabilities=frozenset({"complexity_estimation", "development"}),
        profiles=(
            ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="haiku"),
            ExecutionProfile(profile_id="standard", quality_tier=QualityTier.STANDARD, model="sonnet"),
        ),
        default_profile_id="standard",
        estimator_profile_id="economy",
    )
    fields_.update(overrides)
    return Worker(**fields_)


def _request(tmp_path: Path, **overrides) -> ComplexityEstimationRequest:
    fields_ = dict(
        project_id="proj-1", role="developer", workspace=tmp_path,
        objective="Implement the thing", acceptance_criteria=("tests pass",),
    )
    fields_.update(overrides)
    return ComplexityEstimationRequest(**fields_)


def _handoff(**overrides) -> HandoffRecord:
    fields_ = dict(
        handoff_id="handoff-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1",
        created_at=UTC_NOW, objective="Implement the thing", completed_work="did the first half",
    )
    fields_.update(overrides)
    return HandoffRecord(**fields_)


def _finding(**overrides) -> ReviewFinding:
    fields_ = dict(finding_id="f-1", summary="off by one", severity="major")
    fields_.update(overrides)
    return ReviewFinding(**fields_)


class FakeEstimatorSelector:
    """Picks the first candidate whose capabilities satisfy the request."""

    def __init__(self, workers: list[Worker]) -> None:
        self._workers = list(workers)
        self.requests: list[WorkerSelectionRequest] = []

    async def select(self, request: WorkerSelectionRequest) -> Worker:
        self.requests.append(request)
        candidates = [w for w in self._workers if request.required_capabilities <= w.capabilities]
        if not candidates:
            raise NoEligibleWorkerError(request)
        return candidates[0]


class FakeEstimationExecutionEngine:
    """Returns a scripted ExecutionResult (or raises) for every call."""

    def __init__(self, result: ExecutionResult | None = None, raise_exc: Exception | None = None) -> None:
        self._result = result
        self._raise_exc = raise_exc
        self.requests: list = []

    async def execute(self, request):
        self.requests.append(request)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


def _exec_result(
    *, worker: Worker, execution_id: str = "exec-1", status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    topic: str = PROFILE_RECOMMENDED_TOPIC, payload: str | None = None, exit_code: int | None = None,
) -> ExecutionResult:
    profile = worker.profile(worker.estimator_profile_id)
    record = ExecutionRecord(
        execution_id=execution_id, task_id="estimation:x", worker_id=worker.worker_id,
        provider=worker.provider, backend=worker.backend, model=profile.model,
        reasoning_effort=profile.reasoning_effort, role=ESTIMATOR_ROLE, started_at=UTC_NOW, status=status,
    )
    events = (RalphEvent(topic=topic, timestamp=UTC_NOW, payload=payload),) if payload is not None else ()
    return ExecutionResult(
        record=record, events=events,
        exit_code=(exit_code if exit_code is not None else (0 if status is ExecutionStatus.SUCCEEDED else 1)),
    )


def _valid_payload(tier: str = "COMPLEX", reasoning: str | None = "high", reasons=("cross-module change",)) -> str:
    return json.dumps({
        "minimum_quality_tier": tier, "recommended_reasoning": reasoning, "reasons": list(reasons),
    })


def _service(store, selector, engine, **kwargs) -> ExecutionRecommendationService:
    return ExecutionRecommendationService(
        store, selector, engine, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(), **kwargs
    )


def _run(coro):
    return asyncio.run(coro)


class TestEstimatorSelectionAndProfile:
    def test_estimator_selected_with_capability(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert selector.requests[0].required_capabilities == frozenset({DEFAULT_ESTIMATOR_CAPABILITY})

    def test_estimator_profile_id_is_used_not_default(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()  # estimator_profile_id=economy (haiku), default_profile_id=standard (sonnet)
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.estimator_profile_id == "economy"
        assert engine.requests[0].model == "haiku"

    def test_model_transmitted_to_ralph_execution_engine(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert engine.requests[0].model == "haiku"

    def test_reasoning_effort_transmitted_to_ralph_execution_engine(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        victor = _alice(
            worker_id="victor", provider="openai", backend="codex",
            profiles=(ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="gpt-5.6-terra", reasoning_effort="low"),),
            default_profile_id="economy", estimator_profile_id="economy",
        )
        selector = FakeEstimatorSelector([victor])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=victor, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert engine.requests[0].reasoning_effort == "low"

    def test_role_dedicated_to_estimation_used_for_execution_request(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert engine.requests[0].role == ESTIMATOR_ROLE

    def test_success_and_failure_topics_are_the_documented_contract(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert engine.requests[0].success_topics == frozenset({PROFILE_RECOMMENDED_TOPIC})
        assert engine.requests[0].failure_topics == frozenset({PROFILE_RECOMMENDATION_FAILED_TOPIC})


class TestPromptContent:
    def test_prompt_forbids_modification(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        instructions = engine.requests[0].instructions
        assert "never modify project files" in instructions
        assert "never commit" in instructions
        assert "never push" in instructions

    def test_prompt_includes_role(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path, role="code_review")))

        assert "code_review" in engine.requests[0].instructions

    def test_prompt_includes_objective(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path, objective="Refactor the billing module")))

        assert "Refactor the billing module" in engine.requests[0].instructions

    def test_prompt_includes_acceptance_criteria(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path, acceptance_criteria=("must not break billing",))))

        assert "must not break billing" in engine.requests[0].instructions

    def test_prompt_includes_relevant_handoff(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        handoff = _handoff(completed_work="wired the first endpoint")

        _run(service.estimate(_request(tmp_path, latest_handoff=handoff)))

        assert "wired the first endpoint" in engine.requests[0].instructions

    def test_prompt_includes_review_findings(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        finding = _finding(summary="race condition in the writer")

        _run(service.estimate(_request(tmp_path, review_findings=(finding,))))

        assert "race condition in the writer" in engine.requests[0].instructions

    def test_prompt_never_asks_for_a_model_name(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert "never name a specific model" in engine.requests[0].instructions


class TestPayloadParsing:
    @pytest.mark.parametrize("tier", ["SIMPLE", "STANDARD", "COMPLEX", "CRITICAL"])
    def test_quality_tier_is_parsed(self, tmp_path: Path, tier: str) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload(tier=tier)))
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.minimum_quality_tier is QualityTier[tier]

    def test_recommended_reasoning_none_is_supported(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        payload = _valid_payload(reasoning=None)
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=payload))
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.recommended_reasoning is None

    def test_recommended_reasoning_value_is_supported(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, payload=_valid_payload(reasoning="xhigh"))
        )
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.recommended_reasoning == "xhigh"

    def test_reasons_are_persisted(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, payload=_valid_payload(reasons=("a", "b")))
        )
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.reasons == ("a", "b")

    def test_missing_tier_is_rejected(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        payload = json.dumps({"recommended_reasoning": "high", "reasons": ["x"]})
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=payload))
        service = _service(store, selector, engine)

        with pytest.raises(InvalidRecommendationPayloadError):
            _run(service.estimate(_request(tmp_path)))

    def test_unknown_tier_is_rejected(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, payload=_valid_payload(tier="ULTRA"))
        )
        service = _service(store, selector, engine)

        with pytest.raises(InvalidRecommendationPayloadError):
            _run(service.estimate(_request(tmp_path)))

    def test_invalid_json_is_rejected(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload="{not json"))
        service = _service(store, selector, engine)

        with pytest.raises(InvalidRecommendationPayloadError):
            _run(service.estimate(_request(tmp_path)))

    def test_payload_never_names_a_model_or_worker(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        # Even if the estimator misbehaves and includes model/provider/worker_id,
        # the parsed recommendation must never carry them.
        payload = json.dumps({
            "minimum_quality_tier": "STANDARD", "reasons": ["x"],
            "model": "some-model", "provider": "some-provider", "worker_id": "some-worker",
        })
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=payload))
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert not hasattr(recommendation, "model")
        assert not hasattr(recommendation, "provider")


class TestPersistence:
    def test_recommendation_is_persisted(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert store.get(recommendation.recommendation_id) == recommendation

    def test_recommendation_readable_after_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rec.sqlite3"
        store = ExecutionRecommendationStore(db_path, clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        recommendation = _run(service.estimate(_request(tmp_path)))
        store.close()

        reopened = ExecutionRecommendationStore(db_path)
        assert reopened.get(recommendation.recommendation_id) == recommendation


class TestFingerprint:
    def test_deterministic_across_calls(self, tmp_path: Path) -> None:
        r = _request(tmp_path)
        assert compute_task_fingerprint(r) == compute_task_fingerprint(r)

    def test_same_input_same_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(_request(tmp_path)) == compute_task_fingerprint(_request(tmp_path))

    def test_role_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(_request(tmp_path, role="developer")) != compute_task_fingerprint(
            _request(tmp_path, role="code_review")
        )

    def test_objective_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(_request(tmp_path, objective="A")) != compute_task_fingerprint(
            _request(tmp_path, objective="B")
        )

    def test_acceptance_criteria_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(_request(tmp_path, acceptance_criteria=("a",))) != compute_task_fingerprint(
            _request(tmp_path, acceptance_criteria=("b",))
        )

    def test_handoff_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(
            _request(tmp_path, latest_handoff=_handoff(completed_work="v1"))
        ) != compute_task_fingerprint(_request(tmp_path, latest_handoff=_handoff(completed_work="v2")))

    def test_git_sha_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(_request(tmp_path, git_sha="aaa")) != compute_task_fingerprint(
            _request(tmp_path, git_sha="bbb")
        )

    def test_review_findings_change_changes_fingerprint(self, tmp_path: Path) -> None:
        assert compute_task_fingerprint(
            _request(tmp_path, review_findings=(_finding(summary="x"),))
        ) != compute_task_fingerprint(_request(tmp_path, review_findings=(_finding(summary="y"),)))


class TestCache:
    def test_cache_hit_makes_no_new_ralph_call(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        request = _request(tmp_path)

        first = _run(service.estimate(request))
        second = _run(service.estimate(request))

        assert first.recommendation_id == second.recommendation_id
        assert len(engine.requests) == 1

    def test_force_refresh_triggers_a_new_ralph_call(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        request = _request(tmp_path)

        _run(service.estimate(request))
        _run(service.estimate(request, force_refresh=True))

        assert len(engine.requests) == 2

    def test_force_refresh_keeps_the_previous_recommendation(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)
        request = _request(tmp_path)

        first = _run(service.estimate(request))
        second = _run(service.estimate(request, force_refresh=True))

        assert first.recommendation_id != second.recommendation_id
        assert store.get(first.recommendation_id) == first  # never overwritten


class TestFailClosed:
    def test_no_terminal_event_yields_no_recommendation(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, status=ExecutionStatus.FAILED, payload=None)
        )
        service = _service(store, selector, engine)

        with pytest.raises(NoReliableRecommendationError):
            _run(service.estimate(_request(tmp_path)))

    def test_exit_code_zero_without_event_is_fail_closed(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        # exit_code 0 but the record itself is FAILED (no reliable business
        # event was found) — RalphExecutionEngine's own contract, reused here.
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, status=ExecutionStatus.FAILED, exit_code=0, payload=None)
        )
        service = _service(store, selector, engine)

        with pytest.raises(NoReliableRecommendationError):
            _run(service.estimate(_request(tmp_path)))

    def test_nonzero_exit_code_with_valid_event_still_succeeds(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(
            result=_exec_result(worker=alice, status=ExecutionStatus.SUCCEEDED, exit_code=2, payload=_valid_payload())
        )
        service = _service(store, selector, engine)

        recommendation = _run(service.estimate(_request(tmp_path)))

        assert recommendation.minimum_quality_tier is QualityTier.COMPLEX

    def test_no_estimator_available_is_fail_closed(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeEstimatorSelector([])  # no worker declares complexity_estimation
        engine = FakeEstimationExecutionEngine()
        service = _service(store, selector, engine)

        with pytest.raises(NoEligibleWorkerError):
            _run(service.estimate(_request(tmp_path)))
        assert engine.requests == []

    def test_estimator_profile_not_configured_is_fail_closed(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice(estimator_profile_id=None)
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine()
        service = _service(store, selector, engine)

        with pytest.raises(EstimatorProfileNotConfiguredError):
            _run(service.estimate(_request(tmp_path)))
        assert engine.requests == []

    def test_ralph_launch_error_yields_no_recommendation(self, tmp_path: Path) -> None:
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(raise_exc=RalphLaunchError("boom"))
        service = _service(store, selector, engine)

        with pytest.raises(NoReliableRecommendationError):
            _run(service.estimate(_request(tmp_path)))


class TestNoFinalSelectionOrHardcoding:
    def test_no_provider_specific_branch_in_payload_or_prompt_logic(self) -> None:
        # The module docstring may mention Claude/Codex/Ralph as prose for
        # human readers (every other module in this codebase does the
        # same — planning.py, ralph_execution_engine.py, mvp_manager.py).
        # What must never happen is a concrete model/provider *string*
        # baked into the actual payload/prompt-building logic.
        source = "".join(
            inspect.getsource(obj)
            for obj in (
                complexity_estimation_module._build_estimator_instructions,
                complexity_estimation_module._parse_recommendation_payload,
                complexity_estimation_module.compute_task_fingerprint,
            )
        )
        for forbidden in ("sonnet", "haiku", "gpt-5", "anthropic", "openai"):
            assert forbidden not in source

    def test_module_never_uses_subprocess_directly(self) -> None:
        source = inspect.getsource(complexity_estimation_module)
        assert "import subprocess" not in source

    def test_recommendation_has_no_model_or_provider_field(self) -> None:
        field_names = {f.name for f in fields(ExecutionRecommendation)}
        assert "model" not in field_names
        assert "provider" not in field_names
        assert "worker_id" not in field_names  # only estimator_worker_id, never a "chosen" worker_id

    def test_service_never_touches_a_second_worker_selection_for_development(self, tmp_path: Path) -> None:
        # A single WorkerSelector.select call — never one for the estimator
        # and a second for a "final" development/review worker.
        store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        alice = _alice()
        selector = FakeEstimatorSelector([alice])
        engine = FakeEstimationExecutionEngine(result=_exec_result(worker=alice, payload=_valid_payload()))
        service = _service(store, selector, engine)

        _run(service.estimate(_request(tmp_path)))

        assert len(selector.requests) == 1
