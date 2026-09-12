"""ReleaseManager — evaluates the release gate and builds the activity report.

This is the composition root for Slice 10: it never runs a worker, never
selects one, and never mutates git — it only *reads* the stores Slices
5/7/8/9 already built (``ProjectStateStore``, ``ExecutionStore``,
``ValidationStore``, ``ReviewStore``, ``HandoffStore``), builds a small,
explicit set of :class:`~orchestrator.release.ReleaseCheck` objects, and —
only if every one of them holds — transitions the MVP to ``RELEASED`` and
persists a durable :class:`~orchestrator.activity_report.ActivityReport`.

Kept deliberately separate from :class:`~orchestrator.mvp_manager.MVPManager`:
that class drives WorkItems through development/quality-gate/review: this
class only evaluates whether the MVP, as a whole, is release-worthy, once
those WorkItems are believed to be done. Merging the two would turn
MVPManager into exactly the kind of god object this codebase avoids.

FAIL-CLOSED, always:

- Every WorkItem of the MVP must be ``COMPLETED`` — anything else
  (PLANNED/READY/RUNNING/REVIEWING/NEEDS_REWORK/FAILED/BLOCKED) fails the
  release outright.
- If the project has any configured validation commands, every COMPLETED
  WorkItem must have a ``PASSED`` quality-gate result — a *missing* gate
  result for a COMPLETED WorkItem is never treated as passing.
- Review is only checked when the caller explicitly says it is required
  for this release (``review_required=True``) — this module never
  invents that policy from the mere presence/absence of review data (see
  ROADMAP.md, Slice 10 task notes: no opaque heuristic). When required,
  every COMPLETED WorkItem's *latest* review must be APPROVED.
- No execution tied to any WorkItem of this MVP may still be RUNNING.
- ``exit_code`` is never consulted for this decision — only the
  already-normalized ``WorkItemStatus``/``ValidationStatus``/``ReviewStatus``
  values the earlier slices already computed correctly.

A release attempt is never skipped or overwritten: each call gets its own
``release_id``, persisted via ``ReleaseStore`` regardless of outcome. A
failed gate leaves the MVP in ``VALIDATING`` (correctable — more rework,
another attempt later), never a dead end.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.project_state import MVP, ProjectStateStore, WorkItemStatus
from orchestrator.activity_report import (
    ActivityReport,
    ActivityReportStore,
    ActivitySummary,
    ExecutionSummary,
    HandoffSummary,
    Incident,
    ReleaseCheckSummary,
    ReviewSummary,
    ValidationSummary,
    WorkItemSummary,
)
from orchestrator.release import (
    ReleaseCheck,
    ReleaseGateStatus,
    ReleaseRecord,
    ReleaseStore,
)
from orchestrator.review import ReviewStatus, ReviewStore
from orchestrator.validation import ValidationStore

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

_INCOMPLETE_STATUSES = (
    WorkItemStatus.PLANNED, WorkItemStatus.READY, WorkItemStatus.RUNNING,
    WorkItemStatus.REVIEWING, WorkItemStatus.NEEDS_REWORK, WorkItemStatus.FAILED,
    WorkItemStatus.BLOCKED,
)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _git_head_sha(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


class ReleaseManager:
    """Evaluates the release gate for an MVP and builds its activity report."""

    def __init__(
        self,
        project_state_store: ProjectStateStore,
        execution_store: ExecutionStore,
        validation_store: ValidationStore,
        review_store: ReviewStore,
        handoff_store: HandoffStore,
        release_store: ReleaseStore,
        activity_report_store: ActivityReportStore,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._project_state_store = project_state_store
        self._execution_store = execution_store
        self._validation_store = validation_store
        self._review_store = review_store
        self._handoff_store = handoff_store
        self._release_store = release_store
        self._activity_report_store = activity_report_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory

    def attempt_release(
        self, project_id: str, mvp_id: str, *, review_required: bool = False
    ) -> ReleaseRecord:
        """Evaluates the release gate once and persists the attempt.

        Never raises for a failed/erroring gate — that is a legitimate,
        persisted outcome. Only genuinely unexpected caller errors (e.g.
        an unknown ``mvp_id``) propagate, since those are not "the gate
        failed", they are "this call was malformed".
        """
        started_at = self._clock()
        mvp = self._project_state_store.mark_mvp_validating(mvp_id)
        project = self._project_state_store.get_project(project_id)
        git_sha = _git_head_sha(project.workspace)

        try:
            checks = self._build_checks(project_id, mvp_id, review_required=review_required)
            status = ReleaseGateStatus.PASSED if all(c.passed for c in checks) else ReleaseGateStatus.FAILED
        except Exception as exc:  # noqa: BLE001 - an evaluation error is a real, distinct outcome
            checks = (
                ReleaseCheck(
                    check_id="release-evaluation", passed=False,
                    summary=f"release gate evaluation failed: {exc}",
                ),
            )
            status = ReleaseGateStatus.ERROR

        release_id = self._id_factory()
        report_id: str | None = None

        if status is ReleaseGateStatus.PASSED:
            report = self._build_activity_report(
                release_id=release_id, project_id=project_id, mvp=mvp, git_sha=git_sha, checks=checks,
            )
            self._activity_report_store.record(report)
            report_id = report.report_id
            self._project_state_store.mark_mvp_released(mvp_id)

        release = ReleaseRecord(
            release_id=release_id, project_id=project_id, mvp_id=mvp_id,
            started_at=started_at, finished_at=self._clock(), status=status,
            checks=checks, git_sha=git_sha, report_id=report_id,
        )
        self._release_store.record(release)
        return release

    def _build_checks(
        self, project_id: str, mvp_id: str, *, review_required: bool
    ) -> tuple[ReleaseCheck, ...]:
        work_items = self._project_state_store.list_work_items(mvp_id)

        incomplete = [wi.work_item_id for wi in work_items if wi.status is not WorkItemStatus.COMPLETED]
        all_completed_check = ReleaseCheck(
            check_id="all-work-items-completed",
            passed=not incomplete,
            summary=(
                f"{len(work_items) - len(incomplete)}/{len(work_items)} work items completed"
                if work_items else "no work items defined for this MVP"
            ),
            related_ids=tuple(incomplete),
        )

        completed_ids = [wi.work_item_id for wi in work_items if wi.status is WorkItemStatus.COMPLETED]

        configured_commands = self._validation_store.get_project_commands(project_id)
        if configured_commands:
            missing_or_failed = [
                work_item_id for work_item_id in completed_ids
                if not self._has_passed_gate(work_item_id)
            ]
            gate_check = ReleaseCheck(
                check_id="quality-gates-satisfied",
                passed=not missing_or_failed,
                summary=(
                    f"{len(completed_ids) - len(missing_or_failed)}/{len(completed_ids)} completed "
                    "work items have a passed quality gate"
                ),
                related_ids=tuple(missing_or_failed),
            )
        else:
            gate_check = ReleaseCheck(
                check_id="quality-gates-satisfied", passed=True,
                summary="no validation commands configured for this project",
            )

        if review_required:
            missing_or_unapproved = [
                work_item_id for work_item_id in completed_ids
                if not self._has_approved_review(work_item_id)
            ]
            review_check = ReleaseCheck(
                check_id="reviews-approved",
                passed=not missing_or_unapproved,
                summary=(
                    f"{len(completed_ids) - len(missing_or_unapproved)}/{len(completed_ids)} completed "
                    "work items have an approved review"
                ),
                related_ids=tuple(missing_or_unapproved),
            )
        else:
            review_check = ReleaseCheck(
                check_id="reviews-approved", passed=True,
                summary="review not required for this release",
            )

        dangling = [
            record.execution_id
            for wi in work_items
            for record in self._execution_store.list_for_task(wi.work_item_id)
            if record.status is ExecutionStatus.RUNNING
        ]
        running_check = ReleaseCheck(
            check_id="no-dangling-running-executions",
            passed=not dangling,
            summary=(
                "no dangling RUNNING executions" if not dangling
                else f"{len(dangling)} execution(s) still RUNNING"
            ),
            related_ids=tuple(dangling),
        )

        return (all_completed_check, gate_check, review_check, running_check)

    def _has_passed_gate(self, work_item_id: str) -> bool:
        gate = self._validation_store.latest_gate_result_for_work_item(work_item_id)
        return gate is not None and gate.passed

    def _has_approved_review(self, work_item_id: str) -> bool:
        review = self._review_store.latest_for_work_item(work_item_id)
        return review is not None and review.status is ReviewStatus.APPROVED

    def _build_activity_report(
        self, *, release_id: str, project_id: str, mvp: MVP, git_sha: str | None,
        checks: tuple[ReleaseCheck, ...],
    ) -> ActivityReport:
        work_items = self._project_state_store.list_work_items(mvp.mvp_id)

        work_item_summaries = tuple(
            WorkItemSummary(
                work_item_id=wi.work_item_id, title=wi.title, status=wi.status.value,
                dependencies=tuple(sorted(wi.dependencies)), acceptance_criteria=wi.acceptance_criteria,
            )
            for wi in work_items
        )

        executions: list[ExecutionSummary] = []
        for wi in work_items:
            for record in self._execution_store.list_for_task(wi.work_item_id):
                executions.append(
                    ExecutionSummary(
                        execution_id=record.execution_id, work_item_id=wi.work_item_id,
                        worker_id=record.worker_id, provider=record.provider, backend=record.backend,
                        model=record.model, role=record.role, started_at=record.started_at,
                        status=record.status.value, reasoning_effort=record.reasoning_effort,
                        finished_at=record.finished_at, exit_code=record.exit_code,
                        git_sha_before=record.git_sha_before, git_sha_after=record.git_sha_after,
                        provider_session_id=record.provider_session_id, ralph_loop_id=record.ralph_loop_id,
                    )
                )

        validations: list[ValidationSummary] = []
        for wi in work_items:
            for result in self._validation_store.list_results_for_work_item(wi.work_item_id):
                validations.append(
                    ValidationSummary(
                        validation_run_id=result.validation_run_id, work_item_id=wi.work_item_id,
                        validation_id=result.validation_id, kind=result.kind.value,
                        required=result.required, status=result.status.value,
                        duration_ms=result.duration_ms, exit_code=result.exit_code,
                        git_sha=result.git_sha,
                    )
                )

        reviews: list[ReviewSummary] = []
        for wi in work_items:
            for cycle_number, review in enumerate(
                self._review_store.list_for_work_item(wi.work_item_id), start=1
            ):
                reviews.append(
                    ReviewSummary(
                        review_id=review.review_id, work_item_id=wi.work_item_id,
                        author_worker_id=review.author_worker_id, status=review.status.value,
                        cycle_number=cycle_number, reviewer_worker_id=review.reviewer_worker_id,
                        reviewer_provider=review.reviewer_provider, reviewer_model=review.reviewer_model,
                        findings_summary=tuple(f.summary for f in review.findings),
                        git_sha_reviewed=review.git_sha_reviewed,
                    )
                )

        handoffs: list[HandoffSummary] = []
        for wi in work_items:
            for handoff in self._handoff_store.list_for_work_item(wi.work_item_id):
                handoffs.append(
                    HandoffSummary(
                        handoff_id=handoff.handoff_id, work_item_id=wi.work_item_id,
                        next_action=handoff.next_action,
                    )
                )

        incidents: list[Incident] = []
        for wi in work_items:
            if wi.status is WorkItemStatus.BLOCKED:
                incidents.append(Incident(work_item_id=wi.work_item_id, kind="blocked", summary=wi.blocked_reason or "blocked"))
        for e in executions:
            if e.status in ("failed", "interrupted"):
                incidents.append(
                    Incident(
                        work_item_id=e.work_item_id, kind=f"execution_{e.status}",
                        summary=f"execution {e.execution_id} {e.status}",
                    )
                )
        for v in validations:
            if v.required and v.status in ("failed", "error", "timeout"):
                incidents.append(
                    Incident(
                        work_item_id=v.work_item_id, kind="quality_gate_failed",
                        summary=f"{v.validation_id} {v.status}",
                    )
                )
        for r in reviews:
            if r.status in ("rejected", "error", "interrupted"):
                incidents.append(
                    Incident(work_item_id=r.work_item_id, kind=f"review_{r.status}", summary=f"review {r.review_id} {r.status}")
                )

        workers_used = tuple(sorted({e.worker_id for e in executions}))
        providers_used = tuple(sorted({e.provider for e in executions}))
        failure_count = sum(1 for e in executions if e.status == "failed")
        interruption_count = sum(1 for e in executions if e.status == "interrupted")
        rework_count = sum(1 for r in reviews if r.status == "rejected")

        duration_seconds = None
        finished_ats = [e.finished_at for e in executions if e.finished_at is not None]
        if executions and finished_ats:
            duration_seconds = (max(finished_ats) - min(e.started_at for e in executions)).total_seconds()

        summary = ActivitySummary(
            work_item_count=len(work_items), execution_count=len(executions),
            validation_count=len(validations), review_count=len(reviews),
            rework_count=rework_count, failure_count=failure_count,
            interruption_count=interruption_count, workers_used=workers_used,
            providers_used=providers_used, duration_seconds=duration_seconds,
        )

        return ActivityReport(
            report_id=self._id_factory(), project_id=project_id, mvp_id=mvp.mvp_id,
            release_id=release_id, generated_at=self._clock(), mvp_objective=mvp.objective,
            mvp_final_status="released", summary=summary,
            mvp_acceptance_criteria=mvp.acceptance_criteria, git_sha=git_sha,
            work_items=work_item_summaries, executions=tuple(executions),
            validations=tuple(validations), reviews=tuple(reviews), handoffs=tuple(handoffs),
            incidents=tuple(incidents),
            gate_checks=tuple(
                ReleaseCheckSummary(check_id=c.check_id, passed=c.passed, summary=c.summary)
                for c in checks
            ),
        )
