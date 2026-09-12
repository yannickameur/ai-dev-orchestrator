"""RecoveryCoordinator — explicit reconciliation of orphaned/interrupted executions.

This module answers exactly one question, distinct from
:mod:`~orchestrator.wait`'s: "did the *execution itself* ever reach a
reliable terminal outcome?" — never "is a worker available?" (that remains
``WorkerSelector``/``QuotaManager``'s job). Two, and only two, situations
are recognized:

- an ``ExecutionRecord`` still ``RUNNING`` after a restart, discovered via
  ``ExecutionStore.list_running()`` — the process that was running it is
  gone, so its real fate is unknown; it is marked ``RECOVERY_REQUIRED``
  (never silently treated as SUCCEEDED, never blindly relaunched);
- an ``ExecutionRecord`` already ``INTERRUPTED`` (e.g. a timeout handled by
  ``RalphExecutionEngine`` itself, in the same or a previous process) whose
  owning WorkItem never got moved out of RUNNING/REVIEWING before a crash.

Either way, the interrupted/orphaned execution is NEVER mutated back
toward ``RUNNING`` and NEVER relaunched — it stays exactly where it is
(``RECOVERY_REQUIRED`` or ``INTERRUPTED``) as permanent audit history. This
module's only forward action is to make the *WorkItem* explicitly
re-orchestrable again (``WorkItemStatus.RECOVERY_REQUIRED``), backed by a
durable recovery handoff so a caller (``MVPManager``) can launch a
brand-new execution — on a possibly different worker — without depending
on the interrupted worker for anything, including a summary.

This is pure bookkeeping: no worker selection, no execution launch, no
polling loop, no Claude/Codex/Ralph/subprocess call anywhere here. It only
reads ``ExecutionStore``/``HandoffStore``/``ReviewStore`` and writes
``ExecutionStore`` (existing ``mark_recovery_required``) and
``HandoffStore``/``ProjectStateStore`` (existing primitives) — no new
storage engine, no duplicated WorkerSelector/QuotaManager logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.project_state import ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.review import ReviewStore

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEVELOPER_ROLE = "developer"
REVIEWER_ROLE = "reviewer"

RECOVERY_OPEN_ISSUE = "execution interrupted / recovery required"
RECOVERY_NEXT_ACTION = "continue work from persisted state"


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _summarize_findings(findings) -> str:
    return "; ".join(f"[{f.severity}] {f.summary}" for f in findings)


class RecoveryCoordinator:
    """Reconciles orphaned/interrupted executions into a re-orchestrable WorkItem."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        handoff_store: HandoffStore,
        project_state_store: ProjectStateStore,
        review_store: ReviewStore | None = None,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._execution_store = execution_store
        self._handoff_store = handoff_store
        self._project_state_store = project_state_store
        self._review_store = review_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory

    def reconcile_mvp(self, mvp_id: str) -> list[WorkItem]:
        """Reconciles every RUNNING/REVIEWING WorkItem of this MVP, if needed.

        Cheap and synchronous (no execution launched here) — safe to call
        at the start of every orchestration pass. Returns the WorkItems
        actually moved to RECOVERY_REQUIRED (usually zero or one, since
        this codebase runs one WorkItem at a time, but never assumed).
        """
        mvp = self._project_state_store.get_mvp(mvp_id)
        reconciled: list[WorkItem] = []
        for work_item in self._project_state_store.list_work_items(mvp_id):
            updated = self.reconcile_work_item(project_id=mvp.project_id, mvp_id=mvp_id, work_item=work_item)
            if updated is not None:
                reconciled.append(updated)
        return reconciled

    def reconcile_work_item(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem
    ) -> WorkItem | None:
        """Reconciles one WorkItem if it is stuck behind an orphaned/interrupted execution.

        Returns the updated (RECOVERY_REQUIRED) WorkItem, or ``None`` if
        nothing needed reconciling (not RUNNING/REVIEWING, or its last
        matching execution already reached a genuine terminal outcome).
        """
        if work_item.status not in (WorkItemStatus.RUNNING, WorkItemStatus.REVIEWING):
            return None

        role = REVIEWER_ROLE if work_item.status is WorkItemStatus.REVIEWING else DEVELOPER_ROLE
        relevant = [e for e in self._execution_store.list_for_task(work_item.work_item_id) if e.role == role]
        if not relevant:
            return None
        last = relevant[-1]

        if last.status is ExecutionStatus.RUNNING:
            # The process that owned this execution is gone — its real
            # fate is unknown, so it is never assumed SUCCEEDED and never
            # relaunched. RECOVERY_REQUIRED, not INTERRUPTED: unlike a
            # timeout RalphExecutionEngine observes itself, nothing here
            # actually watched this execution end.
            execution = self._execution_store.mark_recovery_required(
                last.execution_id, finished_at=self._clock()
            )
        elif last.status is ExecutionStatus.INTERRUPTED:
            # Already a genuine terminal outcome (e.g. a timeout handled by
            # RalphExecutionEngine itself) — stays exactly as-is, permanent
            # history. Only the WorkItem-side reconciliation below was
            # left undone (e.g. a crash between execute() returning and
            # the WorkItem being updated) and still needs finishing.
            execution = last
        else:
            # Already SUCCEEDED/FAILED/RECOVERY_REQUIRED: nothing to do —
            # the WorkItem's own status is stale for an unrelated reason,
            # out of scope here.
            return None

        self.ensure_recovery_handoff(
            project_id=project_id, mvp_id=mvp_id, work_item=work_item, execution=execution
        )
        return self._project_state_store.mark_work_item_recovery_required(work_item.work_item_id)

    def ensure_recovery_handoff(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem, execution: ExecutionRecord
    ) -> HandoffRecord:
        """Ensures a durable handoff exists for this interrupted execution.

        Never calls the interrupted worker for a summary: built entirely
        from persisted facts (the execution record itself, the last
        pre-existing handoff for its quality-gate result, the last review
        for its findings if relevant). Idempotent — calling this again for
        the same execution reuses the handoff already created for it
        rather than duplicating it.
        """
        existing = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        if existing is not None and existing.execution_id == execution.execution_id:
            return existing

        open_issues = RECOVERY_OPEN_ISSUE
        if self._review_store is not None:
            previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
            if previous_review is not None and previous_review.findings:
                open_issues += f"; previous review findings: {_summarize_findings(previous_review.findings)}"

        return self._handoff_store.create(
            handoff_id=self._id_factory(),
            project_id=project_id,
            mvp_id=mvp_id,
            work_item_id=work_item.work_item_id,
            objective=work_item.title,
            execution_id=execution.execution_id,
            worker_id=execution.worker_id,
            test_results=existing.test_results if existing is not None else None,
            open_issues=open_issues,
            next_action=RECOVERY_NEXT_ACTION,
            git_sha_after=execution.git_sha_after,
            created_at=self._clock(),
        )
