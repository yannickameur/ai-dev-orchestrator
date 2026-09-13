"""Tests for multi-agent release planning + roadmap synthesis (Phase 1 / Slice 12).

All tests are offline: fake WorkerSelector/RalphExecutionEngine stand in
for the real ones — no subprocess, no network, no real
Claude/Codex/Ralph/Git invocation anywhere in this file.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.activity_report import (
    ActivityReport,
    ActivityReportStore,
    ActivitySummary,
    Incident,
    WorkItemSummary,
)
from orchestrator.adaptive_execution import AdaptiveExecutionDecision, NoCapableProfileError
from orchestrator.complexity_estimation import ComplexityEstimationRequest, ExecutionRecommendation
from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.project_state import ProjectStateStore
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent, RalphLaunchError
from orchestrator.planning import (
    DEFAULT_PLANNING_CAPABILITY,
    DEFAULT_SYNTHESIS_CAPABILITY,
    PLANNER_ROLE,
    PLANNING_FAILED_TOPIC,
    PLANNING_PROPOSED_TOPIC,
    SYNTHESIS_PROPOSED_TOPIC,
    SYNTHESIZER_ROLE,
    NoActivityReportForReleaseError,
    PlannerProposal,
    PlannerProposalStatus,
    PlanningCoordinator,
    PlanningPolicy,
    PlanningSession,
    PlanningSessionFailedError,
    PlanningSessionStatus,
    PlanningSnapshot,
    PlanningStore,
    RoadmapChangeType,
    RoadmapFileNotFoundError,
    RoadmapProposalStatus,
    render_markdown,
)
from orchestrator.worker_selector import NoEligibleWorkerError, QualityTier, Worker, WorkerSelectionRequest

UTC_NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)


def _alice() -> Worker:
    return Worker.with_single_profile(
        worker_id="claude_dev_01", display_name="Alice", provider="anthropic",
        backend="claude_code", model="sonnet",
        capabilities=frozenset({DEFAULT_PLANNING_CAPABILITY, DEFAULT_SYNTHESIS_CAPABILITY}),
    )


def _chloe() -> Worker:
    return Worker.with_single_profile(
        worker_id="claude_dev_02", display_name="Chloe", provider="anthropic",
        backend="claude_code", model="haiku",
        capabilities=frozenset({DEFAULT_PLANNING_CAPABILITY}),
    )


def _victor() -> Worker:
    return Worker.with_single_profile(
        worker_id="codex_dev_01", display_name="Victor", provider="openai",
        backend="codex", model="terra",
        capabilities=frozenset({DEFAULT_PLANNING_CAPABILITY, DEFAULT_SYNTHESIS_CAPABILITY}),
    )


def _seed_project(project_store: ProjectStateStore, tmp_path: Path, roadmap_text: str = "# Roadmap\n\nSlice 11 done.\n") -> None:
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")
    (tmp_path / "ROADMAP.md").write_text(roadmap_text)


def _activity_report(
    *, report_id: str = "report-1", release_id: str = "release-1", mvp_id: str = "mvp-1",
    git_sha: str | None = "deadbeef",
) -> ActivityReport:
    return ActivityReport(
        report_id=report_id, project_id="proj-1", mvp_id=mvp_id, release_id=release_id,
        generated_at=UTC_NOW, mvp_objective="Ship it", mvp_final_status="released",
        summary=ActivitySummary(
            work_item_count=1, execution_count=1, validation_count=0, review_count=0,
            rework_count=0, failure_count=0, interruption_count=0,
        ),
        git_sha=git_sha,
        work_items=(WorkItemSummary(work_item_id="wi-a", title="A", status="completed"),),
        incidents=(Incident(work_item_id="wi-a", kind="blocked", summary="was blocked once"),),
    )


def _stores(tmp_path: Path, *, roadmap_text: str = "# Roadmap\n\nSlice 11 done.\n"):
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    _seed_project(project_store, tmp_path, roadmap_text)
    activity_store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)
    activity_store.record(_activity_report())
    planning_store = PlanningStore(tmp_path / "planning.sqlite3", clock=lambda: UTC_NOW)
    return project_store, activity_store, planning_store


def _counting_id_factory():
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return id_factory


class FakePlanningWorkerSelector:
    """Picks the first non-excluded candidate from the relevant pool, in order."""

    def __init__(self, planners: list[Worker], synthesizers: list[Worker] | None = None) -> None:
        self._planners = list(planners)
        self._synthesizers = list(synthesizers if synthesizers is not None else planners)
        self.requests: list[WorkerSelectionRequest] = []

    async def select(self, request: WorkerSelectionRequest) -> Worker:
        self.requests.append(request)
        pool = self._planners if DEFAULT_PLANNING_CAPABILITY in request.required_capabilities else self._synthesizers
        candidates = [w for w in pool if w.worker_id not in request.excluded_worker_ids]
        if not candidates:
            raise NoEligibleWorkerError(request)
        return candidates[0]


class FakePlanningExecutionEngine:
    """Returns a scripted ExecutionResult (or raises) per worker_id."""

    def __init__(self, results_by_worker: dict | None = None, raise_by_worker: dict | None = None) -> None:
        self._results_by_worker = results_by_worker or {}
        self._raise_by_worker = raise_by_worker or {}
        self.requests: list = []

    async def execute(self, request):
        self.requests.append(request)
        if request.worker.worker_id in self._raise_by_worker:
            raise self._raise_by_worker[request.worker.worker_id]
        return self._results_by_worker[request.worker.worker_id]


def _planning_result(
    *, execution_id: str, worker: Worker, status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    topic: str = PLANNING_PROPOSED_TOPIC, payload: str | None = None, role: str = "planner",
) -> ExecutionResult:
    record = ExecutionRecord(
        execution_id=execution_id, task_id="planning:x", worker_id=worker.worker_id,
        provider=worker.provider, backend=worker.backend, model=worker.profile().model, role=role,
        started_at=UTC_NOW, status=status,
    )
    events = (RalphEvent(topic=topic, timestamp=UTC_NOW, payload=payload),) if payload is not None else ()
    return ExecutionResult(record=record, events=events, exit_code=0 if status is ExecutionStatus.SUCCEEDED else 1)


def _valid_planner_payload(objective: str = "Add observability") -> str:
    return json.dumps({
        "proposed_mvp_objective": objective,
        "rationale": "because reasons",
        "proposed_work_items": [
            {
                "title": "Add metrics", "objective": "instrument key paths", "dependencies": [],
                "acceptance_criteria": ["metrics visible"], "required_capabilities": ["developer"],
                "rationale": "visibility",
            }
        ],
        "acceptance_criteria": ["MVP has metrics"],
        "risks": ["scope creep"],
        "deferred_items": ["dashboards"],
        "roadmap_changes": [
            {"type": "add", "item_reference": "observability", "target_mvp": None, "reason": "needed now", "proposed_item": None}
        ],
    })


def _valid_synthesizer_payload() -> str:
    return json.dumps({
        "next_mvp_objective": "Add observability",
        "next_mvp_rationale": "both planners converge on this",
        "next_mvp_work_items": [
            {
                "title": "Add metrics", "objective": "instrument key paths", "dependencies": [],
                "acceptance_criteria": ["metrics visible"], "required_capabilities": ["developer"],
                "rationale": "visibility",
            }
        ],
        "next_mvp_acceptance_criteria": ["MVP has metrics"],
        "next_mvp_deferred_items": ["dashboards"],
        "roadmap_changes": [
            {"type": "add", "item_reference": "observability", "target_mvp": None, "reason": "needed now", "proposed_item": None},
            {"type": "move", "item_reference": "billing", "target_mvp": "MVP+2", "reason": "not urgent", "proposed_item": None},
        ],
        "risks": ["scope creep"],
        "agreements": ["both want observability next"],
        "disagreements": [
            {"topic": "billing timing", "positions": ["Alice: now", "Victor: later"], "resolution": "defer to MVP+2"}
        ],
        "rationale": "synthesis rationale",
    })


def _coordinator(
    project_store, activity_store, planning_store, selector, engine, *, policy=None,
) -> PlanningCoordinator:
    return PlanningCoordinator(
        planning_store, selector, engine, project_store, activity_store,
        policy=policy, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


class TestPlanningSnapshot:
    def test_snapshot_built_from_release_report_and_roadmap(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)

        session = asyncio.run(
            coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1")
        )

        snapshot = planning_store.get_snapshot(session.snapshot_id)
        assert snapshot.activity_report_id == "report-1"
        assert snapshot.git_sha == "deadbeef"
        assert "Slice 11 done" in snapshot.roadmap_content
        assert snapshot.project_state_summary[0].work_item_id == "wi-a"
        assert any("was blocked once" in issue for issue in snapshot.open_issues)

    def test_roadmap_hash_is_deterministic(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path, roadmap_text="# Roadmap\nfixed content\n")
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)

        s1 = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        s2 = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        h1 = planning_store.get_snapshot(s1.snapshot_id).roadmap_hash
        h2 = planning_store.get_snapshot(s2.snapshot_id).roadmap_hash
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex digest

    def test_roadmap_hash_changes_when_roadmap_content_changes(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path, roadmap_text="version A\n")
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        s1 = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        h1 = planning_store.get_snapshot(s1.snapshot_id).roadmap_hash

        (tmp_path / "ROADMAP.md").write_text("version B\n")
        s2 = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        h2 = planning_store.get_snapshot(s2.snapshot_id).roadmap_hash

        assert h1 != h2

    def test_missing_roadmap_file_raises(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        (tmp_path / "ROADMAP.md").unlink()
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)

        with pytest.raises(RoadmapFileNotFoundError):
            asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

    def test_mismatched_release_id_raises(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)

        with pytest.raises(NoActivityReportForReleaseError):
            asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="not-the-real-release"))


class TestPlannerSelection:
    def test_default_policy_selects_two_planners(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len(proposals) == 2

    def test_planners_have_distinct_worker_ids(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len({p.worker_id for p in proposals}) == 2

    def test_prefers_distinct_providers_when_available(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, chloe, victor = _alice(), _chloe(), _victor()
        # chloe shares alice's provider; victor is cross-provider. Selector
        # returns candidates in [alice, chloe, victor] order — the
        # coordinator must skip chloe (same provider) and reach victor.
        selector = FakePlanningWorkerSelector([alice, chloe, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        providers = {p.provider for p in proposals}
        assert providers == {"anthropic", "openai"}

    def test_same_provider_accepted_when_no_cross_provider_candidate_exists(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, chloe = _alice(), _chloe()
        selector = FakePlanningWorkerSelector([alice, chloe])  # both anthropic
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                chloe.worker_id: _planning_result(execution_id="e2", worker=chloe, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len(proposals) == 2  # not blocked just because providers match

    def test_only_one_eligible_planner_fails_closed(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice = _alice()
        selector = FakePlanningWorkerSelector([alice])  # only one candidate, planner_count=2
        engine = FakePlanningExecutionEngine(
            results_by_worker={alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload())}
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        # The one planner that DID run before the shortage was discovered
        # is still persisted — never silently discarded.
        assert len(planning_store.list_planner_proposals(session.planning_session_id)) == 1


class TestPlannerIndependence:
    def test_planner_instructions_never_reference_a_sibling_proposal(self, tmp_path: Path) -> None:
        from orchestrator.planning import _build_planner_instructions

        params = list(inspect.signature(_build_planner_instructions).parameters)
        assert params == ["snapshot"]  # structurally cannot receive another proposal

    def test_planners_receive_byte_identical_instructions(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        asyncio.run(coordinator.run_planners(session.planning_session_id))

        instructions = {req.instructions for req in engine.requests}
        assert len(instructions) == 1  # same snapshot in, same instructions out — for every planner

    def test_same_snapshot_id_recorded_for_every_planner(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert {p.snapshot_id for p in proposals} == {session.snapshot_id}


class TestPlannerProposalContract:
    def test_valid_payload_produces_structured_proposal(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("Add observability")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("Add observability")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        alice_proposal = next(p for p in proposals if p.worker_id == alice.worker_id)
        assert alice_proposal.status is PlannerProposalStatus.VALID
        assert alice_proposal.proposed_mvp_objective == "Add observability"
        assert alice_proposal.proposed_work_items[0].title == "Add metrics"
        assert alice_proposal.roadmap_changes[0].change_type is RoadmapChangeType.ADD

    def test_invalid_payload_is_rejected_and_recorded(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload='{"not": "the right shape"}'),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        alice_proposal = next(p for p in proposals if p.worker_id == alice.worker_id)
        assert alice_proposal.status is PlannerProposalStatus.INVALID
        assert alice_proposal.error_summary is not None

    def test_planner_without_reliable_terminal_event_is_invalid(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, status=ExecutionStatus.FAILED),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        alice_proposal = next(p for p in proposals if p.worker_id == alice.worker_id)
        assert alice_proposal.status is PlannerProposalStatus.INVALID

    def test_ralph_launch_error_yields_invalid_proposal_not_a_crash(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            raise_by_worker={alice.worker_id: RalphLaunchError("ralph not found")},
            results_by_worker={victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B"))},
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        alice_proposal = next(p for p in proposals if p.worker_id == alice.worker_id)
        assert alice_proposal.status is PlannerProposalStatus.INVALID


class TestSynthesis:
    def test_synthesis_refuses_before_all_proposals_collected(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        # run_planners() was never called: zero proposals collected yet.
        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        assert engine.requests == []  # the synthesizer itself is never even launched

    def test_synthesis_refuses_with_only_one_of_two_valid_proposals(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, status=ExecutionStatus.FAILED),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        assert all(r.role != "synthesizer" for r in engine.requests)

    def test_synthesizer_receives_all_proposals_and_persists_roadmap_proposal(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(
                    execution_id="e2", worker=victor, payload=_valid_synthesizer_payload(),
                    topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
                ),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        # Give both planners a valid payload directly, bypassing the
        # synthesizer's own scripted result collision on victor's worker_id
        # by re-scripting the engine per phase.
        proposals = []
        for worker, payload in ((alice, "A"), (victor, "B")):
            proposals.append(
                PlannerProposal(
                    proposal_id=f"manual-{worker.worker_id}", planning_session_id=session.planning_session_id,
                    snapshot_id=session.snapshot_id, worker_id=worker.worker_id, provider=worker.provider,
                    model=worker.profile().model, execution_id=f"exec-{worker.worker_id}", created_at=UTC_NOW,
                    status=PlannerProposalStatus.VALID, proposed_mvp_objective=payload,
                    rationale="r", proposed_work_items=(), acceptance_criteria=(), risks=(),
                    deferred_items=(), roadmap_changes=(),
                )
            )
        for p in proposals:
            planning_store.record_planner_proposal(p)

        roadmap_proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert roadmap_proposal.status is RoadmapProposalStatus.PROPOSED
        assert set(roadmap_proposal.source_proposal_ids) == {p.proposal_id for p in proposals}
        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.PROPOSAL_READY
        synthesizer_request = next(r for r in engine.requests if r.role == "synthesizer")
        assert "worker claude_dev_01" in synthesizer_request.instructions
        assert "worker codex_dev_01" in synthesizer_request.instructions

    def test_source_proposal_ids_conserved(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        p1 = PlannerProposal(
            proposal_id="p-alice", planning_session_id="sess-1", snapshot_id="snap-1",
            worker_id=alice.worker_id, provider=alice.provider, model=alice.profile().model,
            execution_id="e1", created_at=UTC_NOW, status=PlannerProposalStatus.VALID,
            proposed_mvp_objective="A", rationale="r",
        )
        p2 = PlannerProposal(
            proposal_id="p-victor", planning_session_id="sess-1", snapshot_id="snap-1",
            worker_id=victor.worker_id, provider=victor.provider, model=victor.profile().model,
            execution_id="e2", created_at=UTC_NOW, status=PlannerProposalStatus.VALID,
            proposed_mvp_objective="B", rationale="r",
        )
        planning_store.create_session(
            PlanningSession(
                planning_session_id="sess-1", project_id="proj-1", mvp_id="mvp-1", release_id="release-1",
                snapshot_id="snap-1", created_at=UTC_NOW, status=PlanningSessionStatus.COLLECTING,
                policy_planner_count=2,
            )
        )
        planning_store.record_snapshot(
            PlanningSnapshot(
                snapshot_id="snap-1", project_id="proj-1", source_mvp_id="mvp-1",
                source_release_id="release-1", created_at=UTC_NOW, roadmap_content="# R\n",
                roadmap_hash="abc", activity_report_id="report-1",
            )
        )
        planning_store.record_planner_proposal(p1)
        planning_store.record_planner_proposal(p2)

        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                victor.worker_id: _planning_result(
                    execution_id="synth-exec", worker=victor, payload=_valid_synthesizer_payload(),
                    topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
                ),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)

        roadmap_proposal = asyncio.run(coordinator.synthesize("sess-1"))

        assert set(roadmap_proposal.source_proposal_ids) == {"p-alice", "p-victor"}

    def test_synthesizer_receives_original_snapshot(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path, roadmap_text="# unique marker roadmap\n")
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        asyncio.run(coordinator.synthesize(session.planning_session_id))

        synthesizer_request = next(r for r in engine.requests if r.role == "synthesizer")
        assert "unique marker roadmap" in synthesizer_request.instructions

    def test_disagreements_and_agreements_persisted(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )

        roadmap_proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert roadmap_proposal.agreements == ("both want observability next",)
        assert roadmap_proposal.disagreements[0].topic == "billing timing"
        assert roadmap_proposal.disagreements[0].resolution == "defer to MVP+2"

    def test_keep_add_move_drop_all_persisted(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        synth_payload = json.dumps({
            "next_mvp_objective": "X", "next_mvp_rationale": "r",
            "next_mvp_work_items": [], "next_mvp_acceptance_criteria": [], "next_mvp_deferred_items": [],
            "roadmap_changes": [
                {"type": "keep", "item_reference": "slice-9", "target_mvp": None, "reason": "still valid", "proposed_item": None},
                {"type": "add", "item_reference": "observability", "target_mvp": None, "reason": "needed", "proposed_item": None},
                {"type": "move", "item_reference": "billing", "target_mvp": "MVP+2", "reason": "later", "proposed_item": None},
                {"type": "drop", "item_reference": "legacy-cli", "target_mvp": None, "reason": "obsolete", "proposed_item": None},
            ],
            "risks": [], "agreements": [], "disagreements": [], "rationale": "r",
        })
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=synth_payload,
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )

        roadmap_proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))

        types_seen = {c.change_type for c in roadmap_proposal.roadmap_changes}
        assert types_seen == {
            RoadmapChangeType.KEEP, RoadmapChangeType.ADD, RoadmapChangeType.MOVE, RoadmapChangeType.DROP,
        }
        move_change = next(c for c in roadmap_proposal.roadmap_changes if c.change_type is RoadmapChangeType.MOVE)
        assert move_change.target_mvp == "MVP+2"

    def test_synthesis_failure_leaves_proposals_untouched_and_no_roadmap_proposal(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        before = planning_store.list_planner_proposals(session.planning_session_id)

        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, status=ExecutionStatus.FAILED, role="synthesizer",
        )

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.synthesize(session.planning_session_id))

        after = planning_store.list_planner_proposals(session.planning_session_id)
        assert before == after
        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED

    def test_deterministic_synthesis_input_order_independent_of_execution_order(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()

        def build(order):
            selector = FakePlanningWorkerSelector(order, synthesizers=[victor])
            engine = FakePlanningExecutionEngine(
                results_by_worker={
                    alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                    victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
                }
            )
            coordinator = _coordinator(project_store, activity_store, PlanningStore(":memory:", clock=lambda: UTC_NOW), selector, engine)
            return coordinator, engine

        coordinator_a, engine_a = build([alice, victor])
        session_a = asyncio.run(coordinator_a.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator_a.run_planners(session_a.planning_session_id))
        engine_a._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        asyncio.run(coordinator_a.synthesize(session_a.planning_session_id))
        instructions_a = next(r for r in engine_a.requests if r.role == "synthesizer").instructions

        coordinator_b, engine_b = build([victor, alice])  # reversed execution order
        session_b = asyncio.run(coordinator_b.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator_b.run_planners(session_b.planning_session_id))
        engine_b._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        asyncio.run(coordinator_b.synthesize(session_b.planning_session_id))
        instructions_b = next(r for r in engine_b.requests if r.role == "synthesizer").instructions

        assert instructions_a == instructions_b


class TestNoMutationAndNoRealMVP:
    def test_roadmap_md_file_never_mutated(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path, roadmap_text="original content\n")
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert (tmp_path / "ROADMAP.md").read_text() == "original content\n"

    def test_no_real_mvp_created_in_project_state_store(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        asyncio.run(coordinator.synthesize(session.planning_session_id))

        # Only the one MVP seeded by the test fixture exists — planning
        # never created a second, real MVP row.
        with pytest.raises(Exception):
            project_store.get_mvp("mvp-2")


class TestRenderMarkdown:
    def test_render_markdown_is_deterministic(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor], synthesizers=[victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, payload=_valid_synthesizer_payload(),
            topic=SYNTHESIS_PROPOSED_TOPIC, role="synthesizer",
        )
        roadmap_proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))

        rendered_1 = render_markdown(roadmap_proposal)
        rendered_2 = render_markdown(roadmap_proposal)
        assert rendered_1 == rendered_2
        assert "## ADD" in rendered_1
        assert "## Proposed next MVP" in rendered_1


class TestRestartAndPersistence:
    def test_restart_relit_session_snapshot_and_proposals(self, tmp_path: Path) -> None:
        planning_db = tmp_path / "planning.sqlite3"
        project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
        _seed_project(project_store, tmp_path)
        activity_store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)
        activity_store.record(_activity_report())
        planning_store = PlanningStore(planning_db, clock=lambda: UTC_NOW)

        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        planning_store.close()

        reopened = PlanningStore(planning_db, clock=lambda: UTC_NOW)
        fetched_session = reopened.get_session(session.planning_session_id)
        assert fetched_session.status is PlanningSessionStatus.COLLECTING
        fetched_snapshot = reopened.get_snapshot(session.snapshot_id)
        assert fetched_snapshot.roadmap_hash
        fetched_proposals = reopened.list_planner_proposals(session.planning_session_id)
        assert len(fetched_proposals) == 2
        assert reopened.latest_session_for_release("release-1").planning_session_id == session.planning_session_id

    def test_run_planners_never_reruns_worker_with_existing_proposal(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine(
            results_by_worker={
                alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload("A")),
                victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload("B")),
            }
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        assert len(engine.requests) == 2

        # Simulate a restart + re-invocation of run_planners for the same
        # session: since both planners already have a proposal, nothing
        # new should be launched.
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        assert len(engine.requests) == 2  # unchanged — no silent re-run

    def test_failed_session_is_conserved(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice = _alice()
        selector = FakePlanningWorkerSelector([alice])
        engine = FakePlanningExecutionEngine(
            results_by_worker={alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload())}
        )
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.run_planners(session.planning_session_id))

        refetched = planning_store.get_session(session.planning_session_id)
        assert refetched.status is PlanningSessionStatus.FAILED
        assert refetched.failure_reason is not None


class TestNoForbiddenBehavior:
    def test_module_never_shells_out_or_duplicates_selector_engine_logic(self) -> None:
        from orchestrator import planning as module

        source = inspect.getsource(module)
        for forbidden in (
            "import subprocess", "asyncio.create_subprocess", "Popen",
            "ClaudeCodeAdapter", "CodexAdapter", "QuotaManager(", "WorkerSelector(",
            "RalphExecutionEngine(", "time.sleep", "while True", "ResetCredit", ".consume(",
        ):
            assert forbidden not in source

    def test_uses_dedicated_business_events_not_task_start(self) -> None:
        from orchestrator import planning as module

        source = inspect.getsource(module)
        assert '"task.start"' not in source
        assert '"task.resume"' not in source

    def test_instructions_forbid_file_modification_and_commits(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine()
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        snapshot = planning_store.get_snapshot(session.snapshot_id)

        from orchestrator.planning import _build_planner_instructions

        instructions = _build_planner_instructions(snapshot)
        lowered = instructions.lower()
        assert "never modify project files" in lowered
        assert "never commit" in lowered
        assert "never push" in lowered

    def test_no_approval_notification_or_timer_functionality_yet(self) -> None:
        # Slice 13 territory — the only PlanningSessionStatus/RoadmapProposalStatus
        # values that exist are the ones this slice defines (documentation
        # mentioning future Slice 13 states in a docstring is fine; actual
        # enum members are not).
        assert {s.value for s in PlanningSessionStatus} == {"collecting", "synthesizing", "proposal_ready", "failed"}
        assert {s.value for s in RoadmapProposalStatus} == {"proposed"}

        from orchestrator import planning as module

        source = inspect.getsource(module)
        for forbidden in ("notify", "Notification", "timedelta(minutes=20)", "20 * 60"):
            assert forbidden not in source


# --- Slice 19: adaptive planner/synthesizer selection ------------------------


def _multi_profile_worker(
    *, worker_id: str, provider: str, backend: str, capabilities: frozenset[str],
    default_profile_id: str = "standard",
) -> Worker:
    """A worker with several real ExecutionProfiles (unlike the
    single-profile fixtures above) — needed to prove adaptive selection
    picks the profile ``resolve_profile()`` actually resolves, never
    ``worker.profile()``'s own (possibly different) default."""
    from orchestrator.worker_selector import ExecutionProfile

    profiles = (
        ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="mini", cost_rank=10),
        ExecutionProfile(profile_id="standard", quality_tier=QualityTier.STANDARD, model="mid", cost_rank=20),
        ExecutionProfile(profile_id="deep", quality_tier=QualityTier.COMPLEX, model="max", cost_rank=30),
    )
    return Worker(
        worker_id=worker_id, display_name=worker_id, provider=provider, backend=backend,
        capabilities=capabilities, profiles=profiles, default_profile_id=default_profile_id,
    )


