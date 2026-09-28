"""RecoveryCoordinator — explicit reconciliation of orphaned/interrupted executions.

This module answers exactly one question, distinct from
:mod:`~orchestrator.wait`'s: "did the *execution itself* ever reach a
reliable terminal outcome?" — never "is a worker available?" (that remains
``WorkerSelector``/``QuotaManager``'s job). Two families of situations are
recognized, both ending the same way — the *WorkItem* becomes explicitly
re-orchestrable again (``WorkItemStatus.RECOVERY_REQUIRED``), backed by a
durable recovery handoff so a caller (``MVPManager``) can resume from
persisted facts without depending on the interrupted worker/QA engine for
anything, including a summary:

**A. Developer execution recovery** (``ExecutionStore``, unchanged since
the original slice):

- an ``ExecutionRecord`` still ``RUNNING`` after a restart, discovered via
  ``ExecutionStore.list_running()`` — the process that was running it is
  gone, so its real fate is unknown; it is marked ``RECOVERY_REQUIRED``
  (never silently treated as SUCCEEDED, never blindly relaunched);
- an ``ExecutionRecord`` already ``INTERRUPTED`` (e.g. a timeout, or a
  real cancellation — P18-03 — handled by ``RalphExecutionEngine`` itself,
  in the same or a previous process) whose owning WorkItem never got
  moved out of RUNNING before a crash.

**B. QA-phase recovery** (``QARunStore``, added P18-03): a real gap found
while designing graceful interruption — the last *developer* execution
can already be durably ``SUCCEEDED`` while the WorkItem is still
``RUNNING``, if the interruption happened during QA itself (QA is
orchestrated through ``QARunStore``, never ``ExecutionStore`` — family A
above cannot see it at all, and returned ``None``/did nothing before this
addition, leaving the WorkItem ``RUNNING`` forever). Recognized the same
way as family A, one level down: the *latest* ``QARun`` for this WorkItem
is still ``RUNNING`` (orphaned, unknown fate) or already ``INTERRUPTED``
(a real cancellation ``MVPManager`` already observed itself). An orphaned
``RUNNING`` QARun is marked ``INTERRUPTED`` here (QARunStore's own
existing transition, ``RUNNING -> INTERRUPTED`` — never a fabricated
PASS/FAIL verdict); an already-``INTERRUPTED`` one stays exactly as-is.
Either way the WorkItem becomes ``RECOVERY_REQUIRED`` — ``MVPManager``'s
own resume logic (P18-03) reads the interrupted QARun's
``expected_base_sha``/``expected_head_sha`` to launch a *new* QA attempt
on the same, already-governed code — it never replays DEV A/DEV B.

Either way (A or B), the interrupted/orphaned record is NEVER mutated back
toward its own "in progress" state and NEVER silently turned into a
verdict/result — it stays exactly where it is (terminal, permanent audit
history).

This is pure bookkeeping: no worker selection, no execution/QA launch, no
polling loop, no Claude/Codex/Ralph/subprocess call anywhere here — a
single mechanism recognizing two real, distinct families of durable
facts, never a second, parallel recovery engine. It only reads
``ExecutionStore``/``QARunStore``/``HandoffStore`` and writes
``ExecutionStore``/``QARunStore`` (existing status-transition primitives)
and ``HandoffStore``/``ProjectStateStore`` (existing primitives) — no new
storage engine, no duplicated WorkerSelector/QuotaManager logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.project_state import ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.qa import QARunStatus, QARunStore

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEVELOPER_ROLE = "developer"

RECOVERY_OPEN_ISSUE = "execution interrupted / recovery required"
RECOVERY_NEXT_ACTION = "continue work from persisted state"


def _default_id_factory() -> str:
    return uuid.uuid4().hex


class RecoveryCoordinator:
    """Reconciles orphaned/interrupted executions into a re-orchestrable WorkItem."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        handoff_store: HandoffStore,
        project_state_store: ProjectStateStore,
        *,
        qa_run_store: QARunStore | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._execution_store = execution_store
        self._handoff_store = handoff_store
        self._project_state_store = project_state_store
        # Opt-in, exactly like every other capability MVPManager composes:
        # QA recovery (family B) is only recognized when QA itself is
        # configured (``qa_run_store is not None``) — omitting it preserves
        # this coordinator's exact pre-P18-03 behavior (family A only).
        self._qa_run_store = qa_run_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory

    def reconcile_mvp(self, mvp_id: str) -> list[WorkItem]:
        """Reconciles every RUNNING WorkItem of this MVP, if needed.

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
        """Reconciles one WorkItem if it is stuck behind an orphaned/interrupted
        developer execution (family A) or an orphaned/interrupted QA
        attempt (family B, P18-03).

        Returns the updated (RECOVERY_REQUIRED) WorkItem, or ``None`` if
        nothing needed reconciling (not RUNNING, or the last relevant fact
        already reached a genuine terminal outcome).
        """
        if work_item.status is not WorkItemStatus.RUNNING:
            return None

        relevant = [e for e in self._execution_store.list_for_task(work_item.work_item_id) if e.role == DEVELOPER_ROLE]
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
            # Already a genuine terminal outcome (e.g. a timeout, or a
            # real cancellation — P18-03 — handled by RalphExecutionEngine
            # itself) — stays exactly as-is, permanent history. Only the
            # WorkItem-side reconciliation below was left undone (e.g. a
            # crash between execute() returning and the WorkItem being
            # updated) and still needs finishing.
            execution = last
        elif last.status is ExecutionStatus.SUCCEEDED:
            # Family B (P18-03): development itself is durably done — any
            # remaining recovery need can only be QA's, never a developer
            # execution's. Never reached for FAILED/RECOVERY_REQUIRED
            # (handled by the final `else` below): those are stale for an
            # unrelated reason, not this coordinator's concern.
            return self._reconcile_qa(project_id=project_id, mvp_id=mvp_id, work_item=work_item, last_dev=last)
        else:
            # Already FAILED/RECOVERY_REQUIRED: nothing to do — the
            # WorkItem's own status is stale for an unrelated reason, out
            # of scope here.
            return None

        self.ensure_recovery_handoff(
            project_id=project_id, mvp_id=mvp_id, work_item=work_item, execution=execution
        )
        return self._project_state_store.mark_work_item_recovery_required(work_item.work_item_id)

    def _reconcile_qa(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem, last_dev: ExecutionRecord,
    ) -> WorkItem | None:
        """Family B (P18-03): the WorkItem is RUNNING and its last developer
        execution already SUCCEEDED — any recovery need left can only be
        QA's own (QA is tracked in ``QARunStore``, never ``ExecutionStore``,
        so family A above can never see this case). ``None`` when QA is not
        configured for this MVPManager at all, or the latest QA attempt for
        this WorkItem already reached a genuine terminal outcome
        (``COMPLETED``/``FAILED`` — a real verdict already exists, nothing
        to reconcile)."""
        if self._qa_run_store is None:
            return None
        qa_run = self._qa_run_store.latest_for_work_item(work_item.work_item_id)
        if qa_run is None or qa_run.status not in (QARunStatus.RUNNING, QARunStatus.INTERRUPTED):
            return None
        if qa_run.status is QARunStatus.RUNNING:
            # The process that owned this QA attempt is gone — never
            # assumed PASS/FAIL, never a second QARunStore transition
            # invented: RUNNING -> INTERRUPTED is QARunStatus's own
            # existing, permitted transition.
            self._qa_run_store.update_status(qa_run.run_id, QARunStatus.INTERRUPTED)
        # Reuses the already-SUCCEEDED developer execution for the
        # handoff: its own git_sha_after is, by construction, exactly the
        # QARun's own expected_head_sha — MVPManager's QA-only resume
        # (P18-03) reads this same handoff to launch a fresh QA attempt on
        # the same code, never replaying DEV A/DEV B.
        self.ensure_recovery_handoff(
            project_id=project_id, mvp_id=mvp_id, work_item=work_item, execution=last_dev
        )
        return self._project_state_store.mark_work_item_recovery_required(work_item.work_item_id)

    def ensure_recovery_handoff(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem, execution: ExecutionRecord
    ) -> HandoffRecord:
        """Ensures a durable handoff exists for this interrupted execution.

        Never calls the interrupted worker for a summary: built entirely
        from persisted facts (the execution record itself, the last
        pre-existing handoff for its quality-gate result). Idempotent —
        calling this again for the same execution reuses the handoff
        already created for it rather than duplicating it.
        """
        existing = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        if existing is not None and existing.execution_id == execution.execution_id:
            return existing

        open_issues = RECOVERY_OPEN_ISSUE

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
