"""Tests for MVPManager (Phase 1 / Slice 7).

All tests are offline: fake WorkerSelector/RalphExecutionEngine stand in
for the real ones — no subprocess, no network, no real
Claude/Codex/Ralph/Git invocation anywhere in this file.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.handoff import HandoffStore
from orchestrator.mvp_manager import REVIEW_CAPABILITY, MVPManager
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent, RalphLaunchError
from orchestrator.review import ReviewPolicy, ReviewStatus, ReviewStore
from orchestrator.validation import (
    QualityGateRunner,
    ValidationCommand,
    ValidationKind,
    ValidationStore,
)
from orchestrator.worker_selector import NoEligibleWorkerError, Worker

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
PY = sys.executable


def _alice() -> Worker:
    return Worker(
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
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert "lint=passed" in result.handoff.test_results


def _victor() -> Worker:
    return Worker(
        worker_id="codex_dev_01", display_name="Victor", provider="openai",
        backend="codex", model="gpt-5.6-terra", capabilities=frozenset({"reviewer"}),
    )


def _chloe() -> Worker:
    return Worker(
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
