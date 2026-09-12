"""MVPManager — orchestrates high-level WorkItems for the current MVP.

This is the composition root for the Slice 7/8/9 cycle:

    ROADMAP (functional intent, read by a human/future agent)
      -> MVP / WorkItem (this module's input, via ProjectStateStore)
      -> WorkerSelector (existing, Slice 4: chooses a worker — for both
         development AND review; author-independence enforcement already
         lives there, never duplicated here)
      -> RalphExecutionEngine (existing, Slice 6: runs development AND
         review executions — never a direct claude/codex/ralph call)
      -> QualityGateRunner (existing, Slice 8: validates development —
         optional; a review is only ever attempted after a PASSED gate)
      -> ReviewStore (Slice 9: durable, structured review verdicts)
      -> HandoffStore (Slice 7: durable handoff, now gate- and review-aware)

Both Slice 8 (quality gates) and Slice 9 (independent review) are
deliberately minimal and opt-in, in the same way: supplying
``quality_gate_runner``/``review_store`` enables the corresponding step;
omitting either preserves the exact behavior of the slice before it. A
WorkItem is only ``COMPLETED`` when every step actually configured for
this MVPManager instance said so — development success, quality gate
PASSED (if configured), and review APPROVED (if configured).

MVPManager orchestrates WorkItems — high-level units of work — never a
second, fine-grained task queue: once a WorkItem is delegated to Ralph (via
RalphExecutionEngine), Ralph keeps its own internal orchestration. This
module re-implements none of WorkerSelector's capability/governance/quota
logic (including cross-provider review independence — that policy lives on
the injected WorkerSelector's own WorkerSelectionPolicy, Slice 4, and is
never duplicated here), and never launches `claude`/`codex`/`ralph`
directly — only through the existing `WorkerSelector`/`RalphExecutionEngine`.

Execution is strictly sequential and deterministic in this slice: exactly
one WorkItem is considered per `run_next_work_item()` call, chosen among
the READY/NEEDS_REWORK ones by ascending `work_item_id`. No parallelism.

REWORK MACHINE-CONTROLLED, NEVER A BLIND TECHNICAL RETRY:
a WorkItem moved to NEEDS_REWORK by an independent review rejection is
eligible for exactly one more development attempt, explicitly motivated by
the review's findings (attached to the next development execution's
instructions) — this is business rework, never an automatic re-run of a
crashed/timed-out execution (that remains forbidden everywhere in this
codebase). The review/rework loop is bounded by ``ReviewPolicy.
max_review_cycles``: once exhausted without approval, the WorkItem is
``BLOCKED`` with an explicit reason, and — as with every other terminal
path — its dependents are never executed (they resolve to BLOCKED via
`ProjectStateStore.refresh_readiness`, never silently skipped or re-tried).

A RUNNING/REVIEWING WorkItem is never picked up again by this module:
`run_next_work_item` only ever selects from READY/NEEDS_REWORK items, and
`refresh_readiness` never touches a WorkItem that is not PLANNED.
Recognizing and resuming an orphaned RUNNING/REVIEWING WorkItem after a
crash is a deliberate future decision (see ROADMAP.md, Slice 11) — this
module does not perform that reconciliation itself, exactly like
`RalphExecutionEngine` does not for `ExecutionRecord`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from orchestrator.execution_store import ExecutionStatus
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.project_state import Project, ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    ExecutionResult,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.review import (
    ReviewFinding,
    ReviewPolicy,
    ReviewRecord,
    ReviewStatus,
    ReviewStore,
    parse_findings,
)
from orchestrator.validation import QualityGateResult, QualityGateRunner
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ReviewIndependenceError,
    UnknownWorkerError,
    WorkerSelectionRequest,
    WorkerSelector,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_WORK_ITEM_ROLE = "developer"
REVIEWER_ROLE = "reviewer"
REVIEW_CAPABILITY = "reviewer"
DEFAULT_TIMEOUT_SECONDS = 900.0
INITIAL_EVENT_TOPIC = "work.start"
SUCCESS_TOPIC = "work.completed"
FAILURE_TOPIC = "work.failed"
REVIEW_INITIAL_EVENT_TOPIC = "review.start"
REVIEW_SUCCESS_TOPIC = "review.approved"
REVIEW_FAILURE_TOPIC = "review.rejected"

# Reviewer selection failures that must never be papered over with a
# silent same-worker/same-provider fallback — WorkerSelector already
# raises these when independence cannot be satisfied.
_REVIEWER_SELECTION_ERRORS = (NoEligibleWorkerError, ReviewIndependenceError, UnknownWorkerError)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _summarize_findings(findings: tuple[ReviewFinding, ...]) -> str:
    if not findings:
        return "(no specific findings recorded)"
    return "; ".join(f"[{f.severity}] {f.summary}" for f in findings)


def _build_dev_instructions(work_item: WorkItem, *, rework_context: str | None) -> str:
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    rework_block = (
        f"This work item was previously reviewed and rejected. {rework_context}\n\n"
        if rework_context
        else ""
    )
    return (
        f"{work_item.title}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"{rework_block}"
        "When this work item is genuinely complete, emit exactly:\n\n"
        f'ralph emit "{SUCCESS_TOPIC}" "done"\n\n'
        "If you cannot complete it, emit exactly:\n\n"
        f'ralph emit "{FAILURE_TOPIC}" "<short reason>"\n\n'
        "Then output:\n\nLOOP_COMPLETE\n"
    )


def _build_review_instructions(
    work_item: WorkItem,
    *,
    quality_gate_summary: str,
    git_sha: str | None,
    previous_findings: tuple[ReviewFinding, ...] | None,
) -> str:
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    lines = [
        f"Independently review the work completed for: {work_item.title}",
        "",
        "Acceptance criteria:",
        criteria,
        "",
        f"Quality gate result: {quality_gate_summary}",
    ]
    if git_sha:
        lines.append(f"Git SHA to review: {git_sha}")
    if previous_findings:
        lines += ["", "This is a re-review after a previous rejection. Previous findings:", _summarize_findings(previous_findings)]
    lines += [
        "",
        "Verify the actual result produced yourself — do not simply trust the author's summary.",
        "",
        "If the work meets the acceptance criteria, emit exactly:",
        "",
        f'ralph emit "{REVIEW_SUCCESS_TOPIC}" "approved"',
        "",
        "If it does not, emit exactly (a short reason, or a JSON array of finding objects "
        'with fields summary/severity/detail/file_path/line/category):',
        "",
        f'ralph emit "{REVIEW_FAILURE_TOPIC}" "<reason or JSON findings>"',
        "",
        "Then output:",
        "",
        "LOOP_COMPLETE",
    ]
    return "\n".join(lines)


def _summarize_gate(gate: QualityGateResult) -> str:
    verdict = "PASSED" if gate.passed else "FAILED"
    details = ", ".join(f"{r.validation_id}={r.status.value}" for r in gate.results)
    return f"quality_gate={verdict} ({details})" if details else f"quality_gate={verdict}"


def _summarize_review(review: ReviewRecord) -> str:
    reviewer = review.reviewer_worker_id or "none"
    summary = f"review_id={review.review_id} status={review.status.value} reviewer={reviewer}"
    if review.findings:
        summary += "; findings: " + _summarize_findings(review.findings)
    return summary


@dataclass(frozen=True, slots=True)
class WorkItemRunResult:
    """What happened when MVPManager ran one WorkItem."""

    work_item: WorkItem
    handoff: HandoffRecord
    gate_result: QualityGateResult | None = None
    review_result: ReviewRecord | None = None


class MVPManagerError(Exception):
    """Base for MVPManager domain errors."""


class MVPManager:
    """Sequentially delegates eligible WorkItems to WorkerSelector + RalphExecutionEngine."""

    def __init__(
        self,
        project_state_store: ProjectStateStore,
        handoff_store: HandoffStore,
        worker_selector: WorkerSelector,
        execution_engine: RalphExecutionEngine,
        *,
        quality_gate_runner: QualityGateRunner | None = None,
        review_store: ReviewStore | None = None,
        review_policy: ReviewPolicy | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_state_store = project_state_store
        self._handoff_store = handoff_store
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._quality_gate_runner = quality_gate_runner
        self._review_store = review_store
        self._review_policy = review_policy or ReviewPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._timeout_seconds = timeout_seconds

    async def run_next_work_item(self, mvp_id: str) -> WorkItemRunResult | None:
        """Runs exactly one eligible WorkItem of ``mvp_id``, if any is eligible.

        Eligible means READY (dependencies satisfied) or NEEDS_REWORK (a
        previous review rejected it and rework cycles remain) — the latter
        skips dependency re-checking since it was already established.
        Returns ``None`` when nothing is currently eligible.
        """
        self._project_state_store.refresh_readiness(mvp_id)
        eligible_statuses = (WorkItemStatus.READY, WorkItemStatus.NEEDS_REWORK)
        candidates = sorted(
            (
                wi for wi in self._project_state_store.list_work_items(mvp_id)
                if wi.status in eligible_statuses
            ),
            key=lambda wi: wi.work_item_id,
        )
        if not candidates:
            return None

        work_item = candidates[0]
        is_rework = work_item.status is WorkItemStatus.NEEDS_REWORK
        mvp = self._project_state_store.get_mvp(mvp_id)

        # Resolve a developer before recording anything as RUNNING: if no
        # worker is eligible right now, the WorkItem must stay in its
        # current state, not be marked as an attempt that never happened.
        dev_worker = await self._worker_selector.select(
            WorkerSelectionRequest(required_capabilities=work_item.required_capabilities)
        )

        self._project_state_store.mark_mvp_running(mvp_id)
        work_item = self._project_state_store.mark_work_item_running(work_item.work_item_id)
        project = self._project_state_store.get_project(mvp.project_id)

        rework_context = None
        if is_rework and self._review_store is not None:
            previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
            if previous_review is not None:
                rework_context = f"Previous review findings: {_summarize_findings(previous_review.findings)}"

        dev_request = ExecutionRequest(
            execution_id=self._id_factory(),
            task_id=work_item.work_item_id,
            worker=dev_worker,
            role=DEFAULT_WORK_ITEM_ROLE,
            workspace=project.workspace,
            instructions=_build_dev_instructions(work_item, rework_context=rework_context),
            initial_event_topic=INITIAL_EVENT_TOPIC,
            success_topics=frozenset({SUCCESS_TOPIC}),
            failure_topics=frozenset({FAILURE_TOPIC}),
            timeout_seconds=self._timeout_seconds,
        )
        dev_result = await self._execution_engine.execute(dev_request)
        dev_succeeded = dev_result.record.status is ExecutionStatus.SUCCEEDED

        gate_result: QualityGateResult | None = None
        if dev_succeeded and self._quality_gate_runner is not None:
            gate_result = await self._quality_gate_runner.run_gate(
                project_id=project.project_id,
                cwd=project.workspace,
                mvp_id=mvp_id,
                work_item_id=work_item.work_item_id,
            )
        gate_passed = gate_result is None or gate_result.passed

        review_result: ReviewRecord | None = None
        decisions: str | None = None
        open_issues: str | None = None

        if not dev_succeeded:
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            next_action = "investigate the failure before retrying — no automatic retry"
        elif not gate_passed:
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            next_action = "quality gate failed — investigate before retrying, no automatic retry"
        elif self._review_store is None:
            work_item = self._project_state_store.mark_work_item_completed(work_item.work_item_id)
            next_action = "proceed to the next eligible WorkItem"
        else:
            work_item = self._project_state_store.mark_work_item_reviewing(work_item.work_item_id)
            review_result, work_item, next_action = await self._run_review(
                project=project, mvp_id=mvp_id, work_item=work_item,
                dev_result=dev_result, gate_result=gate_result,
            )
            if review_result.status is ReviewStatus.APPROVED:
                decisions = (
                    f"review_id={review_result.review_id} approved by "
                    f"{review_result.reviewer_worker_id}"
                )
            else:
                open_issues = _summarize_review(review_result)

        handoff = self._handoff_store.create(
            handoff_id=self._id_factory(),
            project_id=project.project_id,
            mvp_id=mvp_id,
            work_item_id=work_item.work_item_id,
            objective=work_item.title,
            execution_id=dev_result.record.execution_id,
            worker_id=dev_result.record.worker_id,
            decisions=decisions,
            test_results=_summarize_gate(gate_result) if gate_result is not None else None,
            open_issues=open_issues,
            next_action=next_action,
            git_sha_after=dev_result.record.git_sha_after,
            created_at=self._clock(),
        )

        return WorkItemRunResult(
            work_item=work_item, handoff=handoff, gate_result=gate_result, review_result=review_result,
        )

    async def _run_review(
        self,
        *,
        project: Project,
        mvp_id: str,
        work_item: WorkItem,
        dev_result: ExecutionResult,
        gate_result: QualityGateResult | None,
    ) -> tuple[ReviewRecord, WorkItem, str]:
        """Selects an independent reviewer and runs the review via Ralph.

        Returns (ReviewRecord, updated WorkItem, next_action). Never
        raises for an expected "cannot review right now" outcome (no
        eligible reviewer, review execution error): those fail-close to
        BLOCKED with an explicit, persisted reason rather than leaving the
        WorkItem dangling in REVIEWING or propagating an exception that
        would abort the caller's loop over other WorkItems.
        """
        review_id = self._id_factory()
        started_at = self._clock()
        gate_summary = _summarize_gate(gate_result) if gate_result is not None else "not configured"

        try:
            reviewer = await self._worker_selector.select(
                WorkerSelectionRequest(
                    required_capabilities=frozenset({REVIEW_CAPABILITY}),
                    author_worker_id=dev_result.record.worker_id,
                )
            )
        except _REVIEWER_SELECTION_ERRORS as exc:
            review = ReviewRecord(
                review_id=review_id, project_id=project.project_id, mvp_id=mvp_id,
                work_item_id=work_item.work_item_id,
                author_execution_id=dev_result.record.execution_id,
                author_worker_id=dev_result.record.worker_id,
                started_at=started_at, finished_at=self._clock(), status=ReviewStatus.ERROR,
                git_sha_reviewed=dev_result.record.git_sha_after,
            )
            self._review_store.record(review)
            work_item = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id,
                reason=f"no eligible independent reviewer available: {exc}",
            )
            return review, work_item, "no eligible independent reviewer — blocked pending manual intervention"

        previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
        previous_findings = previous_review.findings if previous_review is not None else None

        review_request = ExecutionRequest(
            execution_id=self._id_factory(),
            task_id=work_item.work_item_id,
            worker=reviewer,
            role=REVIEWER_ROLE,
            workspace=project.workspace,
            instructions=_build_review_instructions(
                work_item, quality_gate_summary=gate_summary,
                git_sha=dev_result.record.git_sha_after, previous_findings=previous_findings,
            ),
            initial_event_topic=REVIEW_INITIAL_EVENT_TOPIC,
            success_topics=frozenset({REVIEW_SUCCESS_TOPIC}),
            failure_topics=frozenset({REVIEW_FAILURE_TOPIC}),
            timeout_seconds=self._timeout_seconds,
        )

        try:
            review_exec_result = await self._execution_engine.execute(review_request)
        except RalphExecutionEngineError:
            review = ReviewRecord(
                review_id=review_id, project_id=project.project_id, mvp_id=mvp_id,
                work_item_id=work_item.work_item_id,
                author_execution_id=dev_result.record.execution_id,
                author_worker_id=dev_result.record.worker_id,
                reviewer_execution_id=review_request.execution_id,
                reviewer_worker_id=reviewer.worker_id, reviewer_provider=reviewer.provider,
                reviewer_model=reviewer.model,
                started_at=started_at, finished_at=self._clock(), status=ReviewStatus.ERROR,
                git_sha_reviewed=dev_result.record.git_sha_after,
            )
            self._review_store.record(review)
            work_item = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id, reason="review execution failed to run"
            )
            return review, work_item, "review execution failed — blocked pending manual intervention"

        status, findings = self._determine_review_verdict(review_exec_result)

        review = ReviewRecord(
            review_id=review_id, project_id=project.project_id, mvp_id=mvp_id,
            work_item_id=work_item.work_item_id,
            author_execution_id=dev_result.record.execution_id,
            author_worker_id=dev_result.record.worker_id,
            reviewer_execution_id=review_exec_result.record.execution_id,
            reviewer_worker_id=reviewer.worker_id, reviewer_provider=reviewer.provider,
            reviewer_model=reviewer.model,
            started_at=started_at, finished_at=self._clock(), status=status, findings=findings,
            git_sha_reviewed=dev_result.record.git_sha_after,
        )
        self._review_store.record(review)

        if status is ReviewStatus.APPROVED:
            work_item = self._project_state_store.mark_work_item_completed(work_item.work_item_id)
            return review, work_item, "review approved — proceed to the next eligible WorkItem"

        # REJECTED / ERROR / INTERRUPTED: never COMPLETED. Bounded rework.
        cycles_used = self._review_store.count_for_work_item(work_item.work_item_id)
        if cycles_used >= self._review_policy.max_review_cycles:
            work_item = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id,
                reason=(
                    f"max_review_cycles ({self._review_policy.max_review_cycles}) "
                    "reached without approval"
                ),
            )
            return review, work_item, "max review cycles reached — blocked pending manual intervention"

        work_item = self._project_state_store.mark_work_item_needs_rework(work_item.work_item_id)
        return review, work_item, "review rejected — rework needed, see findings"

    def _determine_review_verdict(
        self, review_exec_result: ExecutionResult
    ) -> tuple[ReviewStatus, tuple[ReviewFinding, ...]]:
        if review_exec_result.record.status is ExecutionStatus.SUCCEEDED:
            return ReviewStatus.APPROVED, ()
        if review_exec_result.record.status is ExecutionStatus.INTERRUPTED:
            return ReviewStatus.INTERRUPTED, ()

        # FAILED: RalphExecutionEngine's own fail-closed rule already
        # collapses "explicit review.rejected" and "no reliable terminal
        # event at all" into the same status — distinguish them here using
        # the actual events, so a genuine rejection (with findings) is
        # never conflated with an inconclusive run.
        rejection_events = [e for e in review_exec_result.events if e.topic == REVIEW_FAILURE_TOPIC]
        if rejection_events:
            findings = parse_findings(rejection_events[-1].payload, self._id_factory)
            return ReviewStatus.REJECTED, findings
        return ReviewStatus.ERROR, ()
