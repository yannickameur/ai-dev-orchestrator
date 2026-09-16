"""Tests for MVPManager (Phase 1 / Slice 7).

All tests are offline: fake WorkerSelector/RalphExecutionEngine stand in
for the real ones — no subprocess, no network, no real
Claude/Codex/Ralph/Git invocation anywhere in this file.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.adaptive_execution import AdaptiveExecutionDecision, AdaptiveSelection
from orchestrator.complexity_estimation import (
    ComplexityEstimationRequest,
    NoReliableRecommendationError,
)
from orchestrator.execution_store import ExecutionRecord, ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.mvp_manager import REVIEW_CAPABILITY, MVPManager, WorkflowMode
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent, RalphLaunchError
from orchestrator.recovery import RECOVERY_NEXT_ACTION, RECOVERY_OPEN_ISSUE
from orchestrator.review import ReviewPolicy, ReviewStatus, ReviewStore
from orchestrator.validation import (
    QualityGateRunner,
    ValidationCommand,
    ValidationKind,
    ValidationStore,
)
from orchestrator.wait import WaitPhase, WaitReason, WaitStatus, WaitStore
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ProviderSelectionDiagnostic,
    QualityTier,
    Worker,
    WorkerSelectionRequest,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
PY = sys.executable


def _alice() -> Worker:
    return Worker.with_single_profile(
        worker_id="claude_dev_01", display_name="Alice", provider="anthropic",
        backend="claude_code", model="sonnet", capabilities=frozenset({"developer"}),
    )


def _stores(tmp_path: Path):
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    handoff_store = HandoffStore(tmp_path / "handoff.sqlite3", clock=lambda: UTC_NOW)
    return project_store, handoff_store


def _seed(project_store: ProjectStateStore, tmp_path: Path, **work_items) -> None:
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")
    for work_item_id, kwargs in work_items.items():
        project_store.create_work_item(work_item_id=work_item_id, mvp_id="mvp-1", **kwargs)


def _execution_result(
    *, execution_id: str, task_id: str, worker_id: str, status: ExecutionStatus,
    git_sha_after: str | None = None,
) -> ExecutionResult:
    record = ExecutionRecord(
        execution_id=execution_id, task_id=task_id, worker_id=worker_id,
        provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        started_at=UTC_NOW, status=status, git_sha_after=git_sha_after,
    )
    return ExecutionResult(record=record, exit_code=0 if status is ExecutionStatus.SUCCEEDED else 1)


class FakeWorkerSelector:
    def __init__(self, worker: Worker | None = None, error: Exception | None = None) -> None:
        self._worker = worker
        self._error = error
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._worker


class FakeExecutionEngine:
    def __init__(self, result: ExecutionResult | None = None, results_by_task: dict | None = None) -> None:
        self._result = result
        self._results_by_task = results_by_task or {}
        self.requests: list = []

    async def execute(self, request):
        self.requests.append(request)
        if request.task_id in self._results_by_task:
            return self._results_by_task[request.task_id]
        return self._result


def _counting_id_factory():
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return id_factory


def _manager(project_store, handoff_store, selector, engine) -> MVPManager:
    id_factory = _counting_id_factory()
    return MVPManager(
        project_store, handoff_store, selector, engine,
        clock=lambda: UTC_NOW, id_factory=id_factory,
        workflow_mode=WorkflowMode.GOVERNED_FULL,
    )


class TestBasicRun:
    def test_work_item_without_dependency_is_executed_and_completed(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A", "required_capabilities": ["developer"]}})

        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result is not None
        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.COMPLETED

    def test_no_ready_work_item_returns_none(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}},
        )
        # Only wi-b's dependency is unsatisfied; wi-a itself is READY, so run
        # it first, then check wi-b alone (still blocked on wi-a).
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        # wi-a stays RUNNING (not completed): wi-b must not become READY.
        selector = FakeWorkerSelector(worker=_alice())
        engine = FakeExecutionEngine()
        manager = _manager(project_store, handoff_store, selector, engine)

        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert outcome is None
        assert engine.requests == []


class TestWorkerSelectorIntegration:
    def test_required_capabilities_are_transmitted_to_worker_selector(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A", "required_capabilities": ["developer", "python"]}},
        )
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert selector.requests[0].required_capabilities == frozenset({"developer", "python"})

    def test_selected_worker_is_transmitted_to_execution_engine(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert engine.requests[0].worker is alice

    def test_no_eligible_worker_propagates_and_leaves_work_item_ready(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        from orchestrator.worker_selector import WorkerSelectionRequest

        selector = FakeWorkerSelector(
            error=NoEligibleWorkerError(WorkerSelectionRequest(required_capabilities=frozenset()))
        )
        engine = FakeExecutionEngine()
        manager = _manager(project_store, handoff_store, selector, engine)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        # The WorkItem was never marked RUNNING: no execution was attempted.
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.READY
        assert engine.requests == []


class TestFailureAndDependents:
    def test_execution_failure_yields_non_completed_work_item(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.FAILED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.FAILED
        assert result.work_item.status is not WorkItemStatus.COMPLETED

    def test_dependent_work_item_is_never_executed_after_a_failure(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}},
        )
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.FAILED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))  # runs & fails wi-a
        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))  # nothing else eligible

        assert outcome is None
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.BLOCKED
        # wi-b was never handed to the execution engine.
        assert all(req.task_id != "wi-b" for req in engine.requests)

    def test_no_automatic_retry_after_failure(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.FAILED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))
        asyncio.run(manager.run_next_work_item("mvp-1"))  # a second call: nothing to retry

        assert len(engine.requests) == 1  # never called again for wi-a


class TestDeterministicOrder:
    def test_lexically_first_ready_work_item_runs_first(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-b": {"title": "B"}, "wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.work_item_id == "wi-a"


class TestHandoff:
    def test_handoff_is_created_after_execution(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED, git_sha_after="deadbee",
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        handoff = result.handoff
        assert handoff.work_item_id == "wi-a"
        assert handoff.execution_id == "exec-1"
        assert handoff.worker_id == alice.worker_id
        assert handoff.git_sha_after == "deadbee"
        assert handoff.created_at.tzinfo is not None

    def test_handoff_is_readable_after_restart(self, tmp_path: Path) -> None:
        project_db = tmp_path / "project.sqlite3"
        handoff_db = tmp_path / "handoff.sqlite3"
        project_store = ProjectStateStore(project_db, clock=lambda: UTC_NOW)
        handoff_store = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)
        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        handoff_id = result.handoff.handoff_id
        project_store.close()
        handoff_store.close()

        reopened_handoffs = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        fetched = reopened_handoffs.get(handoff_id)
        assert fetched.work_item_id == "wi-a"
        assert reopened_handoffs.latest_for_work_item("wi-a").handoff_id == handoff_id


class TestResumeAfterRestart:
    def test_running_work_item_is_never_picked_up_again(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")  # simulate a prior crash mid-run

        selector = FakeWorkerSelector(worker=_alice())
        engine = FakeExecutionEngine()
        manager = _manager(project_store, handoff_store, selector, engine)

        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert outcome is None
        assert engine.requests == []
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.RUNNING


class TestNoForbiddenBehavior:
    def test_module_never_shells_out_or_duplicates_worker_selector_logic(self) -> None:
        from orchestrator import mvp_manager as module

        source = inspect.getsource(module)
        for forbidden in (
            "import subprocess", "asyncio.create_subprocess", "Popen",
            "ClaudeCodeAdapter", "CodexAdapter", "QuotaManager(",
        ):
            assert forbidden not in source


def _gate_runner(tmp_path: Path, commands: list[ValidationCommand]) -> QualityGateRunner:
    store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
    store.set_project_commands("proj-1", commands)
    return QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=lambda: "gate-run-1")


class TestQualityGateIntegration:
    def test_no_runner_behaves_like_slice_7(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)  # no quality_gate_runner

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.gate_result is None

    def test_no_configured_commands_gate_trivially_passes(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(tmp_path, [])
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW,
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.gate_result.passed is True

    def test_passing_gate_completes_work_item(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(
            tmp_path,
            [ValidationCommand(validation_id="unit-tests", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"))],
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW,
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.gate_result.passed is True
        assert "quality_gate=PASSED" in result.handoff.test_results

    def test_failing_required_gate_prevents_completion(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(
            tmp_path,
            [
                ValidationCommand(
                    validation_id="unit-tests", kind=ValidationKind.UNIT_TEST,
                    argv=(PY, "-c", "import sys; sys.exit(1)"),
                )
            ],
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW,
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is not WorkItemStatus.COMPLETED
        assert result.work_item.status is WorkItemStatus.FAILED
        assert result.gate_result.passed is False

    def test_dependent_not_launched_after_gate_failure(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}},
        )
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(
            tmp_path,
            [
                ValidationCommand(
                    validation_id="unit-tests", kind=ValidationKind.UNIT_TEST,
                    argv=(PY, "-c", "import sys; sys.exit(1)"),
                )
            ],
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW,
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-a: execution ok, gate fails
        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert outcome is None
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.BLOCKED
        assert all(req.task_id != "wi-b" for req in engine.requests)

    def test_handoff_contains_quality_gate_result(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(
            tmp_path,
            [ValidationCommand(validation_id="lint", kind=ValidationKind.LINT, argv=(PY, "-c", "pass"))],
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW,
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert "lint=passed" in result.handoff.test_results


def _victor() -> Worker:
    return Worker.with_single_profile(
        worker_id="codex_dev_01", display_name="Victor", provider="openai",
        backend="codex", model="gpt-5.6-terra", capabilities=frozenset({REVIEW_CAPABILITY}),
    )


def _chloe() -> Worker:
    return Worker.with_single_profile(
        worker_id="claude_dev_02", display_name="Chloe", provider="anthropic",
        backend="claude_code", model="haiku", capabilities=frozenset({"developer"}),
    )


def _review_result(
    *, execution_id: str, worker_id: str, provider: str, model: str, status: ExecutionStatus,
    events: tuple = (), exit_code: int | None = None,
) -> ExecutionResult:
    record = ExecutionRecord(
        execution_id=execution_id, task_id="wi-a", worker_id=worker_id, provider=provider,
        backend="codex" if provider == "openai" else "claude_code", model=model, role="reviewer",
        started_at=UTC_NOW, status=status,
    )
    return ExecutionResult(
        record=record, events=events,
        exit_code=exit_code if exit_code is not None else (0 if status is ExecutionStatus.SUCCEEDED else 1),
    )


class FakeReviewAwareWorkerSelector:
    """Returns a different worker for developer vs reviewer requests."""

    def __init__(self, dev_workers: list[Worker], reviewer: Worker | None = None, reviewer_error: Exception | None = None) -> None:
        self._dev_workers = list(dev_workers)
        self._reviewer = reviewer
        self._reviewer_error = reviewer_error
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        if REVIEW_CAPABILITY in request.required_capabilities:
            if self._reviewer_error is not None:
                raise self._reviewer_error
            return self._reviewer
        return self._dev_workers.pop(0) if len(self._dev_workers) > 1 else self._dev_workers[0]


class FakeReviewAwareExecutionEngine:
    """Returns dev results in order, and review results in order, keyed by role."""

    def __init__(self, dev_results: list, review_results: list | None = None, raise_on_review: Exception | None = None) -> None:
        self._dev_results = list(dev_results)
        self._review_results = list(review_results or [])
        self._raise_on_review = raise_on_review
        self.requests: list = []

    async def execute(self, request):
        self.requests.append(request)
        if request.role == "reviewer":
            if self._raise_on_review is not None:
                raise self._raise_on_review
            return self._review_results.pop(0) if len(self._review_results) > 1 else self._review_results[0]
        return self._dev_results.pop(0) if len(self._dev_results) > 1 else self._dev_results[0]


def _review_manager(
    project_store, handoff_store, selector, engine, *, gate_runner=None, review_policy=None,
) -> tuple[MVPManager, ReviewStore]:
    review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    manager = MVPManager(
        project_store, handoff_store, selector, engine,
        quality_gate_runner=gate_runner, review_store=review_store, review_policy=review_policy,
        clock=lambda: UTC_NOW, id_factory=id_factory,
        workflow_mode=WorkflowMode.GOVERNED_FULL,
    )
    return manager, review_store


class TestIndependentReview:
    def test_reviewer_differs_from_author_and_is_excluded_explicitly(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED)],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        review_request = next(r for r in selector.requests if REVIEW_CAPABILITY in r.required_capabilities)
        assert review_request.author_worker_id == alice.worker_id
        assert result.review_result.reviewer_worker_id == victor.worker_id
        assert result.review_result.reviewer_worker_id != result.review_result.author_worker_id

    def test_quality_gate_failed_prevents_any_review(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
        )
        gate_runner = _gate_runner(tmp_path, [ValidationCommand(validation_id="t", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "import sys; sys.exit(1)"))])
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine, gate_runner=gate_runner)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.review_result is None
        assert all(req.role != "reviewer" for req in engine.requests)
        assert result.work_item.status is WorkItemStatus.FAILED

    def test_gate_passed_triggers_review(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED)],
        )
        gate_runner = _gate_runner(tmp_path, [ValidationCommand(validation_id="t", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"))])
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine, gate_runner=gate_runner)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert any(req.role == "reviewer" for req in engine.requests)
        assert result.review_result.status is ReviewStatus.APPROVED

    def test_approved_review_completes_work_item(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED, exit_code=2)],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.review_result.status is ReviewStatus.APPROVED

    def test_rejected_review_persists_findings_and_needs_rework(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        rejection_event = RalphEvent(topic="review.rejected", timestamp=UTC_NOW, payload="off by one error")
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.FAILED, events=(rejection_event,))],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.review_result.status is ReviewStatus.REJECTED
        assert result.review_result.findings[0].summary == "off by one error"
        assert result.work_item.status is WorkItemStatus.NEEDS_REWORK
        assert "off by one error" in result.handoff.open_issues

    def test_dependents_not_executed_after_needs_rework(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}})
        alice = _alice()
        victor = _victor()
        rejection_event = RalphEvent(topic="review.rejected", timestamp=UTC_NOW, payload="nope")
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice, alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.FAILED, events=(rejection_event,))],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-a -> NEEDS_REWORK
        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))  # picks wi-a again (NEEDS_REWORK), not wi-b

        assert all(req.task_id != "wi-b" for req in engine.requests)
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.PLANNED

    def test_no_eligible_reviewer_blocks_without_completing(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        from orchestrator.worker_selector import WorkerSelectionRequest

        selector = FakeReviewAwareWorkerSelector(
            dev_workers=[alice],
            reviewer_error=NoEligibleWorkerError(WorkerSelectionRequest(required_capabilities=frozenset({REVIEW_CAPABILITY}))),
        )
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert result.review_result.status is ReviewStatus.ERROR
        assert all(req.role != "reviewer" or req not in engine.requests for req in engine.requests)

    def test_review_execution_error_blocks_without_completing(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            raise_on_review=RalphLaunchError("ralph not found"),
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert result.review_result.status is ReviewStatus.ERROR

    def test_max_review_cycles_exhausted_blocks_with_explicit_reason(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        rejection_event = RalphEvent(topic="review.rejected", timestamp=UTC_NOW, payload="still broken")
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice, alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.FAILED, events=(rejection_event,))],
        )
        manager, review_store = _review_manager(
            project_store, handoff_store, selector, engine, review_policy=ReviewPolicy(max_review_cycles=2)
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))  # cycle 1: rejected -> NEEDS_REWORK
        result = asyncio.run(manager.run_next_work_item("mvp-1"))  # cycle 2: rejected -> BLOCKED (limit)

        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert "max_review_cycles" in result.work_item.blocked_reason
        assert review_store.count_for_work_item("wi-a") == 2

    def test_rework_uses_a_different_developer_and_reviewer_still_excludes_it(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        chloe = _chloe()
        victor = _victor()
        rejection_event = RalphEvent(topic="review.rejected", timestamp=UTC_NOW, payload="nope")
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice, chloe], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[
                _execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED),
                _execution_result(execution_id="exec-3", task_id="wi-a", worker_id=chloe.worker_id, status=ExecutionStatus.SUCCEEDED),
            ],
            review_results=[
                _review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.FAILED, events=(rejection_event,)),
                _review_result(execution_id="exec-4", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED),
            ],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        asyncio.run(manager.run_next_work_item("mvp-1"))  # Alice dev -> Victor rejects -> NEEDS_REWORK
        result = asyncio.run(manager.run_next_work_item("mvp-1"))  # Chloe dev -> Victor approves

        assert result.work_item.status is WorkItemStatus.COMPLETED
        reviews = review_store.list_for_work_item("wi-a")
        assert reviews[0].author_worker_id == alice.worker_id
        assert reviews[1].author_worker_id == chloe.worker_id
        assert review_store.count_for_work_item("wi-a") == 2

    def test_review_history_retrievable_after_restart(self, tmp_path: Path) -> None:
        project_db = tmp_path / "project.sqlite3"
        handoff_db = tmp_path / "handoff.sqlite3"
        review_db = tmp_path / "review.sqlite3"
        project_store = ProjectStateStore(project_db, clock=lambda: UTC_NOW)
        handoff_store = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED)],
        )
        review_store = ReviewStore(review_db, clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, selector, engine, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )
        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        review_id = result.review_result.review_id
        review_store.close()

        reopened = ReviewStore(review_db, clock=lambda: UTC_NOW)
        fetched = reopened.get(review_id)
        assert fetched.status is ReviewStatus.APPROVED
        assert reopened.latest_for_work_item("wi-a").review_id == review_id

    def test_review_never_approved_without_terminal_event(self, tmp_path: Path) -> None:
        # RalphExecutionEngine itself would already return FAILED here (no
        # reliable business event); MVPManager must classify this as ERROR,
        # never derive REJECTED findings or APPROVED from nothing.
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.FAILED, events=())],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.review_result.status is ReviewStatus.ERROR
        assert result.work_item.status is not WorkItemStatus.COMPLETED

    def test_interrupted_review_never_completes(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
            review_results=[_review_result(execution_id="exec-2", worker_id=victor.worker_id, provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.INTERRUPTED)],
        )
        manager, review_store = _review_manager(project_store, handoff_store, selector, engine)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.review_result.status is ReviewStatus.INTERRUPTED
        assert result.work_item.status is not WorkItemStatus.COMPLETED

    def test_no_review_store_preserves_slice_8_behavior(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice])
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[_execution_result(execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)],
        )
        manager = _manager(project_store, handoff_store, selector, engine)  # no review_store

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.review_result is None


def _quota_diag(*, provider: str = "anthropic", reset_at: datetime | None = None) -> ProviderSelectionDiagnostic:
    return ProviderSelectionDiagnostic(
        provider=provider, available=False, reason="quota_exhausted",
        reset_at=(reset_at,) if reset_at is not None else (),
    )


def _probe_error_diag(*, provider: str = "anthropic") -> ProviderSelectionDiagnostic:
    return ProviderSelectionDiagnostic(provider=provider, available=False, reason="probe_error")


class ScriptedWorkerSelector:
    """Replays a fixed sequence of results (Worker or Exception), in order.

    Used to script "quota exhausted now, recovered (maybe with a
    different worker) later" scenarios — this codebase only ever awaits
    ``select()`` sequentially, never concurrently, so a single queue is
    enough and stays simple.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ScriptedReviewWorkerSelector:
    """Fixed developer, scripted sequence of reviewer outcomes."""

    def __init__(self, dev_worker: Worker, reviewer_script: list) -> None:
        self._dev_worker = dev_worker
        self._reviewer_script = list(reviewer_script)
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        if REVIEW_CAPABILITY in request.required_capabilities:
            outcome = self._reviewer_script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return self._dev_worker