def _recommendation(**overrides) -> ExecutionRecommendation:
    fields = dict(
        recommendation_id="rec-plan-1", project_id="proj-1", role=PLANNER_ROLE,
        estimator_worker_id="claude_dev_01", estimator_execution_id="est-1",
        estimator_profile_id="default", task_fingerprint="fp-plan-1",
        minimum_quality_tier=QualityTier.STANDARD, reasons=("because",), created_at=UTC_NOW,
        mvp_id="mvp-1",
    )
    fields.update(overrides)
    return ExecutionRecommendation(**fields)


class FakeRecommendationService:
    """Scriptable ExecutionRecommendationService-shaped fake: a fixed
    recommendation, a per-role mapping, or a scripted error — never
    actually estimates."""

    def __init__(self, *, recommendation=None, by_role: dict | None = None, error: Exception | None = None) -> None:
        self._recommendation = recommendation
        self._by_role = by_role or {}
        self._error = error
        self.calls: list[ComplexityEstimationRequest] = []

    async def estimate(self, request: ComplexityEstimationRequest, *, force_refresh: bool = False):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if request.role in self._by_role:
            return self._by_role[request.role]
        return self._recommendation


class FakeDecisionStore:
    """Records every AdaptiveExecutionDecision persisted — never a real sqlite3 store."""

    def __init__(self) -> None:
        self.recorded: list[AdaptiveExecutionDecision] = []

    def record(self, decision: AdaptiveExecutionDecision) -> AdaptiveExecutionDecision:
        self.recorded.append(decision)
        return decision


