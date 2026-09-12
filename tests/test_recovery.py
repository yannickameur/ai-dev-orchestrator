"""Tests for RecoveryCoordinator (Phase 1 / Slice 11b).

All tests are offline: real sqlite3 files under pytest's ``tmp_path``
(ExecutionStore/HandoffStore/ProjectStateStore/ReviewStore), an injectable
clock, no network, no subprocess, no Claude/Codex/Ralph invocation, no
real sleep anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.recovery import RECOVERY_NEXT_ACTION, RECOVERY_OPEN_ISSUE, RecoveryCoordinator
from orchestrator.review import ReviewFinding, ReviewRecord, ReviewStatus, ReviewStore

UTC_NOW = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)


def _stores(tmp_path: Path):
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3", clock=lambda: UTC_NOW)
    handoff_store = HandoffStore(tmp_path / "handoff.sqlite3", clock=lambda: UTC_NOW)
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    return execution_store, handoff_store, project_store


def _seed(project_store: ProjectStateStore, tmp_path: Path) -> None:
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")
    project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")


def _coordinator(execution_store, handoff_store, project_store, review_store=None) -> RecoveryCoordinator:
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"recovery-id-{counter['n']}"

    return RecoveryCoordinator(
        execution_store, handoff_store, project_store, review_store,
        clock=lambda: UTC_NOW, id_factory=id_factory,
    )


class TestOrphanedRunningExecution:
    def test_orphaned_running_dev_execution_is_marked_recovery_required(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )  # never finalized — simulates a crash mid-run

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert updated is not None
        assert updated.status is WorkItemStatus.RECOVERY_REQUIRED
        assert execution_store.get("exec-1").status is ExecutionStatus.RECOVERY_REQUIRED

    def test_orphaned_execution_is_never_relaunched_or_flipped_back_to_running(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        coordinator = _coordinator(execution_store, handoff_store, project_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        # exec-1 is now terminal (RECOVERY_REQUIRED) — it must stay that
        # way forever; no code path here ever calls anything that could
        # move it back to RUNNING.
        assert execution_store.get("exec-1").status is ExecutionStatus.RECOVERY_REQUIRED

    def test_old_execution_id_stays_in_history(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-orphaned", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        coordinator = _coordinator(execution_store, handoff_store, project_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert execution_store.get("exec-orphaned").execution_id == "exec-orphaned"
        assert execution_store.list_for_task("wi-a")[0].execution_id == "exec-orphaned"

    def test_non_running_non_reviewing_work_item_is_left_alone(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")  # wi-a is READY, not RUNNING/REVIEWING

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(
            project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a")
        )

        assert updated is None
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.READY

    def test_running_work_item_with_no_matching_execution_is_left_alone(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        # No ExecutionRecord created at all for wi-a — nothing to reconcile.

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(
            project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a")
        )

        assert updated is None

    def test_reconcile_mvp_scans_all_work_items(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.create_work_item(work_item_id="wi-b", mvp_id="mvp-1", title="B")
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_running("wi-b")
        for wid in ("wi-a", "wi-b"):
            execution_store.create(
                execution_id=f"exec-{wid}", task_id=wid, worker_id="claude_dev_01",
                provider="anthropic", backend="claude_code", model="sonnet", role="developer",
            )

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        reconciled = coordinator.reconcile_mvp("mvp-1")

        assert {wi.work_item_id for wi in reconciled} == {"wi-a", "wi-b"}
        assert project_store.get_work_item("wi-a").status is WorkItemStatus.RECOVERY_REQUIRED
        assert project_store.get_work_item("wi-b").status is WorkItemStatus.RECOVERY_REQUIRED


class TestInterruptedExecutionStaysHistorical:
    def test_already_interrupted_execution_is_not_mutated(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_interrupted("exec-1")

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert updated is not None
        assert updated.status is WorkItemStatus.RECOVERY_REQUIRED
        # The execution itself is untouched: still INTERRUPTED, never RUNNING again.
        assert execution_store.get("exec-1").status is ExecutionStatus.INTERRUPTED

    def test_already_recovery_required_or_terminal_execution_is_a_no_op(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-1")
        # WorkItem status manually left at RUNNING to simulate stale data
        # (the WorkItem-side transition failed to apply for some reason);
        # a SUCCEEDED execution is not something this module re-interprets.

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert updated is None


class TestRecoveryHandoff:
    def test_recovery_handoff_created_when_none_exists(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
            git_sha_before="abc123",
        )

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        handoff = handoff_store.latest_for_work_item("wi-a")
        assert handoff is not None
        assert handoff.execution_id == "exec-1"
        assert handoff.worker_id == "claude_dev_01"
        assert handoff.open_issues == RECOVERY_OPEN_ISSUE
        assert handoff.next_action == RECOVERY_NEXT_ACTION
        assert handoff.work_item_id == "wi-a"
        assert handoff.project_id == "proj-1"
        assert handoff.mvp_id == "mvp-1"

    def test_recovery_handoff_readable_after_restart(self, tmp_path: Path) -> None:
        execution_db = tmp_path / "execution.sqlite3"
        handoff_db = tmp_path / "handoff.sqlite3"
        project_db = tmp_path / "project.sqlite3"
        execution_store = ExecutionStore(execution_db, clock=lambda: UTC_NOW)
        handoff_store = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        project_store = ProjectStateStore(project_db, clock=lambda: UTC_NOW)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        coordinator = _coordinator(execution_store, handoff_store, project_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))
        handoff_id = handoff_store.latest_for_work_item("wi-a").handoff_id
        execution_store.close()
        handoff_store.close()
        project_store.close()

        reopened_handoffs = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        fetched = reopened_handoffs.get(handoff_id)
        assert fetched.open_issues == RECOVERY_OPEN_ISSUE

    def test_ensure_recovery_handoff_is_idempotent_for_the_same_execution(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        coordinator = _coordinator(execution_store, handoff_store, project_store)
        execution = execution_store.get("exec-1")

        first = coordinator.ensure_recovery_handoff(
            project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"), execution=execution
        )
        second = coordinator.ensure_recovery_handoff(
            project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"), execution=execution
        )

        assert first.handoff_id == second.handoff_id
        assert len(handoff_store.list_for_work_item("wi-a")) == 1

    def test_recovery_handoff_carries_forward_previous_quality_gate_result(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        # A prior successful dev handoff already carries a PASSED gate summary.
        handoff_store.create(
            handoff_id="handoff-dev", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="A", execution_id="exec-dev", worker_id="claude_dev_01",
            test_results="quality_gate=PASSED (unit-tests=passed)", next_action="proceed to review",
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        new_handoff = handoff_store.latest_for_work_item("wi-a")
        assert new_handoff.execution_id == "exec-review"
        assert new_handoff.test_results == "quality_gate=PASSED (unit-tests=passed)"

    def test_recovery_handoff_folds_in_previous_review_findings(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        review_store = ReviewStore(":memory:", clock=lambda: UTC_NOW)
        review_store.record(
            ReviewRecord(
                review_id="review-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
                author_execution_id="exec-dev", author_worker_id="claude_dev_01",
                reviewer_execution_id="exec-review-1", reviewer_worker_id="codex_dev_01",
                reviewer_provider="openai", reviewer_model="terra",
                started_at=UTC_NOW, finished_at=UTC_NOW, status=ReviewStatus.REJECTED,
                findings=(ReviewFinding(finding_id="f1", summary="off by one", severity="major"),),
            )
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-rework", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )

        coordinator = _coordinator(execution_store, handoff_store, project_store, review_store)
        coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        handoff = handoff_store.latest_for_work_item("wi-a")
        assert "off by one" in handoff.open_issues
        assert RECOVERY_OPEN_ISSUE in handoff.open_issues

    def test_no_call_to_the_interrupted_worker_needed(self, tmp_path: Path) -> None:
        # RecoveryCoordinator builds everything from persisted stores —
        # there is no worker/execution-engine dependency injected at all.
        execution_store, handoff_store, project_store = _stores(tmp_path)
        coordinator = _coordinator(execution_store, handoff_store, project_store)
        assert not hasattr(coordinator, "_worker_selector")
        assert not hasattr(coordinator, "_execution_engine")


class TestReviewPhaseRecovery:
    def test_orphaned_reviewing_work_item_is_marked_recovery_required(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_reviewing("wi-a")
        execution_store.create(
            execution_id="exec-review", task_id="wi-a", worker_id="codex_dev_01",
            provider="openai", backend="codex", model="terra", role="reviewer",
        )  # orphaned mid-review

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert updated is not None
        assert updated.status is WorkItemStatus.RECOVERY_REQUIRED
        assert updated.status is not WorkItemStatus.COMPLETED
        assert execution_store.get("exec-review").status is ExecutionStatus.RECOVERY_REQUIRED

    def test_reviewing_work_item_only_considers_reviewer_role_executions(self, tmp_path: Path) -> None:
        execution_store, handoff_store, project_store = _stores(tmp_path)
        _seed(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store.create(
            execution_id="exec-dev", task_id="wi-a", worker_id="claude_dev_01",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )
        execution_store.mark_succeeded("exec-dev")
        project_store.mark_work_item_reviewing("wi-a")
        # No reviewer-role execution recorded at all yet — nothing to reconcile.

        coordinator = _coordinator(execution_store, handoff_store, project_store)
        updated = coordinator.reconcile_work_item(project_id="proj-1", mvp_id="mvp-1", work_item=project_store.get_work_item("wi-a"))

        assert updated is None