def _wait_manager(
    project_store, handoff_store, selector, engine, *, wait_store, review_store=None, clock,
) -> MVPManager:
    return MVPManager(
        project_store, handoff_store, selector, engine,
        review_store=review_store, wait_store=wait_store,
        clock=clock, id_factory=_counting_id_factory(),
        workflow_mode=WorkflowMode.GOVERNED_FULL,
    )


class TestQuotaWaitingOnDeveloperSelection:
    def test_quota_exhausted_moves_work_item_to_waiting_with_eligible_at(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        reset_at = UTC_NOW + timedelta(hours=3)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        selector = FakeWorkerSelector(error=error)
        engine = FakeExecutionEngine()
        manager = _wait_manager(project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: UTC_NOW)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result is not None
        assert result.work_item.status is WorkItemStatus.WAITING
        assert result.handoff is None  # nothing executed — no attempt to record
        assert result.wait is not None
        assert result.wait.eligible_at == reset_at
        assert result.wait.phase is WaitPhase.DEVELOPMENT
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.WAITING
        assert engine.requests == []

    def test_probe_error_still_propagates_even_with_wait_store_configured(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_probe_error_diag(),),
        )
        selector = FakeWorkerSelector(error=error)
        engine = FakeExecutionEngine()
        manager = _wait_manager(project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: UTC_NOW)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert project_store.get_work_item("wi-a").status is WorkItemStatus.READY
        assert wait_store.list_pending() == []

    def test_unrelated_exception_still_propagates_with_wait_store_configured(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(error=RuntimeError("programming error, not a provider failure"))
        engine = FakeExecutionEngine()
        manager = _wait_manager(project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: UTC_NOW)

        with pytest.raises(RuntimeError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert wait_store.list_pending() == []

    def test_dependent_of_waiting_work_item_never_launched(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}},
        )
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        reset_at = UTC_NOW + timedelta(hours=3)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        selector = FakeWorkerSelector(error=error)
        engine = FakeExecutionEngine()
        manager = _wait_manager(project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: UTC_NOW)

        asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-a -> WAITING
        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))  # not due yet

        assert outcome is None
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.PLANNED
        assert all(req.task_id != "wi-b" for req in engine.requests)


