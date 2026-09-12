"""MVPManager — orchestrates high-level WorkItems for the current MVP.

This is the composition root for the Slice 7/8 cycle:

    ROADMAP (functional intent, read by a human/future agent)
      -> MVP / WorkItem (this module's input, via ProjectStateStore)
      -> WorkerSelector (existing, Slice 4: chooses a worker)
      -> RalphExecutionEngine (existing, Slice 6: runs it)
      -> QualityGateRunner (existing, Slice 8: validates it — optional)
      -> HandoffStore (Slice 7: durable handoff, now gate-aware)

Slice 8 integration is deliberately minimal and opt-in: a WorkItem whose
execution SUCCEEDED is only gated if a ``QualityGateRunner`` was supplied
AND the project has at least one configured validation command. When
either is absent, behavior is identical to Slice 7 (execution success ->
COMPLETED) — no existing caller is forced to adopt quality gates. This
module never runs commands itself, never re-implements
``ValidationStore``/``QualityGateRunner`` — it only calls
``QualityGateRunner.run_gate()`` and reads the returned
``QualityGateResult.passed``.

MVPManager orchestrates WorkItems — high-level units of work — never a
second, fine-grained task queue: once a WorkItem is delegated to Ralph (via
RalphExecutionEngine), Ralph keeps its own internal orchestration. This
module re-implements none of WorkerSelector's capability/governance/quota
logic, and never launches `claude`/`codex`/`ralph` directly — only through
the existing `WorkerSelector`/`RalphExecutionEngine`.

Execution is strictly sequential and deterministic in this slice: exactly
one WorkItem is considered per `run_next_work_item()` call, chosen among
the READY ones by ascending `work_item_id`. No parallelism, no retry, no
fallback — a failed WorkItem's dependents are never executed (they resolve
to BLOCKED via `ProjectStateStore.refresh_readiness`, never silently
skipped or re-tried).

A RUNNING WorkItem is never picked up again by this module: `run_next_work_item`
only ever selects from `READY` items, and `refresh_readiness` never touches
a WorkItem that is not `PLANNED`. Recognizing and resuming an orphaned
`RUNNING` WorkItem after a crash is a deliberate future decision (see
ROADMAP.md, Slice 11) — this module does not perform that reconciliation
itself, exactly like `RalphExecutionEngine` does not for `ExecutionRecord`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from orchestrator.execution_store import ExecutionStatus
from orchestrator.project_state import ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.ralph_execution_engine import ExecutionRequest, RalphExecutionEngine
from orchestrator.validation import QualityGateResult, QualityGateRunner
from orchestrator.worker_selector import WorkerSelectionRequest, WorkerSelector

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_WORK_ITEM_ROLE = "developer"
DEFAULT_TIMEOUT_SECONDS = 900.0
INITIAL_EVENT_TOPIC = "work.start"
SUCCESS_TOPIC = "work.completed"
FAILURE_TOPIC = "work.failed"


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _build_instructions(work_item: WorkItem) -> str:
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    return (
        f"{work_item.title}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "When this work item is genuinely complete, emit exactly:\n\n"
        f'ralph emit "{SUCCESS_TOPIC}" "done"\n\n'
        "If you cannot complete it, emit exactly:\n\n"
        f'ralph emit "{FAILURE_TOPIC}" "<short reason>"\n\n'
        "Then output:\n\nLOOP_COMPLETE\n"
    )


def _summarize_gate(gate: QualityGateResult) -> str:
    verdict = "PASSED" if gate.passed else "FAILED"
    details = ", ".join(f"{r.validation_id}={r.status.value}" for r in gate.results)
    return f"quality_gate={verdict} ({details})" if details else f"quality_gate={verdict}"


@dataclass(frozen=True, slots=True)
class WorkItemRunResult:
    """What happened when MVPManager ran one WorkItem."""

    work_item: WorkItem
    handoff: HandoffRecord
    gate_result: QualityGateResult | None = None


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
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_state_store = project_state_store
        self._handoff_store = handoff_store
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._quality_gate_runner = quality_gate_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._timeout_seconds = timeout_seconds

    async def run_next_work_item(self, mvp_id: str) -> WorkItemRunResult | None:
        """Runs exactly one eligible WorkItem of ``mvp_id``, if any is READY.

        Returns ``None`` when nothing is currently READY (nothing to do
        right now — this is not an error: dependencies may simply not be
        satisfied yet, or everything is already terminal/RUNNING).
        """
        self._project_state_store.refresh_readiness(mvp_id)
        ready = sorted(
            (
                wi for wi in self._project_state_store.list_work_items(mvp_id)
                if wi.status is WorkItemStatus.READY
            ),
            key=lambda wi: wi.work_item_id,
        )
        if not ready:
            return None

        work_item = ready[0]
        mvp = self._project_state_store.get_mvp(mvp_id)

        # Resolve a worker before recording anything as RUNNING: if no
        # worker is eligible right now, the WorkItem must stay READY, not
        # be marked as an attempt that never happened.
        worker = await self._worker_selector.select(
            WorkerSelectionRequest(required_capabilities=work_item.required_capabilities)
        )

        self._project_state_store.mark_mvp_running(mvp_id)
        work_item = self._project_state_store.mark_work_item_running(work_item.work_item_id)

        project = self._project_state_store.get_project(mvp.project_id)
        execution_id = self._id_factory()

        request = ExecutionRequest(
            execution_id=execution_id,
            task_id=work_item.work_item_id,
            worker=worker,
            role=DEFAULT_WORK_ITEM_ROLE,
            workspace=project.workspace,
            instructions=_build_instructions(work_item),
            initial_event_topic=INITIAL_EVENT_TOPIC,
            success_topics=frozenset({SUCCESS_TOPIC}),
            failure_topics=frozenset({FAILURE_TOPIC}),
            timeout_seconds=self._timeout_seconds,
        )
        result = await self._execution_engine.execute(request)
        execution_succeeded = result.record.status is ExecutionStatus.SUCCEEDED

        # Quality gate integration (Slice 8): only attempted when the
        # execution itself succeeded, and only when a runner was actually
        # supplied — a project with no configured validation commands (or
        # no runner at all) behaves exactly like Slice 7 (execution
        # success -> COMPLETED).
        gate_result: QualityGateResult | None = None
        if execution_succeeded and self._quality_gate_runner is not None:
            gate_result = await self._quality_gate_runner.run_gate(
                project_id=project.project_id,
                cwd=project.workspace,
                mvp_id=mvp_id,
                work_item_id=work_item.work_item_id,
            )

        gate_passed = gate_result is None or gate_result.passed

        if execution_succeeded and gate_passed:
            work_item = self._project_state_store.mark_work_item_completed(work_item.work_item_id)
            next_action = "proceed to the next eligible WorkItem"
        elif execution_succeeded:  # gate_result is not None and not gate_passed
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            next_action = "quality gate failed — investigate before retrying, no automatic retry"
        else:
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            next_action = "investigate the failure before retrying — no automatic retry"

        handoff = self._handoff_store.create(
            handoff_id=self._id_factory(),
            project_id=project.project_id,
            mvp_id=mvp_id,
            work_item_id=work_item.work_item_id,
            objective=work_item.title,
            execution_id=result.record.execution_id,
            worker_id=result.record.worker_id,
            test_results=_summarize_gate(gate_result) if gate_result is not None else None,
            next_action=next_action,
            git_sha_after=result.record.git_sha_after,
            created_at=self._clock(),
        )

        return WorkItemRunResult(work_item=work_item, handoff=handoff, gate_result=gate_result)
