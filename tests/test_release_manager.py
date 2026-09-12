"""Tests for ReleaseManager (Phase 1 / Slice 10).

All tests are offline: real sqlite-backed stores seeded directly with
controlled facts (bypassing MVPManager for focus/speed) — no network, no
subprocess beyond the local `git` binary for SHA capture, no
Claude/Codex/Ralph invocation anywhere in this file.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.activity_report import ActivityReportStore
from orchestrator.execution_store import ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.project_state import ProjectStateStore, WorkItemStatus, MVPStatus
from orchestrator.release import ReleaseGateStatus, ReleaseStore
from orchestrator.release_manager import ReleaseManager
from orchestrator.review import ReviewFinding, ReviewRecord, ReviewStatus, ReviewStore
from orchestrator.validation import (
    ValidationCommand,
    ValidationKind,
    ValidationResult,
    ValidationStatus,
    ValidationStore,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _stores(tmp_path: Path):
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3", clock=lambda: UTC_NOW)
    validation_store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
    review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
    handoff_store = HandoffStore(tmp_path / "handoff.sqlite3", clock=lambda: UTC_NOW)
    release_store = ReleaseStore(tmp_path / "release.sqlite3", clock=lambda: UTC_NOW)
    activity_store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)
    return project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store


def _manager(stores, **kwargs) -> ReleaseManager:
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return ReleaseManager(*stores, clock=lambda: UTC_NOW, id_factory=id_factory, **kwargs)


def _seed(project_store: ProjectStateStore, tmp_path: Path, work_item_ids: list[str]) -> None:
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it", acceptance_criteria=("works",))
    for wid in work_item_ids:
        project_store.create_work_item(work_item_id=wid, mvp_id="mvp-1", title=f"Task {wid}")
    project_store.mark_mvp_running("mvp-1")


def _complete(project_store: ProjectStateStore, work_item_id: str) -> None:
    project_store.refresh_readiness("mvp-1")
    project_store.mark_work_item_running(work_item_id)
    project_store.mark_work_item_completed(work_item_id)


def _record_execution(
    execution_store: ExecutionStore, *, execution_id: str, task_id: str, worker_id: str = "claude_dev_01",
    status=None, provider="anthropic", backend="claude_code", model="sonnet",
) -> None:
    from orchestrator.execution_store import ExecutionStatus

    execution_store.create(
        execution_id=execution_id, task_id=task_id, worker_id=worker_id, provider=provider,
        backend=backend, model=model, role="developer", started_at=UTC_NOW,
    )
    if status is ExecutionStatus.SUCCEEDED:
        execution_store.mark_succeeded(execution_id, exit_code=0, finished_at=UTC_NOW)
    elif status is ExecutionStatus.FAILED:
        execution_store.mark_failed(execution_id, exit_code=1, finished_at=UTC_NOW)
    # status=None (or RUNNING) -> leave RUNNING, deliberately.


def _configure_validation(validation_store: ValidationStore, *, required: bool = True) -> None:
    validation_store.set_project_commands(
        "proj-1",
        [ValidationCommand(validation_id="unit-tests", kind=ValidationKind.UNIT_TEST, argv=("true",), required=required)],
    )


def _record_gate(
    validation_store: ValidationStore, *, work_item_id: str, run_id: str,
    status: ValidationStatus = ValidationStatus.PASSED, required: bool = True,
) -> None:
    result = ValidationResult(
        validation_run_id=run_id, validation_id="unit-tests", kind=ValidationKind.UNIT_TEST,
        required=required, argv=("true",), status=status, started_at=UTC_NOW, finished_at=UTC_NOW,
        exit_code=0 if status is ValidationStatus.PASSED else 1,
    )
    validation_store.record_result(run_id, "proj-1", result, work_item_id=work_item_id, git_sha="deadbee")


def _record_review(
    review_store: ReviewStore, *, work_item_id: str, review_id: str,
    status: ReviewStatus = ReviewStatus.APPROVED, author_worker_id: str = "claude_dev_01",
    reviewer_worker_id: str | None = "codex_dev_01",
) -> None:
    review_store.record(
        ReviewRecord(
            review_id=review_id, project_id="proj-1", mvp_id="mvp-1", work_item_id=work_item_id,
            author_execution_id="exec-1", author_worker_id=author_worker_id,
            started_at=UTC_NOW, finished_at=UTC_NOW, status=status,
            reviewer_execution_id="exec-2" if reviewer_worker_id else None,
            reviewer_worker_id=reviewer_worker_id, reviewer_provider="openai" if reviewer_worker_id else None,
            reviewer_model="gpt-5.6-terra" if reviewer_worker_id else None,
            findings=() if status is ReviewStatus.APPROVED else (ReviewFinding(finding_id="f-1", summary="fix this"),),
        )
    )


class TestWorkItemCompletionCheck:
    def test_all_completed_passes_release(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.PASSED

    @pytest.mark.parametrize(
        "setup_status",
        ["ready", "running", "needs_rework", "failed", "blocked"],
    )
    def test_incomplete_work_item_refuses_release(self, tmp_path: Path, setup_status: str) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])

        if setup_status == "ready":
            project_store.refresh_readiness("mvp-1")
        elif setup_status == "running":
            project_store.refresh_readiness("mvp-1")
            project_store.mark_work_item_running("wi-a")
        elif setup_status == "needs_rework":
            project_store.refresh_readiness("mvp-1")
            project_store.mark_work_item_running("wi-a")
            project_store.mark_work_item_reviewing("wi-a")
            project_store.mark_work_item_needs_rework("wi-a")
        elif setup_status == "failed":
            project_store.refresh_readiness("mvp-1")
            project_store.mark_work_item_running("wi-a")
            project_store.mark_work_item_failed("wi-a")
        elif setup_status == "blocked":
            project_store.refresh_readiness("mvp-1")
            project_store.mark_work_item_running("wi-a")
            project_store.mark_work_item_reviewing("wi-a")
            project_store.mark_work_item_blocked("wi-a", reason="test")

        manager = _manager((project_store, *rest))
        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.FAILED
        completion_check = next(c for c in release.checks if c.check_id == "all-work-items-completed")
        assert completion_check.passed is False
        assert "wi-a" in completion_check.related_ids


class TestQualityGateCheck:
    def test_required_validation_passed_allows_release(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _configure_validation(validation_store, required=True)
        _record_gate(validation_store, work_item_id="wi-a", run_id="run-1", status=ValidationStatus.PASSED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.PASSED

    def test_required_validation_failed_refuses_release(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _configure_validation(validation_store, required=True)
        _record_gate(validation_store, work_item_id="wi-a", run_id="run-1", status=ValidationStatus.FAILED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.FAILED
        gate_check = next(c for c in release.checks if c.check_id == "quality-gates-satisfied")
        assert gate_check.passed is False

    def test_missing_gate_result_fails_closed(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _configure_validation(validation_store, required=True)
        # No gate result ever recorded for wi-a.
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.FAILED
        gate_check = next(c for c in release.checks if c.check_id == "quality-gates-satisfied")
        assert "wi-a" in gate_check.related_ids

    def test_optional_validation_failure_does_not_block_release(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _configure_validation(validation_store, required=False)
        _record_gate(validation_store, work_item_id="wi-a", run_id="run-1", status=ValidationStatus.FAILED, required=False)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.PASSED

    def test_no_configured_commands_trivially_passes_gate_check(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        release = manager.attempt_release("proj-1", "mvp-1")

        gate_check = next(c for c in release.checks if c.check_id == "quality-gates-satisfied")
        assert gate_check.passed is True


class TestReviewCheck:
    def test_approved_review_allows_release_when_required(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_review(review_store, work_item_id="wi-a", review_id="rev-1", status=ReviewStatus.APPROVED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1", review_required=True)

        assert release.status is ReleaseGateStatus.PASSED

    def test_rejected_review_refuses_release_when_required(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_review(review_store, work_item_id="wi-a", review_id="rev-1", status=ReviewStatus.REJECTED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1", review_required=True)

        assert release.status is ReleaseGateStatus.FAILED
        review_check = next(c for c in release.checks if c.check_id == "reviews-approved")
        assert review_check.passed is False

    def test_missing_review_refuses_release_when_required(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1", review_required=True)

        assert release.status is ReleaseGateStatus.FAILED

    def test_review_not_required_is_skipped_entirely(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        release = manager.attempt_release("proj-1", "mvp-1", review_required=False)

        review_check = next(c for c in release.checks if c.check_id == "reviews-approved")
        assert review_check.passed is True
        assert release.status is ReleaseGateStatus.PASSED


class TestDanglingExecutionCheck:
    def test_running_execution_refuses_release(self, tmp_path: Path) -> None:
        from orchestrator.execution_store import ExecutionStatus

        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_execution(execution_store, execution_id="exec-dangling", task_id="wi-a", status=None)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.status is ReleaseGateStatus.FAILED
        running_check = next(c for c in release.checks if c.check_id == "no-dangling-running-executions")
        assert running_check.passed is False
        assert "exec-dangling" in running_check.related_ids


class TestMVPStatusTransitions:
    def test_passed_gate_releases_mvp(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        manager.attempt_release("proj-1", "mvp-1")

        assert project_store.get_mvp("mvp-1").status is MVPStatus.RELEASED

    def test_failed_gate_leaves_mvp_validating_not_released(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        # wi-a stays PLANNED/READY: never completed.
        manager = _manager((project_store, *rest))

        manager.attempt_release("proj-1", "mvp-1")

        assert project_store.get_mvp("mvp-1").status is MVPStatus.VALIDATING


class TestReleaseRecordPersistence:
    def test_release_record_is_persisted(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        fetched = release_store.get(release.release_id)
        assert fetched == release

    def test_multiple_attempts_for_same_mvp_are_kept(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        first = manager.attempt_release("proj-1", "mvp-1")  # fails: wi-a not completed
        _complete(project_store, "wi-a")
        second = manager.attempt_release("proj-1", "mvp-1")  # passes

        assert first.release_id != second.release_id
        assert first.status is ReleaseGateStatus.FAILED
        assert second.status is ReleaseGateStatus.PASSED
        assert len(release_store.list_for_mvp("mvp-1")) == 2

    def test_latest_for_mvp_is_correct(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        manager.attempt_release("proj-1", "mvp-1")
        _complete(project_store, "wi-a")
        second = manager.attempt_release("proj-1", "mvp-1")

        assert release_store.latest_for_mvp("mvp-1").release_id == second.release_id


class TestActivityReportGeneration:
    def test_report_generated_only_after_successful_release(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_execution(execution_store, execution_id="exec-1", task_id="wi-a", status=None)
        execution_store.mark_succeeded("exec-1", exit_code=0, finished_at=UTC_NOW)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.report_id is not None
        report = activity_store.get(release.report_id)
        assert report.mvp_id == "mvp-1"

    def test_no_report_fabricated_after_failed_release(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        # wi-a not completed -> gate fails.
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.report_id is None
        assert activity_store.latest_for_mvp("mvp-1") is None

    def test_report_survives_restart(self, tmp_path: Path) -> None:
        project_db, execution_db, validation_db, review_db, handoff_db, release_db, activity_db = [
            tmp_path / name for name in
            ("project.sqlite3", "execution.sqlite3", "validation.sqlite3", "review.sqlite3", "handoff.sqlite3", "release.sqlite3", "activity.sqlite3")
        ]
        project_store = ProjectStateStore(project_db, clock=lambda: UTC_NOW)
        execution_store = ExecutionStore(execution_db, clock=lambda: UTC_NOW)
        validation_store = ValidationStore(validation_db, clock=lambda: UTC_NOW)
        review_store = ReviewStore(review_db, clock=lambda: UTC_NOW)
        handoff_store = HandoffStore(handoff_db, clock=lambda: UTC_NOW)
        release_store = ReleaseStore(release_db, clock=lambda: UTC_NOW)
        activity_store = ActivityReportStore(activity_db, clock=lambda: UTC_NOW)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))
        release = manager.attempt_release("proj-1", "mvp-1")
        activity_store.close()

        reopened = ActivityReportStore(activity_db, clock=lambda: UTC_NOW)
        report = reopened.get(release.report_id)
        assert report.mvp_id == "mvp-1"

    def test_report_contains_worker_provider_model(self, tmp_path: Path) -> None:
        from orchestrator.execution_store import ExecutionStatus

        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_execution(execution_store, execution_id="exec-1", task_id="wi-a", worker_id="codex_dev_01", provider="openai", backend="codex", model="gpt-5.6-terra", status=ExecutionStatus.SUCCEEDED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")
        report = activity_store.get(release.report_id)

        assert report.executions[0].worker_id == "codex_dev_01"
        assert report.executions[0].provider == "openai"
        assert report.executions[0].model == "gpt-5.6-terra"

    def test_report_contains_validations(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _configure_validation(validation_store, required=True)
        _record_gate(validation_store, work_item_id="wi-a", run_id="run-1", status=ValidationStatus.PASSED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")
        report = activity_store.get(release.report_id)

        assert len(report.validations) == 1
        assert report.validations[0].validation_id == "unit-tests"

    def test_report_contains_reviews_and_findings(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        _record_review(review_store, work_item_id="wi-a", review_id="rev-1", status=ReviewStatus.APPROVED)
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1", review_required=True)
        report = activity_store.get(release.report_id)

        assert len(report.reviews) == 1
        assert report.reviews[0].reviewer_worker_id == "codex_dev_01"

    def test_report_contains_handoffs(self, tmp_path: Path) -> None:
        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        handoff_store.create(
            handoff_id="ho-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Task wi-a", next_action="proceed", created_at=UTC_NOW,
        )
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")
        report = activity_store.get(release.report_id)

        assert len(report.handoffs) == 1
        assert report.handoffs[0].handoff_id == "ho-1"

    def test_report_and_summary_count_failures_and_reworks(self, tmp_path: Path) -> None:
        from orchestrator.execution_store import ExecutionStatus

        project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _record_execution(execution_store, execution_id="exec-1", task_id="wi-a", status=ExecutionStatus.FAILED)
        _record_review(review_store, work_item_id="wi-a", review_id="rev-1", status=ReviewStatus.REJECTED)
        _complete(project_store, "wi-a")  # force-complete for the purpose of this summary-count test
        manager = _manager((project_store, execution_store, validation_store, review_store, handoff_store, release_store, activity_store))

        release = manager.attempt_release("proj-1", "mvp-1")
        report = activity_store.get(release.report_id)

        assert report.summary.failure_count == 1
        assert report.summary.rework_count == 1
        assert len(report.incidents) >= 2


class TestGitShaCapture:
    def test_git_sha_captured_read_only(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)

        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.git_sha is not None
        assert len(release.git_sha) == 40

        # No mutation happened: still exactly one commit, same branch tip.
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True)
        assert len(log.stdout.strip().splitlines()) == 1

    def test_git_sha_none_for_non_git_workspace(self, tmp_path: Path) -> None:
        project_store, *rest = _stores(tmp_path)
        _seed(project_store, tmp_path, ["wi-a"])
        _complete(project_store, "wi-a")
        manager = _manager((project_store, *rest))

        release = manager.attempt_release("proj-1", "mvp-1")

        assert release.git_sha is None


class TestNoForbiddenBehavior:
    def test_no_git_mutation_commands_in_source(self) -> None:
        import inspect

        from orchestrator import release_manager as module

        source = inspect.getsource(module)
        for forbidden in ('"add"', '"commit"', '"checkout"', '"branch"', '"merge"', '"reset"', '"clean"'):
            assert forbidden not in source

    def test_no_claude_codex_ralph_or_llm_involvement(self) -> None:
        import inspect

        from orchestrator import release_manager as module

        source = inspect.getsource(module)
        for forbidden in ("ClaudeCodeAdapter", "CodexAdapter", "RalphExecutionEngine", "WorkerSelector", '"ralph"'):
            assert forbidden not in source