class TestQuotaWaitResumeOnDeveloperSelection:
    def test_resume_after_deadline_uses_fresh_execution_id_and_may_pick_a_different_worker(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        victor_dev = Worker.with_single_profile(
            worker_id="codex_dev_09", display_name="Victor", provider="openai",
            backend="codex", model="terra", capabilities=frozenset({"developer"}),
        )
        selector = ScriptedWorkerSelector([error, victor_dev])
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="whatever-the-fake-returns", task_id="wi-a",
                worker_id=victor_dev.worker_id, status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: clock_box["now"]
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.WAITING

        clock_box["now"] = reset_at + timedelta(minutes=5)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.COMPLETED
        assert engine.requests[0].worker.worker_id == victor_dev.worker_id
        # A brand new execution_id was generated for the resumed attempt —
        # never a reused/blind-retried one.
        assert engine.requests[0].execution_id not in (None, "")
        assert len(engine.requests) == 1
        resolved_wait = wait_store.get(first.wait.wait_id)
        assert resolved_wait.status is WaitStatus.RESOLVED

    def test_resume_before_deadline_does_nothing(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        selector = ScriptedWorkerSelector([error])
        engine = FakeExecutionEngine()
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: clock_box["now"]
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at - timedelta(minutes=1)
        outcome = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert outcome is None
        assert len(selector.requests) == 1  # never re-probed before the deadline
        assert wait_store.list_pending()[0].status is WaitStatus.PENDING

    def test_resume_still_unavailable_requeues_with_a_new_reset(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at_1 = UTC_NOW + timedelta(hours=3)
        reset_at_2 = UTC_NOW + timedelta(hours=6)
        error1 = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at_1),),
        )
        error2 = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at_2),),
        )
        selector = ScriptedWorkerSelector([error1, error2])
        engine = FakeExecutionEngine()
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: clock_box["now"]
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at_1 + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.WAITING
        assert second.wait.eligible_at == reset_at_2
        assert wait_store.get(first.wait.wait_id).status is WaitStatus.RESOLVED
        assert engine.requests == []  # never executed anything blindly

    def test_resume_gives_up_to_blocked_when_no_reliable_reset_remains(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        error1 = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        error2 = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(_probe_error_diag(),),
        )
        selector = ScriptedWorkerSelector([error1, error2])
        engine = FakeExecutionEngine()
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store, clock=lambda: clock_box["now"]
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.BLOCKED
        assert "no reliable reset" in second.work_item.blocked_reason
        assert wait_store.get(first.wait.wait_id).status is WaitStatus.RESOLVED
        assert wait_store.list_pending() == []