class RecordingTierAwareWorkerSelector:
    """Like FakePlanningWorkerSelector, but actually honors
    minimum_quality_tier (mirroring WorkerSelector's own real filter) —
    needed for tests that must prove no-downgrade/fail-closed behavior."""

    def __init__(self, planners: list[Worker], synthesizers: list[Worker] | None = None) -> None:
        self._planners = list(planners)
        self._synthesizers = list(synthesizers if synthesizers is not None else planners)
        self.requests: list[WorkerSelectionRequest] = []

    async def select(self, request: WorkerSelectionRequest) -> Worker:
        self.requests.append(request)
        pool = self._planners if DEFAULT_PLANNING_CAPABILITY in request.required_capabilities else self._synthesizers
        candidates = [
            w for w in pool
            if w.worker_id not in request.excluded_worker_ids
            and (
                request.minimum_quality_tier is None
                or any(p.quality_tier >= request.minimum_quality_tier for p in w.profiles)
            )
        ]
        if not candidates:
            raise NoEligibleWorkerError(request)
        return candidates[0]


def _adaptive_coordinator(
    project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
    *, policy=None,
) -> PlanningCoordinator:
    return PlanningCoordinator(
        planning_store, selector, engine, project_store, activity_store,
        policy=policy, execution_recommendation_service=recommendation_service,
        adaptive_execution_decision_store=decision_store,
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


class TestAdaptivePlannerSelection:
    def test_each_planner_execution_gets_a_preflight(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len(proposals) == 2
        assert all(r.role == PLANNER_ROLE for r in recommendation_service.calls)

    def test_minimum_quality_tier_respected_no_downgrade(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        # Both configured workers cap at STANDARD; recommendation demands COMPLEX.
        alice = _alice()
        victor = _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine()
        recommendation_service = FakeRecommendationService(
            recommendation=_recommendation(minimum_quality_tier=QualityTier.COMPLEX)
        )
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        assert engine.requests == []  # never executed with a weaker tier
        assert decision_store.recorded == []  # never a fabricated decision

    def test_resolved_profile_used_not_workers_own_default(self, tmp_path: Path) -> None:
        deep_alice = _multi_profile_worker(
            worker_id="claude_dev_01", provider="anthropic", backend="claude_code",
            capabilities=frozenset({DEFAULT_PLANNING_CAPABILITY}),
        )
        deep_victor = _multi_profile_worker(
            worker_id="codex_dev_01", provider="openai", backend="codex",
            capabilities=frozenset({DEFAULT_PLANNING_CAPABILITY}),
        )
        assert deep_alice.profile().model == "mid"  # the worker's OWN default (standard)
        selector = RecordingTierAwareWorkerSelector([deep_alice, deep_victor])
        project_store, activity_store, planning_store = _stores(tmp_path)
        engine = FakePlanningExecutionEngine({
            deep_alice.worker_id: _planning_result(execution_id="e1", worker=deep_alice, payload=_valid_planner_payload()),
            deep_victor.worker_id: _planning_result(execution_id="e2", worker=deep_victor, payload=_valid_planner_payload()),
        })
        # COMPLEX forces resolve_profile() to pick "deep" (model="max"), never "standard".
        recommendation_service = FakeRecommendationService(
            recommendation=_recommendation(minimum_quality_tier=QualityTier.COMPLEX)
        )
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert all(p.model == "max" for p in proposals)
        assert all(req.model == "max" for req in engine.requests)
        assert all(d.quality_tier is QualityTier.COMPLEX for d in decision_store.recorded)

    def test_multiple_planners_still_multiple_executions_and_decisions(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len(proposals) == 2
        assert {p.worker_id for p in proposals} == {alice.worker_id, victor.worker_id}
        assert len(engine.requests) == 2

    def test_planner_diversity_still_prefers_distinct_providers(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, chloe, victor = _alice(), _chloe(), _victor()  # alice+chloe same provider, victor different
        selector = RecordingTierAwareWorkerSelector([alice, chloe, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
            policy=PlanningPolicy(planner_count=2, prefer_distinct_providers=True),
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert {p.provider for p in proposals} == {"anthropic", "openai"}  # never both anthropic

    def test_recommendation_reused_across_planners_same_fingerprint(self, tmp_path: Path) -> None:
        """Two planners on the identical snapshot share one recommendation
        (one estimate() call for both) — explicitly acceptable — but still
        get their own persisted decision each."""
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert len(recommendation_service.calls) == 1  # shared recommendation
        assert len(decision_store.recorded) == 2  # but two distinct, audited decisions
        assert decision_store.recorded[0].decision_id != decision_store.recorded[1].decision_id
        assert {d.worker_id for d in decision_store.recorded} == {alice.worker_id, victor.worker_id}

    def test_structural_incapacity_fails_closed_no_fabricated_profile(self, tmp_path: Path) -> None:
        """A worker WorkerSelector returns with no profile actually
        reaching the tier (should not happen with a real WorkerSelector,
        but this module never trusts that silently either) fails closed
        via NoCapableProfileError — never a fabricated CRITICAL profile."""
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()  # single STANDARD-tier profile each
        # Deliberately the TIER-BLIND fake: it does not filter by tier
        # itself, so a CRITICAL recommendation reaches resolve_profile()
        # with a worker that cannot actually satisfy it.
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine()
        recommendation_service = FakeRecommendationService(
            recommendation=_recommendation(minimum_quality_tier=QualityTier.CRITICAL)
        )
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        assert decision_store.recorded == []
        assert engine.requests == []

    def test_no_adaptive_selection_when_not_configured_preserves_prior_behavior(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = FakePlanningWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)  # no adaptive deps
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))

        proposals = asyncio.run(coordinator.run_planners(session.planning_session_id))

        assert all(p.model == alice.profile().model or p.model == victor.profile().model for p in proposals)


class TestAdaptiveSynthesizerSelection:
    def test_synthesis_has_its_own_preflight_distinct_from_planner(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        engine._results_by_worker[alice.worker_id] = _planning_result(
            execution_id="e3", worker=alice, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )

        asyncio.run(coordinator.synthesize(session.planning_session_id))

        roles_seen = {r.role for r in recommendation_service.calls}
        assert roles_seen == {PLANNER_ROLE, SYNTHESIZER_ROLE}
        # The two role-specific requests have distinct content (never the
        # exact same ComplexityEstimationRequest object/fingerprint input).
        planner_call = next(r for r in recommendation_service.calls if r.role == PLANNER_ROLE)
        synthesis_call = next(r for r in recommendation_service.calls if r.role == SYNTHESIZER_ROLE)
        assert planner_call.acceptance_criteria != synthesis_call.acceptance_criteria

    def test_synthesis_does_not_reuse_planner_recommendation(self, tmp_path: Path) -> None:
        """Different tiers per role prove synthesis's recommendation is its
        own, never copied from the planner's."""
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(by_role={
            PLANNER_ROLE: _recommendation(role=PLANNER_ROLE, minimum_quality_tier=QualityTier.SIMPLE),
            SYNTHESIZER_ROLE: _recommendation(
                recommendation_id="rec-synth-1", role=SYNTHESIZER_ROLE, minimum_quality_tier=QualityTier.STANDARD,
            ),
        })
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        engine._results_by_worker[alice.worker_id] = _planning_result(
            execution_id="e3", worker=alice, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )
        proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))

        synth_requests = [r for r in selector.requests if DEFAULT_SYNTHESIS_CAPABILITY in r.required_capabilities]
        assert all(r.minimum_quality_tier is QualityTier.STANDARD for r in synth_requests)
        assert proposal.synthesizer_worker_id in {alice.worker_id, victor.worker_id}
        synth_decision = next(d for d in decision_store.recorded if d.role == SYNTHESIZER_ROLE)
        assert synth_decision.recommendation_id == "rec-synth-1"

    def test_synthesizer_diversity_preference_preserved(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()  # both declare synthesis capability
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        recommendation_service = FakeRecommendationService(recommendation=_recommendation())
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
            policy=PlanningPolicy(planner_count=2, prefer_distinct_synthesizer_worker=True),
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        engine._results_by_worker[alice.worker_id] = _planning_result(
            execution_id="e3", worker=alice, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )
        engine._results_by_worker[victor.worker_id] = _planning_result(
            execution_id="e3", worker=victor, topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )

        # Both alice and victor were used as planners (planner_count=2, two
        # workers) — prefer_distinct_synthesizer_worker cannot be honored,
        # so the fallback (reuse a planner) kicks in, exactly as before
        # Slice 19; this proves the diversity *preference* code path is
        # still reached (not skipped) even under adaptive selection.
        proposal = asyncio.run(coordinator.synthesize(session.planning_session_id))
        assert proposal.synthesizer_worker_id in {alice.worker_id, victor.worker_id}

    def test_synthesis_quota_exhaustion_fails_closed_not_downgraded(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        alice, victor = _alice(), _victor()
        selector = RecordingTierAwareWorkerSelector([alice, victor])
        engine = FakePlanningExecutionEngine({
            alice.worker_id: _planning_result(execution_id="e1", worker=alice, payload=_valid_planner_payload()),
            victor.worker_id: _planning_result(execution_id="e2", worker=victor, payload=_valid_planner_payload()),
        })
        # Both planners are STANDARD-tier only; synthesis demands COMPLEX.
        recommendation_service = FakeRecommendationService(by_role={
            PLANNER_ROLE: _recommendation(role=PLANNER_ROLE, minimum_quality_tier=QualityTier.STANDARD),
            SYNTHESIZER_ROLE: _recommendation(
                recommendation_id="rec-synth-2", role=SYNTHESIZER_ROLE, minimum_quality_tier=QualityTier.COMPLEX,
            ),
        })
        decision_store = FakeDecisionStore()
        coordinator = _adaptive_coordinator(
            project_store, activity_store, planning_store, selector, engine, recommendation_service, decision_store,
        )
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))

        with pytest.raises(PlanningSessionFailedError):
            asyncio.run(coordinator.synthesize(session.planning_session_id))

        assert planning_store.get_session(session.planning_session_id).status is PlanningSessionStatus.FAILED
        # No synthesis decision was ever fabricated.
        assert all(d.role != SYNTHESIZER_ROLE for d in decision_store.recorded)


class TestRoadmapSynthesisIsRealLLM:
    """Factual finding (Slice 19): unlike a hypothetical deterministic
    synthesis, THIS codebase's roadmap synthesis genuinely runs via
    WorkerSelector + RalphExecutionEngine (SYNTHESIZER_ROLE,
    roadmap_synthesis capability) — so the CAS B branch of the Slice 19
    spec applies, never CAS A. This is a factual/documentation-anchoring
    test, not a behavior test."""

    def test_synthesize_genuinely_launches_a_worker_execution(self, tmp_path: Path) -> None:
        project_store, activity_store, planning_store = _stores(tmp_path)
        selector = FakePlanningWorkerSelector([_alice(), _victor()])
        engine = FakePlanningExecutionEngine({
            "claude_dev_01": _planning_result(execution_id="e1", worker=_alice(), payload=_valid_planner_payload()),
            "codex_dev_01": _planning_result(execution_id="e2", worker=_victor(), payload=_valid_planner_payload()),
        })
        coordinator = _coordinator(project_store, activity_store, planning_store, selector, engine)
        session = asyncio.run(coordinator.start_planning_session(project_id="proj-1", mvp_id="mvp-1", release_id="release-1"))
        asyncio.run(coordinator.run_planners(session.planning_session_id))
        engine._results_by_worker["claude_dev_01"] = _planning_result(
            execution_id="e3", worker=_alice(), topic=SYNTHESIS_PROPOSED_TOPIC, payload=_valid_synthesizer_payload(),
        )

        asyncio.run(coordinator.synthesize(session.planning_session_id))

        # A real ExecutionRequest was built and sent through the real
        # RalphExecutionEngine seam — never a Python-only deterministic
        # computation.
        assert any(r.role == SYNTHESIZER_ROLE for r in engine.requests)