class TestQuotaWaitingOnReviewerSelection:
    def test_reviewer_quota_exhausted_moves_to_waiting_review_phase_without_completing(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        reset_at = UTC_NOW + timedelta(hours=2)
        reviewer_error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset({REVIEW_CAPABILITY})),
            diagnostics=(_quota_diag(provider="openai", reset_at=reset_at),),
        )
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer_error=reviewer_error)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[
                _execution_result(
                    execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                    status=ExecutionStatus.SUCCEEDED, git_sha_after="sha-dev",
                )
            ],
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store,
            review_store=review_store, clock=lambda: UTC_NOW,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.WAITING
        assert result.work_item.status is not WorkItemStatus.COMPLETED
        assert result.review_result is None
        assert result.handoff is not None  # the development attempt itself is still recorded
        assert result.handoff.git_sha_after == "sha-dev"
        # no spurious review recorded — must never consume a bounded rework cycle.
        assert review_store.count_for_work_item("wi-a") == 0
        pending = wait_store.list_pending()
        assert len(pending) == 1
        assert pending[0].phase is WaitPhase.REVIEW
        assert pending[0].eligible_at == reset_at

    def test_review_wait_resume_uses_last_handoff_with_fresh_execution_and_may_change_reviewer(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        victor = _victor()
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=2)
        reviewer_error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset({REVIEW_CAPABILITY})),
            diagnostics=(_quota_diag(provider="openai", reset_at=reset_at),),
        )
        selector = ScriptedReviewWorkerSelector(dev_worker=alice, reviewer_script=[reviewer_error, victor])
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[
                _execution_result(
                    execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                    status=ExecutionStatus.SUCCEEDED, git_sha_after="sha-dev",
                )
            ],
            review_results=[
                _review_result(
                    execution_id="whatever-the-fake-returns", worker_id=victor.worker_id,
                    provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED,
                )
            ],
        )
        review_store = ReviewStore(":memory:", clock=lambda: clock_box["now"])
        manager = _wait_manager(
            project_store, handoff_store, selector, engine, wait_store=wait_store,
            review_store=review_store, clock=lambda: clock_box["now"],
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.WAITING

        clock_box["now"] = reset_at + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.COMPLETED
        assert second.review_result.status is ReviewStatus.APPROVED
        assert second.review_result.reviewer_worker_id == victor.worker_id
        assert second.review_result.author_worker_id == alice.worker_id
        assert second.review_result.reviewer_worker_id != second.review_result.author_worker_id
        review_requests = [r for r in engine.requests if r.role == "reviewer"]
        assert len(review_requests) == 1
        assert review_requests[0].execution_id  # freshly generated, never the old dev execution_id
        assert review_requests[0].execution_id != "exec-1"
        # the review used the dev git SHA carried over from the last handoff.
        assert "sha-dev" in review_requests[0].instructions


class TestNoForbiddenWaitBehavior:
    def test_wait_module_never_polls_shells_out_or_touches_reset_credit(self) -> None:
        from orchestrator import wait as wait_module

        source = inspect.getsource(wait_module)
        for forbidden in (
            "import subprocess", "asyncio.create_subprocess", "Popen",
            "ResetCredit", ".consume(", "time.sleep", "while True",
            "ClaudeCodeAdapter", "CodexAdapter",
        ):
            assert forbidden not in source

    def test_mvp_manager_wait_integration_never_polls_or_touches_reset_credit(self) -> None:
        from orchestrator import mvp_manager as module

        source = inspect.getsource(module)
        for forbidden in (
            "ResetCredit", ".consume(", "time.sleep", "while True",
            "import subprocess", "asyncio.create_subprocess", "Popen",
        ):
            assert forbidden not in source

    def test_recovery_module_never_shells_out_or_touches_reset_credit(self) -> None:
        from orchestrator import recovery as module

        source = inspect.getsource(module)
        for forbidden in (
            "import subprocess", "asyncio.create_subprocess", "Popen",
            "ResetCredit", ".consume(", "time.sleep", "while True",
            "ClaudeCodeAdapter", "CodexAdapter", "QuotaManager(", "WorkerSelector(",
        ):
            assert forbidden not in source


def _execution_store(tmp_path: Path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "execution.sqlite3", clock=lambda: UTC_NOW)


class TestExecutionRecoveryDevPhase:
    """Slice 11b: an orphaned/interrupted RUNNING development execution is
    reconciled explicitly and resumed with a brand-new execution — never
    relaunched, never silently treated as success or plain FAILED."""

    def test_orphaned_running_dev_resumes_with_fresh_execution_and_may_change_worker(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")  # simulate a prior crash mid-run
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )  # never finalized — orphaned by the crash

        chloe = _chloe()
        selector = FakeWorkerSelector(worker=chloe)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="whatever-the-fake-returns", task_id="wi-a",
                worker_id=chloe.worker_id, status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert execution_store.get("exec-old").status is ExecutionStatus.RECOVERY_REQUIRED
        assert engine.requests[0].worker.worker_id == chloe.worker_id
        assert engine.requests[0].execution_id != "exec-old"
        # Two handoffs: the recovery one (created during reconciliation)
        # plus the fresh completion handoff — never overwriting history.
        assert len(handoff_store.list_for_work_item("wi-a")) == 2

    def test_resumed_dev_execution_replays_the_quality_gate(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="whatever", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        gate_runner = _gate_runner(
            tmp_path, [ValidationCommand(validation_id="unit-tests", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"))]
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            quality_gate_runner=gate_runner, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        # A brand-new development execution always means new code could
        # have been produced — the gate must be re-evaluated before review.
        assert result.gate_result is not None
        assert result.gate_result.passed is True

    def test_orphaned_dev_execution_never_relaunched_and_stays_historical(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="whatever", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        # exec-old is never reused as a request target, and its terminal
        # status is never flipped back toward RUNNING.
        assert all(req.execution_id != "exec-old" for req in engine.requests)
        assert execution_store.get("exec-old").status is ExecutionStatus.RECOVERY_REQUIRED

    def test_interrupted_dev_execution_within_same_call_becomes_recovery_required_not_failed(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-timeout", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.INTERRUPTED,
            )
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.RECOVERY_REQUIRED
        assert result.work_item.status is not WorkItemStatus.FAILED
        assert result.work_item.status is not WorkItemStatus.COMPLETED
        assert result.handoff is not None
        assert result.handoff.open_issues == RECOVERY_OPEN_ISSUE
        assert result.handoff.next_action == RECOVERY_NEXT_ACTION

    def test_interrupted_dev_execution_without_execution_store_preserves_old_failed_behavior(
        self, tmp_path: Path
    ) -> None:
        # Opt-in: no execution_store configured -> exact pre-Slice-11b
        # behavior (INTERRUPTED treated like any other non-success).
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="exec-timeout", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.INTERRUPTED,
            )
        )
        manager = _manager(project_store, handoff_store, selector, engine)  # no execution_store

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.FAILED

    def test_restart_after_interruption_resumes_with_new_execution(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-interrupted", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_interrupted("exec-interrupted")  # handled pre-restart, then the process crashed

        alice = _alice()
        selector = FakeWorkerSelector(worker=alice)
        engine = FakeExecutionEngine(
            result=_execution_result(
                execution_id="whatever", task_id="wi-a", worker_id=alice.worker_id,
                status=ExecutionStatus.SUCCEEDED,
            )
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert execution_store.get("exec-interrupted").status is ExecutionStatus.INTERRUPTED
        assert all(req.execution_id != "exec-interrupted" for req in engine.requests)

    def test_recovery_required_dependent_never_executed(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(
            project_store, tmp_path,
            **{"wi-a": {"title": "A"}, "wi-b": {"title": "B", "dependencies": ["wi-a"]}},
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        # No eligible developer at resume time — an ordinary, non-quota
        # exception, propagates exactly like the pre-Slice-11 top-level
        # dev selection path (no wait_store configured here).
        selector = FakeWorkerSelector(
            error=NoEligibleWorkerError(WorkerSelectionRequest(required_capabilities=frozenset()))
        )
        engine = FakeExecutionEngine()
        manager = MVPManager(
            project_store, handoff_store, selector, engine, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert project_store.get_work_item("wi-a").status is WorkItemStatus.RECOVERY_REQUIRED
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.PLANNED
        assert engine.requests == []


class TestExecutionRecoveryReviewPhase:
    """Slice 11b applied symmetrically to a review execution."""

    def test_orphaned_review_resumes_with_fresh_execution_and_independent_reviewer(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-dev", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-dev")
        handoff_store.create(
            handoff_id="handoff-dev", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev", worker_id="claude_dev_01",
            test_results="quality_gate=PASSED (unit-tests=passed)", next_action="proceed to review",
            created_at=UTC_NOW,
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review-old", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )  # orphaned mid-review

        victor = _victor()
        selector = ScriptedReviewWorkerSelector(dev_worker=_alice(), reviewer_script=[victor])
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[],
            review_results=[
                _review_result(
                    execution_id="whatever", worker_id=victor.worker_id,
                    provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED,
                )
            ],
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, selector, engine, review_store=review_store,
            execution_store=execution_store, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.review_result.status is ReviewStatus.APPROVED
        assert result.review_result.reviewer_worker_id == victor.worker_id
        assert result.review_result.author_worker_id == "claude_dev_01"
        assert result.review_result.reviewer_worker_id != result.review_result.author_worker_id
        assert execution_store.get("exec-review-old").status is ExecutionStatus.RECOVERY_REQUIRED
        review_requests = [r for r in engine.requests if r.role == "reviewer"]
        assert len(review_requests) == 1
        assert review_requests[0].execution_id != "exec-review-old"
        # No fantom review ever recorded for the orphaned attempt itself.
        assert review_store.count_for_work_item("wi-a") == 1

    def test_review_interrupted_within_same_call_becomes_recovery_required_no_fantom_verdict(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        alice = _alice()
        victor = _victor()
        selector = FakeReviewAwareWorkerSelector(dev_workers=[alice], reviewer=victor)
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[
                _execution_result(
                    execution_id="exec-1", task_id="wi-a", worker_id=alice.worker_id,
                    status=ExecutionStatus.SUCCEEDED,
                )
            ],
            review_results=[
                _review_result(
                    execution_id="exec-2", worker_id=victor.worker_id, provider="openai",
                    model="gpt-5.6-terra", status=ExecutionStatus.INTERRUPTED,
                )
            ],
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, selector, engine, review_store=review_store,
            execution_store=execution_store, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.RECOVERY_REQUIRED
        assert result.work_item.status is not WorkItemStatus.COMPLETED
        # An honest INTERRUPTED record is kept (not a fantom verdict: never
        # APPROVED/REJECTED for an attempt that never reached one), but it
        # never burns a bounded rework cycle either.
        assert result.review_result.status is ReviewStatus.INTERRUPTED
        assert review_store.count_for_work_item("wi-a") == 1

    def test_review_phase_recovery_never_replays_the_quality_gate(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-dev", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-dev")
        handoff_store.create(
            handoff_id="handoff-dev", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev", worker_id="claude_dev_01",
            test_results="quality_gate=PASSED (unit-tests=passed)", next_action="proceed to review",
            created_at=UTC_NOW,
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review-old", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )

        victor = _victor()
        selector = ScriptedReviewWorkerSelector(dev_worker=_alice(), reviewer_script=[victor])
        engine = FakeReviewAwareExecutionEngine(
            dev_results=[],
            review_results=[
                _review_result(
                    execution_id="whatever", worker_id=victor.worker_id,
                    provider="openai", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED,
                )
            ],
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        # A gate that would FAIL if it were ever invoked here — proves it
        # never is: no new development code was produced, so the already
        # PASSED gate result is carried forward from the handoff as-is.
        gate_runner = _gate_runner(
            tmp_path,
            [ValidationCommand(validation_id="t", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "import sys; sys.exit(1)"))],
        )
        manager = MVPManager(
            project_store, handoff_store, selector, engine, review_store=review_store,
            quality_gate_runner=gate_runner, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.gate_result is None  # never re-evaluated during a review-only resume
        assert result.handoff.test_results == "quality_gate=PASSED (unit-tests=passed)"


# --- Slice 17: adaptive development/rework selection ------------------------


class FakeAdaptiveSelector:
    """A minimal AdaptiveExecutionSelector-shaped fake: scripted worker +
    decision, or a scripted error — never actually estimates/selects."""

    def __init__(self, *, worker: Worker | None = None, decision: AdaptiveExecutionDecision | None = None, error: Exception | None = None) -> None:
        self._worker = worker
        self._decision = decision
        self._error = error
        self.calls: list = []
        self.author_worker_ids: list = []

    async def select(
        self, *, estimation_request, required_capabilities, excluded_worker_ids=frozenset(),
        author_worker_id=None, force_refresh=False,
    ):
        self.calls.append(estimation_request)
        self.author_worker_ids.append(author_worker_id)
        if self._error is not None:
            raise self._error
        return AdaptiveSelection(worker=self._worker, decision=self._decision)


def _decision(**overrides) -> AdaptiveExecutionDecision:
    fields = dict(
        decision_id="decision-1", recommendation_id="rec-1", project_id="proj-1", role="developer",
        worker_id="codex_dev_01", provider="openai", backend="codex", profile_id="deep",
        quality_tier=QualityTier.COMPLEX, model="gpt-5.6-terra-deep", reasoning_effort="high",
        created_at=UTC_NOW,
    )
    fields.update(overrides)
    return AdaptiveExecutionDecision(**fields)


def _adaptive_manager(project_store, handoff_store, engine, adaptive_selector, *, wait_store=None) -> MVPManager:
    # A plain WorkerSelector is never consulted for development when
    # adaptive_execution_selector is configured — a scripted error proves
    # this if it were ever (wrongly) called.
    dead_selector = FakeWorkerSelector(error=AssertionError("plain WorkerSelector must not be used for adaptive development"))
    return MVPManager(
        project_store, handoff_store, dead_selector, engine,
        adaptive_execution_selector=adaptive_selector, wait_store=wait_store,
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
        workflow_mode=WorkflowMode.GOVERNED_FULL,
    )


class TestAdaptiveDevelopmentSelection:
    def test_development_calls_preflight(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing", "acceptance_criteria": ("works",)}})
        decision = _decision()
        victor = _victor()
        adaptive = FakeAdaptiveSelector(worker=victor, decision=decision)
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = _adaptive_manager(project_store, handoff_store, engine, adaptive)

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert len(adaptive.calls) == 1
        assert adaptive.calls[0].objective == "Do the thing"
        assert adaptive.calls[0].acceptance_criteria == ("works",)
        assert adaptive.calls[0].role == "developer"

    def test_rework_calls_preflight_with_review_findings(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        from orchestrator.review import ReviewFinding, ReviewRecord

        project_store.mark_work_item_reviewing("wi-a")
        review_store.record(ReviewRecord(
            review_id="rev-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            author_execution_id="e0", author_worker_id="claude_dev_01", started_at=UTC_NOW, finished_at=UTC_NOW,
            status=ReviewStatus.REJECTED, findings=(ReviewFinding(finding_id="f1", summary="off by one", severity="major"),),
        ))
        project_store.mark_work_item_needs_rework("wi-a")

        decision = _decision()
        victor = _victor()
        review_decision = _decision(
            decision_id="decision-review-1", recommendation_id="rec-review-1",
            worker_id="claude_dev_01", provider="anthropic", backend="claude_code", role="reviewer",
        )
        adaptive = ScriptedAdaptiveSelector([
            AdaptiveSelection(worker=victor, decision=decision),
            AdaptiveSelection(worker=_alice(), decision=review_decision),
        ])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(worker=_alice()), engine,
            adaptive_execution_selector=adaptive, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        # dev pre-flight (index 0) + review pre-flight (index 1, Slice 19:
        # review_store is configured here so review runs too) — dev always
        # comes first chronologically.
        assert len(adaptive.calls) == 2
        assert adaptive.calls[0].role == "developer"
        findings = adaptive.calls[0].review_findings
        assert len(findings) == 1 and findings[0].summary == "off by one"

    def test_execution_request_uses_decision_model_not_default_profile(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        victor = _victor()  # default_profile model would be "gpt-5.6-terra", not the decision's
        decision = _decision(model="gpt-5.6-terra-deep", reasoning_effort="high")
        adaptive = FakeAdaptiveSelector(worker=victor, decision=decision)
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = _adaptive_manager(project_store, handoff_store, engine, adaptive)

        asyncio.run(manager.run_next_work_item("mvp-1"))

        dev_request = engine.requests[0]
        assert dev_request.model == "gpt-5.6-terra-deep"
        assert dev_request.reasoning_effort == "high"

    def test_quality_gate_and_review_still_run_after_adaptive_development(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        victor = _victor()
        decision = _decision()
        review_decision = _decision(
            decision_id="decision-review-1", recommendation_id="rec-review-1",
            worker_id="claude_dev_01", provider="anthropic", backend="claude_code", role="reviewer",
        )
        adaptive = ScriptedAdaptiveSelector([
            AdaptiveSelection(worker=victor, decision=decision),
            AdaptiveSelection(worker=_alice(), decision=review_decision),
        ])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        gate_runner = _gate_runner(
            tmp_path, [ValidationCommand(validation_id="t", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"))]
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)

        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("plain selector must not be used")),
            engine, adaptive_execution_selector=adaptive, quality_gate_runner=gate_runner, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.gate_result is not None and result.gate_result.passed
        assert result.review_result is not None  # the review step still ran, now adaptively (Slice 19)

    def test_no_development_on_preflight_failure(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        adaptive = FakeAdaptiveSelector(error=NoReliableRecommendationError("no event"))
        engine = FakeExecutionEngine()
        manager = _adaptive_manager(project_store, handoff_store, engine, adaptive)

        with pytest.raises(NoReliableRecommendationError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert engine.requests == []
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.READY

    def test_insufficient_capability_is_fail_closed_not_waiting(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        # No worker configured reaches the required tier at all: WorkerSelector
        # would report this with EMPTY diagnostics (never diagnosable as quota).
        adaptive = FakeAdaptiveSelector(error=NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=()))
        engine = FakeExecutionEngine()
        manager = _adaptive_manager(project_store, handoff_store, engine, adaptive, wait_store=wait_store)

        with pytest.raises(NoEligibleWorkerError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert project_store.get_work_item("wi-a").status is WorkItemStatus.READY  # never WAITING
        assert wait_store.list_pending() == []

    def test_quota_exhaustion_still_yields_waiting(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        reset_at = UTC_NOW + timedelta(hours=2)
        diagnostics = (ProviderSelectionDiagnostic(provider="openai", available=False, reason="quota_exhausted", reset_at=(reset_at,)),)
        adaptive = FakeAdaptiveSelector(error=NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=diagnostics))
        engine = FakeExecutionEngine()
        manager = _adaptive_manager(project_store, handoff_store, engine, adaptive, wait_store=wait_store)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.WAITING
        assert result.wait is not None


class TestReviewSelectionIsAdaptive:
    """Slice 19: review now goes through the exact same adaptive chain as
    development — never the plain ``WorkerSelector`` + ``Worker.profile()``
    default when ``adaptive_execution_selector`` is configured."""

    def test_review_uses_adaptive_selector_with_code_review_capability_and_author_exclusion(
        self, tmp_path: Path
    ) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        alice = _alice()
        victor = _victor()
        dev_decision = _decision(worker_id=alice.worker_id, role="developer")
        review_decision = _decision(
            decision_id="decision-review-1", recommendation_id="rec-review-1",
            worker_id=victor.worker_id, provider="openai", backend="codex", role="reviewer",
            model="gpt-5.6-terra-deep", reasoning_effort="high",
        )
        adaptive = ScriptedAdaptiveSelector([
            AdaptiveSelection(worker=alice, decision=dev_decision),
            AdaptiveSelection(worker=victor, decision=review_decision),
        ])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("plain selector must not be used")),
            engine, adaptive_execution_selector=adaptive, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert len(adaptive.calls) == 2
        assert adaptive.calls[0].role == "developer"
        assert adaptive.calls[1].role == "reviewer"
        assert adaptive.calls[1].work_item_id == "wi-a"
        # author exclusion is forwarded, not bypassed
        assert adaptive.author_worker_ids == [None, alice.worker_id]
        # the review ExecutionRequest used the decision's model/reasoning,
        # never reviewer.profile()'s own default
        review_request = engine.requests[1]
        assert review_request.model == "gpt-5.6-terra-deep"
        assert review_request.reasoning_effort == "high"
        assert result.review_result is not None
        assert result.review_result.reviewer_worker_id == victor.worker_id
        assert result.review_result.reviewer_model == "gpt-5.6-terra-deep"

    def test_review_recommendation_is_independent_of_development(self, tmp_path: Path) -> None:
        """development=SIMPLE and review=COMPLEX in the same WorkItem run —
        the review tier is never copied from development's."""
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        alice = _alice()
        victor = _victor()
        dev_decision = _decision(worker_id=alice.worker_id, quality_tier=QualityTier.SIMPLE, role="developer")
        review_decision = _decision(
            decision_id="decision-review-1", recommendation_id="rec-review-1",
            worker_id=victor.worker_id, quality_tier=QualityTier.COMPLEX, role="reviewer",
        )
        adaptive = ScriptedAdaptiveSelector([
            AdaptiveSelection(worker=alice, decision=dev_decision),
            AdaptiveSelection(worker=victor, decision=review_decision),
        ])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert adaptive.calls[0].role == "developer"
        assert adaptive.calls[1].role == "reviewer"
        # Two independently-built requests with different role -> different
        # fingerprint/recommendation by construction (Slice 16); here proven
        # via the two distinct scripted decisions actually being consumed.

    def test_no_review_downgrade_and_no_fabricated_critical_profile(self, tmp_path: Path) -> None:
        """A CRITICAL review recommendation with no capable profile fails
        closed (structural incapacity) — propagates uncaught, never a
        fabricated profile, never silently downgraded, never a fake
        WAITING (this is not a WorkerSelector-diagnosed quota condition)."""
        from orchestrator.adaptive_execution import NoCapableProfileError

        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        alice = _alice()
        dev_decision = _decision(worker_id=alice.worker_id, role="developer")
        adaptive = ScriptedAdaptiveSelector([
            AdaptiveSelection(worker=alice, decision=dev_decision),
            NoCapableProfileError("victor", QualityTier.CRITICAL),
        ])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, review_store=review_store, wait_store=wait_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        with pytest.raises(NoCapableProfileError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        # Never turned into a WAITING — NoCapableProfileError is not a
        # WorkerSelector diagnostic-bearing exception.
        assert wait_store.list_pending() == []

    def test_review_quota_exhaustion_waits_never_falls_back_to_author(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        alice = _alice()
        dev_decision = _decision(worker_id=alice.worker_id, role="developer")
        reset_at = UTC_NOW + timedelta(hours=2)
        quota_error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset({"code_review"})),
            diagnostics=(_quota_diag(provider="openai", reset_at=reset_at),),
        )
        adaptive = ScriptedAdaptiveSelector([AdaptiveSelection(worker=alice, decision=dev_decision), quota_error])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="e1", task_id="wi-a", worker_id=alice.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, review_store=review_store, wait_store=wait_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.WAITING
        pending = wait_store.list_pending()
        assert len(pending) == 1 and pending[0].phase is WaitPhase.REVIEW
        # Only the developer ran — no review execution was ever attempted.
        assert len(engine.requests) == 1


# --- Slice 17 correction: adaptive resume (wait + recovery) -----------------


class ScriptedAdaptiveSelector:
    """Replays a fixed sequence of outcomes (AdaptiveSelection or Exception),
    in order — mirrors ScriptedWorkerSelector, but at the adaptive layer."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[ComplexityEstimationRequest] = []
        self.author_worker_ids: list = []

    async def select(
        self, *, estimation_request, required_capabilities, excluded_worker_ids=frozenset(),
        author_worker_id=None, force_refresh=False,
    ):
        self.calls.append(estimation_request)
        self.author_worker_ids.append(author_worker_id)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resume_decision(**overrides) -> AdaptiveExecutionDecision:
    fields = dict(
        decision_id="decision-resume-1", recommendation_id="rec-resume-1", project_id="proj-1",
        role="developer", worker_id="codex_dev_09", provider="openai", backend="codex",
        profile_id="deep", quality_tier=QualityTier.COMPLEX, model="gpt-5.6-terra-deep",
        reasoning_effort="high", created_at=UTC_NOW,
    )
    fields.update(overrides)
    return AdaptiveExecutionDecision(**fields)


class TestAdaptiveWaitResume:
    def test_resume_waiting_development_uses_recommendation_and_adaptive_selector(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        quota_error = NoEligibleWorkerError(
            WorkerSelectionRequest(minimum_quality_tier=QualityTier.COMPLEX),
            diagnostics=(_quota_diag(provider="openai", reset_at=reset_at),),
        )
        victor = _victor()  # default profile model is "gpt-5.6-terra", never used here
        decision = _resume_decision()
        adaptive = ScriptedAdaptiveSelector([quota_error, AdaptiveSelection(worker=victor, decision=decision)])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("plain selector must not be used")), engine,
            adaptive_execution_selector=adaptive, wait_store=wait_store,
            clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.WAITING

        clock_box["now"] = reset_at + timedelta(minutes=5)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.COMPLETED
        assert len(adaptive.calls) == 2  # pre-flight ran again on resume, never skipped
        assert engine.requests[0].model == "gpt-5.6-terra-deep"  # decision.model, never default_profile
        assert engine.requests[0].reasoning_effort == "high"

    def test_resume_never_uses_default_profile(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        quota_error = NoEligibleWorkerError(
            WorkerSelectionRequest(minimum_quality_tier=QualityTier.COMPLEX),
            diagnostics=(_quota_diag(reset_at=reset_at),),
        )
        # victor's OWN default profile model differs from the decision's —
        # proves the request never falls back to Worker.profile().
        victor = _victor()
        assert victor.profile().model != "gpt-5.6-terra-deep"
        decision = _resume_decision(model="gpt-5.6-terra-deep", reasoning_effort="high")
        adaptive = ScriptedAdaptiveSelector([quota_error, AdaptiveSelection(worker=victor, decision=decision)])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, wait_store=wait_store,
            clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at + timedelta(minutes=5)
        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert engine.requests[0].model == "gpt-5.6-terra-deep"

    def test_resume_still_unavailable_requeues_never_executes(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at_1 = UTC_NOW + timedelta(hours=3)
        reset_at_2 = UTC_NOW + timedelta(hours=6)
        error1 = NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=(_quota_diag(reset_at=reset_at_1),))
        error2 = NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=(_quota_diag(reset_at=reset_at_2),))
        adaptive = ScriptedAdaptiveSelector([error1, error2])
        engine = FakeExecutionEngine()
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, wait_store=wait_store,
            clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at_1 + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.WAITING
        assert second.wait.eligible_at == reset_at_2
        assert engine.requests == []

    def test_resume_with_structural_incapacity_fails_closed_not_waiting_again(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=3)
        quota_error = NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=(_quota_diag(reset_at=reset_at),))
        # On resume: config changed, no worker anywhere reaches the tier —
        # never diagnosable as quota (empty diagnostics).
        structural_error = NoEligibleWorkerError(WorkerSelectionRequest(), diagnostics=())
        adaptive = ScriptedAdaptiveSelector([quota_error, structural_error])
        engine = FakeExecutionEngine()
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, wait_store=wait_store,
            clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))
        clock_box["now"] = reset_at + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.BLOCKED
        assert wait_store.list_pending() == []


class TestAdaptiveRecoveryResume:
    def test_recovery_development_calls_preflight_and_adaptive_selector(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        victor = _victor()
        decision = _resume_decision()
        adaptive = ScriptedAdaptiveSelector([AdaptiveSelection(worker=victor, decision=decision)])
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id=victor.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert len(adaptive.calls) == 1
        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert engine.requests[0].model == decision.model
        assert engine.requests[0].reasoning_effort == decision.reasoning_effort
        assert engine.requests[0].worker.worker_id == victor.worker_id
        assert execution_store.get("exec-old").status is ExecutionStatus.RECOVERY_REQUIRED
        assert all(req.execution_id != "exec-old" for req in engine.requests)  # never reused

    def test_recovery_changed_facts_can_produce_new_fingerprint(self, tmp_path: Path) -> None:
        # A real ExecutionRecommendationStore/cache: the fingerprint is
        # recomputed from current facts on every pre-flight call — proven
        # here directly at the fingerprint level (already exercised
        # end-to-end via ScriptedAdaptiveSelector's call count elsewhere).
        from orchestrator.complexity_estimation import compute_task_fingerprint

        base = ComplexityEstimationRequest(
            project_id="proj-1", role="developer", workspace=tmp_path, objective="Do the thing",
            work_item_id="wi-a", git_sha="sha-before",
        )
        after_recovery = ComplexityEstimationRequest(
            project_id="proj-1", role="developer", workspace=tmp_path, objective="Do the thing",
            work_item_id="wi-a", git_sha="sha-after-recovery-handoff",
        )
        assert compute_task_fingerprint(base) != compute_task_fingerprint(after_recovery)

    def test_recovery_unchanged_facts_produce_same_fingerprint(self, tmp_path: Path) -> None:
        from orchestrator.complexity_estimation import compute_task_fingerprint

        def _make():
            return ComplexityEstimationRequest(
                project_id="proj-1", role="developer", workspace=tmp_path, objective="Do the thing",
                work_item_id="wi-a", git_sha="sha-1",
            )

        assert compute_task_fingerprint(_make()) == compute_task_fingerprint(_make())

    def test_recovery_no_development_on_preflight_failure(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "Do the thing"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        adaptive = ScriptedAdaptiveSelector([NoReliableRecommendationError("no event")])
        engine = FakeExecutionEngine()
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, execution_store=execution_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        with pytest.raises(NoReliableRecommendationError):
            asyncio.run(manager.run_next_work_item("mvp-1"))

        assert engine.requests == []




class TestAdaptiveReviewResume:
    """Slice 19: WaitPhase.REVIEW resume and review-phase recovery resume
    both become adaptive, mirroring Slice 17's development-resume fix —
    never a plain ``WorkerSelector`` + ``Worker.profile()`` default when
    ``adaptive_execution_selector`` is configured, and the original
    developer/author stays excluded even after a cold resume."""

    def test_review_wait_resume_uses_adaptive_selector_and_excludes_author(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        project_store.mark_work_item_waiting("wi-a")
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev-1", worker_id="claude_dev_01",
            created_at=UTC_NOW, test_results="quality_gate=PASSED", git_sha_after="sha-1",
        )
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        wait_store.create(
            wait_id="w1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            phase=WaitPhase.REVIEW, reason=WaitReason.QUOTA_RESET, eligible_at=UTC_NOW,
        )
        victor = _victor()
        review_decision = _decision(
            decision_id="decision-review-resume-1", recommendation_id="rec-review-resume-1",
            worker_id=victor.worker_id, provider="openai", backend="codex", role="reviewer",
            model="gpt-5.6-terra-deep", reasoning_effort="high",
        )
        adaptive = ScriptedAdaptiveSelector([AdaptiveSelection(worker=victor, decision=review_decision)])
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id="codex_dev_01", status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("plain selector must not be used")),
            engine, adaptive_execution_selector=adaptive, wait_store=wait_store, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert len(adaptive.calls) == 1
        assert adaptive.calls[0].role == "reviewer"
        assert adaptive.author_worker_ids == ["claude_dev_01"]  # original developer, still excluded
        assert engine.requests[0].model == "gpt-5.6-terra-deep"
        assert engine.requests[0].reasoning_effort == "high"
        assert result.review_result is not None
        assert result.review_result.reviewer_worker_id == victor.worker_id
        # A brand-new execution_id, never the interrupted one.
        assert engine.requests[0].execution_id != "exec-dev-1"

    def test_review_phase_recovery_resume_uses_adaptive_selector_and_excludes_author(self, tmp_path: Path) -> None:
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-dev-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-dev-1")
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev-1", worker_id="claude_dev_01",
            created_at=UTC_NOW, test_results="quality_gate=PASSED", git_sha_after="sha-1",
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review-old", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )  # orphaned mid-review

        victor = _victor()
        review_decision = _decision(
            decision_id="decision-review-recovery-1", recommendation_id="rec-review-recovery-1",
            worker_id=victor.worker_id, provider="openai", backend="codex", role="reviewer",
        )
        adaptive = ScriptedAdaptiveSelector([AdaptiveSelection(worker=victor, decision=review_decision)])
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id="codex_dev_01", status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("plain selector must not be used")),
            engine, adaptive_execution_selector=adaptive, execution_store=execution_store, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert len(adaptive.calls) == 1
        assert adaptive.calls[0].role == "reviewer"
        # The author is read from the last *developer*-role handoff, never
        # the orphaned reviewer's own recovery handoff (see
        # `_find_last_developer_handoff`) — still correctly excluded.
        assert adaptive.author_worker_ids == ["claude_dev_01"]
        assert all(req.execution_id != "exec-review-old" for req in engine.requests)  # never reused

    def test_review_recovery_resume_can_change_reviewer(self, tmp_path: Path) -> None:
        """The previous reviewer may be reused or changed on recovery —
        never an arbitrary new rule pinning it to the same worker."""
        project_store, handoff_store = _stores(tmp_path)
        _seed(project_store, tmp_path, **{"wi-a": {"title": "A"}})
        execution_store = _execution_store(tmp_path)
        execution_store.create(
            execution_id="exec-dev-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-dev-1")
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev-1", worker_id="claude_dev_01",
            created_at=UTC_NOW, test_results="quality_gate=PASSED", git_sha_after="sha-1",
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review-old", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )  # the PREVIOUS reviewer was victor/codex_dev_01

        # This time a DIFFERENT worker is adaptively selected as reviewer.
        third_reviewer = Worker.with_single_profile(
            worker_id="claude_dev_99", display_name="Third", provider="anthropic",
            backend="claude_code", model="opus", capabilities=frozenset({"code_review"}),
        )
        review_decision = _decision(
            decision_id="decision-review-changed-1", recommendation_id="rec-review-changed-1",
            worker_id=third_reviewer.worker_id, provider="anthropic", backend="claude_code", role="reviewer",
        )
        adaptive = ScriptedAdaptiveSelector([AdaptiveSelection(worker=third_reviewer, decision=review_decision)])
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        engine = FakeExecutionEngine(
            result=_execution_result(execution_id="whatever", task_id="wi-a", worker_id=third_reviewer.worker_id, status=ExecutionStatus.SUCCEEDED)
        )
        manager = MVPManager(
            project_store, handoff_store, FakeWorkerSelector(error=AssertionError("unused")), engine,
            adaptive_execution_selector=adaptive, execution_store=execution_store, review_store=review_store,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
            workflow_mode=WorkflowMode.GOVERNED_FULL,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.review_result.reviewer_worker_id == "claude_dev_99"  # changed, never pinned
