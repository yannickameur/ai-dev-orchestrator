"""MVPManager — orchestrates high-level WorkItems for the current MVP.

This is the composition root for the supported product workflow,
``LEAN_FEATURE_FLOW`` — the only WorkItem execution pipeline this module
implements:

    ROADMAP (functional intent, read by a human/future agent)
      -> MVP / WorkItem (this module's input, via ProjectStateStore)
      -> WorkerSelector (chooses DEV A, then DEV B — a genuinely
         independent second developer, `DEV_B.worker_id != DEV_A.worker_id`
         enforced by WorkerSelector itself, never duplicated here)
      -> RalphExecutionEngine (runs DEV A/DEV B — never a direct
         claude/codex/ralph call)
      -> deterministic QA (opt-in via ``qa_engine``/``qa_policy``/
         ``qa_run_store`` — read-only, non-LLM, the sole PASS/FAIL
         authority: `LLM IS NOT ORACLE`, a developer's own claim that
         "it works" is never sufficient)
      -> GitGovernanceService (opt-in via ``git_governance_service`` —
         merge/tag once QA PASSES)
      -> HandoffStore (durable handoff after every step, gate/QA-aware)

A WorkItem reaches ``COMPLETED`` only once DEV A succeeded, DEV B's
corrective review completed, and QA (if configured) verdict is PASS.

MVPManager orchestrates WorkItems — high-level units of work — never a
second, fine-grained task queue: once a WorkItem is delegated to Ralph (via
RalphExecutionEngine), Ralph keeps its own internal orchestration. This
module re-implements none of WorkerSelector's capability/governance/quota
logic, and never launches `claude`/`codex`/`ralph` directly — only through
the existing `WorkerSelector`/`RalphExecutionEngine`.

Execution is strictly sequential and deterministic: exactly one WorkItem is
considered per `run_next_work_item()` call, chosen among the
READY/NEEDS_REWORK ones by ascending `work_item_id`. No parallelism.

REWORK MACHINE-CONTROLLED, NEVER A BLIND TECHNICAL RETRY:
a WorkItem that fails QA moves to `NEEDS_REWORK` and is eligible for a
bounded number of further DEV-FIX-then-QA cycles
(``QAPolicy.max_qa_cycles``) — this is business rework driven by QA's own
findings, never an automatic re-run of a crashed/timed-out execution (that
remains forbidden everywhere in this codebase). Once exhausted without a
PASS, the WorkItem is `BLOCKED` with an explicit reason
(`HUMAN_REVIEW_REQUIRED`), and — as with every other terminal path — its
dependents are never executed (they resolve to BLOCKED via
`ProjectStateStore.refresh_readiness`, never silently skipped or re-tried).

A plain RUNNING WorkItem is never picked up again by the normal candidate
query: `run_next_work_item`'s fresh-candidate step only ever selects from
READY/NEEDS_REWORK items, and `refresh_readiness` never touches a WorkItem
that is not PLANNED. Recognizing *why* a WorkItem is still RUNNING (a live
in-progress attempt vs. one orphaned by a crash) is not something this
module — or `RalphExecutionEngine` for `ExecutionRecord` — decides
implicitly; see EXECUTION-LEVEL RECOVERY below for the explicit
reconciliation this module performs before that query ever runs.

QUOTA WAITING / DURABLE RESUME:

Supplying an optional ``wait_store`` enables a third opt-in capability,
composed the same way as the two before it: when `WorkerSelector` raises
`NoEligibleWorkerError` for DEV A or DEV B, this module asks the error's
own ``diagnostics`` (a read-only, `WorkerSelector`-authored fact list —
never re-derived or duplicated here) whether a plausible next retry moment
is known. If — and only if — at least one candidate provider is diagnosed
``quota_exhausted`` with a real `QuotaWindow.reset_at`, the WorkItem moves
to `WorkItemStatus.WAITING` and a durable `orchestrator.wait.WaitRecord`
is persisted with that exact deadline. If no reliable deadline is known,
the exception propagates — this module never invents a wait.

Resuming is pull-based and re-checked, never a background poller: the
very first thing `run_next_work_item` does is ask
`WaitCoordinator.find_due` whether this MVP has a wait whose deadline has
passed. If so, it re-probes `WorkerSelector` right then (a theoretical
reset is never treated as proof of availability) and either resumes with
a **brand-new `execution_id`** (never the interrupted one) — reusing the
last `HandoffRecord` as recovery context rather than asking the
interrupted worker for a fresh summary — or requeues the wait against a
newly-diagnosed deadline, or gives up to `BLOCKED` when no reliable
deadline remains. A resumed WorkItem may land on an entirely different
worker/provider than before; nothing about resumption depends on the
previous worker still existing or remembering anything.

EXECUTION-LEVEL RECOVERY:

Distinct from quota waiting above: this handles an *execution* that never
reached a reliable terminal outcome — a RUNNING `ExecutionRecord` found
orphaned after a restart (the process that owned it is gone; its real
fate is unknown), or one that came back `INTERRUPTED` (e.g. a
`RalphExecutionEngine` timeout) whose WorkItem never got moved out of
RUNNING before a crash. Supplying an optional ``execution_store`` enables
a fourth opt-in capability: a `orchestrator.recovery.RecoveryCoordinator`
reconciles every RUNNING WorkItem of the MVP at the very start of every
`run_next_work_item` call — cheap, synchronous, no execution launched by
the reconciliation itself. An orphaned/interrupted execution is NEVER
relaunched and NEVER flipped back toward RUNNING; it stays exactly as
`RECOVERY_REQUIRED`/`INTERRUPTED` forever, permanent audit history.
Instead, the WorkItem moves to `WorkItemStatus.RECOVERY_REQUIRED`
(immediately re-orchestrable, no deadline to wait out, unlike `WAITING`),
backed by a durable recovery handoff (reused if one already exists for
that exact execution, else synthesized from persisted
`ExecutionRecord`/`WorkItem`/prior-handoff facts — never by asking the
interrupted worker for a summary). The very next thing this module does
is resume it: a fresh `WorkerSelector` selection (possibly a different
worker/provider) and a **brand-new** `execution_id` — never the
orphaned/interrupted one.

ADAPTIVE DEVELOPMENT/REWORK SELECTION:

Supplying an optional ``adaptive_execution_selector`` enables a fifth,
independent opt-in capability: every DEVELOPMENT/REWORK path that resolves
a developer — the fresh-candidate path in `run_next_work_item`, the
quota-wait resume (`_try_resume_due_wait`), and the execution-recovery
resume (`_try_resume_recovery_required`) — shares one helper,
`_select_dev_worker`, so a *resumed* attempt is held to exactly the same
rules as a fresh one. When configured, each call runs a complexity
pre-flight (`orchestrator.complexity_estimation`) from *current* persisted
facts (latest handoff, git SHA — re-read every time, never cached in a
`WaitRecord`/recovery handoff) and resolves a concrete
`Worker`/`ExecutionProfile` (`orchestrator.adaptive_execution`) instead of
a plain `WorkerSelector.select(...)` + `Worker.profile()` default. No
`LEAN_FEATURE_FLOW` caller in this codebase currently wires this
capability (the Lean default is a plain `WorkerSelector.select()`), but
the mechanism itself is orthogonal to workflow choice and stays available.
No silent downgrade below a recommended quality tier, no fallback to a
default profile on any pre-flight/selection failure (those exceptions
propagate unchanged).

Historical note: an earlier, heavier pipeline (`WorkflowMode.GOVERNED_FULL`
— a separate read-only Reviewer phase, isolated QA Test Authoring, and a
separate Final QA phase) existed in this module and was removed before the
first public release, superseded by `LEAN_FEATURE_FLOW`'s DEV A -> DEV B
corrective review -> deterministic QA. See ROADMAP.md's dated removal
entry for the full rationale; nothing in this module implements it anymore.

Release planning and roadmap synthesis are made adaptive by
`orchestrator.planning`, not by this module — see that module's own
docstring; nothing here changes for either.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.adaptive_execution import AdaptiveExecutionSelector
from orchestrator.complexity_estimation import ComplexityEstimationRequest
from orchestrator.execution_store import ExecutionStatus, ExecutionStore, UnknownExecutionError
from orchestrator.git_governance import (
    RALPH_RUNTIME_NOISE_PREFIXES,
    GitGovernanceService,
    GitWorkItemRecord,
    GitWorkItemStatus,
    LocalGitWorkspace,
)
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.internal_qa_engine import working_tree_changed_files
from orchestrator.project_state import MVP, Project, ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.qa import (
    QAEngine,
    QAEvidenceManifest,
    QAPhase,
    QAPolicy,
    QARequest,
    QARun,
    QARunStatus,
    QARunStore,
    QAVerdictStatus,
    evaluate_qa_verdict,
    new_qa_run,
)
from orchestrator.qa_protection import (
    ProtectedTestBaseline,
    ProtectedTestFileState,
    compare_protected_test_baseline,
    has_unauthorized_change,
)
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    ExecutionResult,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.recovery import RecoveryCoordinator
from orchestrator.validation import QualityGateResult, ValidationStore
from orchestrator.wait import WaitCoordinator, WaitPhase, WaitRecord, WaitStore
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ProviderSelectionDiagnostic,
    UnknownWorkerError,
    Worker,
    WorkerSelectionRequest,
    WorkerSelector,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_WORK_ITEM_ROLE = "developer"
DEFAULT_TIMEOUT_SECONDS = 900.0
INITIAL_EVENT_TOPIC = "work.start"
SUCCESS_TOPIC = "work.completed"
FAILURE_TOPIC = "work.failed"


def _default_id_factory() -> str:
    return uuid.uuid4().hex


# Generic, backend-agnostic commit-governance reminder included in every
# development-role instruction (DEV A and DEV B alike). Not backend- or
# provider-specific: Claude Code/Codex already commit reliably on their
# own agentic default and this reminder is harmless for them; some
# backends (e.g. Vibe — see docs/VIBE_SPIKE.md §19) do not commit unless
# explicitly told to, and this is what makes them do so reliably —
# VERIFIED by a real disposable execution, not assumed. Never a
# backend-specific branch in code: the same instructions text for every
# worker, regardless of provider/backend.
_COMMIT_GOVERNANCE_REMINDER = (
    "Before emitting completion, ensure any code/test changes are committed with "
    "git — do not leave them uncommitted. Use this repository's already "
    "configured git identity (git config user.name/user.email) as both author "
    "and committer; never pass --author, never use a different identity. Do not "
    "add any AI attribution, Co-Authored-By, Signed-off-by, or similar trailer "
    "to the commit message. Leave the working tree clean after committing."
)


def _build_dev_instructions(work_item: WorkItem, *, resume_context: str | None) -> str:
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    rework_block = f"{resume_context}\n\n" if resume_context else ""
    return (
        f"{work_item.title}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"{rework_block}"
        f"{_COMMIT_GOVERNANCE_REMINDER}\n\n"
        "When this work item is genuinely complete, emit exactly:\n\n"
        f'ralph emit "{SUCCESS_TOPIC}" "done"\n\n'
        "If you cannot complete it, emit exactly:\n\n"
        f'ralph emit "{FAILURE_TOPIC}" "<short reason>"\n\n'
        "Then output:\n\nLOOP_COMPLETE\n"
    )


def _build_dev_b_instructions(work_item: WorkItem, *, dev_a_worker_id: str) -> str:
    """DEV B's corrective review — deliberately NOT read-only: fix evident
    issues directly rather than only listing suggestions, and commit the
    fix."""
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    return (
        f"Corrective review of the implementation just produced by another developer "
        f"({dev_a_worker_id}) for: {work_item.title}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "Re-read the actual code and tests already committed on this branch. Verify it "
        "genuinely meets the acceptance criteria, is simple (KISS), and does not implement "
        "anything beyond this work item's scope (YAGNI). If you find evident issues, fix them "
        "directly and commit the fix — do not just write a list of suggestions for someone else "
        "to apply. Do not implement the next feature, refactor unrelated code, or add "
        "frameworks/abstractions not required here.\n\n"
        f"{_COMMIT_GOVERNANCE_REMINDER}\n\n"
        "When you are done reviewing (whether or not you made changes), emit exactly:\n\n"
        f'ralph emit "{SUCCESS_TOPIC}" "done"\n\n'
        "If you cannot complete this review, emit exactly:\n\n"
        f'ralph emit "{FAILURE_TOPIC}" "<short reason>"\n\n'
        "Then output:\n\nLOOP_COMPLETE\n"
    )


@dataclass(frozen=True, slots=True)
class WorkItemRunResult:
    """What happened when MVPManager ran one WorkItem.

    ``handoff`` is ``None`` exactly when nothing was executed this call —
    a fresh WAITING (no eligible worker at all, but a reset is known) or a
    give-up to BLOCKED with no execution attempted. ``wait`` is set only
    when this call's outcome was recording or updating a durable wait
    (never set on a resumed, executed, or terminal outcome).
    """

    work_item: WorkItem
    handoff: HandoffRecord | None
    gate_result: QualityGateResult | None = None
    wait: WaitRecord | None = None


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
        wait_store: WaitStore | None = None,
        execution_store: ExecutionStore | None = None,
        adaptive_execution_selector: AdaptiveExecutionSelector | None = None,
        validation_store: ValidationStore | None = None,
        git_governance_service: GitGovernanceService | None = None,
        qa_engine: QAEngine | None = None,
        qa_policy: QAPolicy | None = None,
        qa_run_store: QARunStore | None = None,
        qa_protected_paths: tuple[str, ...] = (),
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_state_store = project_state_store
        self._handoff_store = handoff_store
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._timeout_seconds = timeout_seconds
        self._wait_store = wait_store
        self._adaptive_execution_selector = adaptive_execution_selector
        self._validation_store = validation_store
        self._git_governance_service = git_governance_service
        self._wait_coordinator = (
            WaitCoordinator(wait_store, clock=self._clock, id_factory=self._id_factory)
            if wait_store is not None
            else None
        )
        self._execution_store = execution_store
        self._recovery_coordinator = (
            RecoveryCoordinator(
                execution_store, handoff_store, project_state_store,
                clock=self._clock, id_factory=self._id_factory,
            )
            if execution_store is not None
            else None
        )
        # QA integration — opt-in, composed exactly like every capability
        # above: supplying qa_engine+qa_policy+qa_run_store enables it,
        # omitting any one preserves the exact behavior of not having QA at
        # all. `qa_engine` is only ever used through the provider-
        # independent `QAEngine` Protocol (`.run(request)`) — this module
        # never imports or isinstance-checks a specific engine
        # implementation (InternalQAEngine or otherwise).
        self._qa_engine = qa_engine
        self._qa_policy = qa_policy or QAPolicy()
        self._qa_run_store = qa_run_store
        self._qa_protected_paths = tuple(qa_protected_paths)

    @property
    def _qa_enabled(self) -> bool:
        return self._qa_engine is not None and self._qa_run_store is not None

    async def run_next_work_item(self, mvp_id: str) -> WorkItemRunResult | None:
        """Runs exactly one eligible WorkItem of ``mvp_id``, if any is eligible.

        Reconciliation (Slice 11b) runs first, unconditionally, if
        ``execution_store`` was supplied: cheap and synchronous, it never
        launches anything itself, only moves a WorkItem stuck behind an
        orphaned/interrupted execution to ``RECOVERY_REQUIRED``. A due
        RECOVERY_REQUIRED WorkItem then takes priority over a due quota
        wait, which in turn takes priority over fresh candidates — never
        more than one of these in the same call, to keep "exactly one
        WorkItem per call" true regardless of which opt-in capabilities are
        enabled.

        Otherwise: eligible means READY (dependencies satisfied) or
        NEEDS_REWORK (QA failed with rework cycles remaining) — the
        latter skips dependency re-checking since it was already
        established. Returns ``None`` when nothing is currently eligible
        and nothing is due.
        """
        if self._recovery_coordinator is not None:
            self._recovery_coordinator.reconcile_mvp(mvp_id)
            recovered = await self._try_resume_recovery_required(mvp_id)
            if recovered is not None:
                return recovered

        if self._wait_coordinator is not None:
            resumed = await self._try_resume_due_wait(mvp_id)
            if resumed is not None:
                return resumed

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
        # Adaptive selection (Slice 17) may raise complexity-estimation/
        # profile-resolution errors here too — those are never caught for
        # a wait conversion (they are not a quota condition) and simply
        # propagate: no development happens this attempt.
        try:
            dev_worker, dev_model, dev_reasoning_effort = await self._select_dev_worker(
                mvp=mvp, work_item=work_item, is_rework=is_rework,
            )
        except NoEligibleWorkerError as exc:
            if self._wait_coordinator is not None:
                wait_result = self._record_dev_wait(
                    project_id=mvp.project_id, mvp_id=mvp_id, work_item=work_item,
                    is_rework=is_rework, diagnostics=exc.diagnostics,
                )
                if wait_result is not None:
                    return wait_result
            raise

        resume_context = self._build_resume_context(work_item.work_item_id) if not is_rework else None
        return await self._execute_work_item_lean(
            mvp_id=mvp_id, mvp=mvp, work_item=work_item, dev_worker=dev_worker,
            is_rework=is_rework, resume_context=resume_context,
            dev_model=dev_model, dev_reasoning_effort=dev_reasoning_effort,
        )

    async def _select_dev_worker(
        self, *, mvp: MVP, work_item: WorkItem, is_rework: bool,
    ) -> tuple[Worker, str | None, str | None]:
        """Resolves the developer for one DEVELOPMENT/REWORK attempt.

        Shared by the fresh-candidate path (``run_next_work_item``) *and*
        both resume paths (``_try_resume_due_wait``,
        ``_try_resume_recovery_required``) — a resumed attempt is held to
        exactly the same rules as a fresh one, never a weaker
        ``Worker.profile()`` default "because it's just a resume" (that
        would silently violate the no-downgrade invariant: see module
        docstring). When ``adaptive_execution_selector`` is configured,
        this reruns the pre-flight from *current* persisted facts every
        time — the Slice 16 fingerprint/cache naturally reuses the
        existing recommendation when nothing relevant changed, and
        naturally produces a fresh one when it did (new handoff/git SHA/
        review findings) — never forced either way here.

        Returns ``(worker, model, reasoning_effort)``; ``model``/
        ``reasoning_effort`` are ``None`` when adaptive execution is not
        configured (caller then falls back to ``Worker.profile()``'s
        default in ``_execute_work_item``, exactly as pre-Slice-17).
        """
        if self._adaptive_execution_selector is not None:
            project = self._project_state_store.get_project(mvp.project_id)
            if self._git_governance_service is not None:
                self._git_governance_service.ensure_runtime_exclusion(project.workspace)
            estimation_request = self._build_estimation_request(
                project=project, mvp_id=mvp.mvp_id, work_item=work_item, is_rework=is_rework,
            )
            selection = await self._adaptive_execution_selector.select(
                estimation_request=estimation_request,
                required_capabilities=work_item.required_capabilities,
            )
            return selection.worker, selection.decision.model, selection.decision.reasoning_effort
        worker = await self._worker_selector.select(
            WorkerSelectionRequest(required_capabilities=work_item.required_capabilities)
        )
        return worker, None, None

    def _build_estimation_request(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, is_rework: bool,
    ) -> ComplexityEstimationRequest:
        """Facts available right now for a development/rework pre-flight.

        Includes the latest handoff/git SHA if one already exists (REWORK
        under Lean is driven by QA findings, attached to the next
        development execution's own instructions — see
        ``_execute_work_item_lean`` — never a separate review-findings
        channel here). The Slice 16 fingerprint decides on its own whether
        an existing recommendation is still reusable — this method never
        second-guesses that by forcing ``force_refresh``, whether called
        fresh or from a resume path.
        """
        latest_handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        git_sha = latest_handoff.git_sha_after if latest_handoff is not None else None
        return ComplexityEstimationRequest(
            project_id=project.project_id, role=DEFAULT_WORK_ITEM_ROLE, workspace=project.workspace,
            objective=work_item.title, acceptance_criteria=work_item.acceptance_criteria,
            mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            latest_handoff=latest_handoff, review_findings=(), git_sha=git_sha,
        )

    # --- LEAN_FEATURE_FLOW (2026-09-16 product decision, the only
    # supported workflow — see ROADMAP.md's dated removal entry for the
    # earlier, heavier GOVERNED_FULL pipeline this superseded) ----------
    #
    # DEV A -> DEV B corrective review -> single QA phase -> merge -> tag.
    # No complexity estimation, no isolated QA Test Authoring/promotion, no
    # separate read-only Review, no separate Final QA phase.

    async def _run_lean_development(
        self, *, project: Project, work_item: WorkItem, worker: Worker,
        model: str | None, reasoning_effort: str | None, instructions: str,
    ) -> ExecutionResult:
        """One real, write-capable execution directly in the governed
        target workspace (no isolation — DEV A/DEV B/a post-QA-FAIL fix
        are all real development, by design). Shared by every lean
        development step; only ``worker``/``instructions`` differ."""
        request = ExecutionRequest(
            execution_id=self._id_factory(), task_id=work_item.work_item_id, worker=worker,
            role=DEFAULT_WORK_ITEM_ROLE, workspace=project.workspace, instructions=instructions,
            initial_event_topic=INITIAL_EVENT_TOPIC, success_topics=frozenset({SUCCESS_TOPIC}),
            failure_topics=frozenset({FAILURE_TOPIC}), timeout_seconds=self._timeout_seconds,
            model=model, reasoning_effort=reasoning_effort,
        )
        result = await self._execution_engine.execute(request)
        if self._git_governance_service is not None:
            self._git_governance_service.capture_head(
                work_item.work_item_id, repository_path=project.workspace,
            )
        return result

    async def _execute_work_item_lean(
        self, *, mvp_id: str, mvp: MVP, work_item: WorkItem, dev_worker: Worker, is_rework: bool,
        resume_context: str | None, dev_model: str | None = None, dev_reasoning_effort: str | None = None,
    ) -> WorkItemRunResult:
        """LEAN_FEATURE_FLOW entry point — mirrors ``_execute_work_item``'s
        signature exactly (same caller contract) so ``run_next_work_item``/
        ``_try_resume_due_wait`` can dispatch to either with no other
        change. On rework (a previous QA FAIL that still has attempts
        left): ``DEV FIX -> QA`` directly, never a second DEV B review. On
        a fresh attempt: ``DEV A -> DEV B corrective review -> QA``.
        """
        self._project_state_store.mark_mvp_running(mvp_id)
        work_item = self._project_state_store.mark_work_item_running(work_item.work_item_id)
        project = self._project_state_store.get_project(mvp.project_id)

        base_sha: str | None = None
        if self._git_governance_service is not None:
            self._git_governance_service.ensure_runtime_exclusion(project.workspace)
            prep_record = self._git_governance_service.prepare_work_item(
                project_id=project.project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
                repository_path=project.workspace,
            )
            base_sha = prep_record.base_sha

        if dev_model is None:
            dev_profile = dev_worker.profile()
            dev_model, dev_reasoning_effort = dev_profile.model, dev_profile.reasoning_effort

        if is_rework:
            latest_handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
            fix_context = resume_context
            if latest_handoff is not None and latest_handoff.open_issues:
                fix_context = f"QA findings from the previous attempt: {latest_handoff.open_issues}"
            dev_result = await self._run_lean_development(
                project=project, work_item=work_item, worker=dev_worker,
                model=dev_model, reasoning_effort=dev_reasoning_effort,
                instructions=_build_dev_instructions(work_item, resume_context=fix_context),
            )
            if dev_result.record.status is not ExecutionStatus.SUCCEEDED:
                work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
                return WorkItemRunResult(
                    work_item=work_item, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
                )
            head_sha = dev_result.record.git_sha_after
            return await self._run_lean_qa_and_finalize(
                project=project, mvp_id=mvp_id, work_item=work_item,
                head_sha=head_sha, base_sha=base_sha or head_sha,
            )

        # --- fresh attempt: DEV A ---
        dev_a_result = await self._run_lean_development(
            project=project, work_item=work_item, worker=dev_worker,
            model=dev_model, reasoning_effort=dev_reasoning_effort,
            instructions=_build_dev_instructions(work_item, resume_context=resume_context),
        )
        if dev_a_result.record.status is not ExecutionStatus.SUCCEEDED:
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            return WorkItemRunResult(
                work_item=work_item, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
            )
        head_after_a = dev_a_result.record.git_sha_after
        self._handoff_store.create(
            handoff_id=self._id_factory(), project_id=project.project_id, mvp_id=mvp_id,
            work_item_id=work_item.work_item_id, objective=work_item.title,
            execution_id=dev_a_result.record.execution_id, worker_id=dev_a_result.record.worker_id,
            next_action="DEV A succeeded — proceeding to DEV B corrective review",
            git_sha_after=head_after_a, created_at=self._clock(),
        )

        try:
            dev_b_worker = await self._worker_selector.select(
                WorkerSelectionRequest(
                    required_capabilities=work_item.required_capabilities, author_worker_id=dev_worker.worker_id,
                )
            )
        except NoEligibleWorkerError as exc:
            if self._wait_coordinator is not None:
                wait = self._wait_coordinator.record_wait(
                    project_id=project.project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
                    phase=WaitPhase.DEV_B_REVIEW, diagnostics=exc.diagnostics,
                )
                if wait is not None:
                    waiting = self._project_state_store.mark_work_item_waiting(work_item.work_item_id)
                    return WorkItemRunResult(work_item=waiting, handoff=None, wait=wait)
            blocked = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id, reason=f"no eligible DEV B (independent developer) available: {exc}",
            )
            return WorkItemRunResult(
                work_item=blocked, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
            )

        return await self._run_lean_dev_b_onward(
            project=project, mvp_id=mvp_id, work_item=work_item, dev_a_worker_id=dev_worker.worker_id,
            dev_b_worker=dev_b_worker, head_after_a=head_after_a, base_sha=base_sha or head_after_a,
        )

    async def _run_lean_dev_b_onward(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, dev_a_worker_id: str,
        dev_b_worker: Worker, head_after_a: str, base_sha: str,
    ) -> WorkItemRunResult:
        """DEV B's corrective review execution (write-capable, direct in
        the governed workspace — never read-only, never isolated: a
        deliberate design choice, see this module's own docstring) through
        to QA. Shared by the fresh-attempt path and the DEV_B_REVIEW wait
        resume, so both go through the exact same continuation."""
        profile = dev_b_worker.profile()
        dev_b_result = await self._run_lean_development(
            project=project, work_item=work_item, worker=dev_b_worker,
            model=profile.model, reasoning_effort=profile.reasoning_effort,
            instructions=_build_dev_b_instructions(work_item, dev_a_worker_id=dev_a_worker_id),
        )
        if dev_b_result.record.status is not ExecutionStatus.SUCCEEDED:
            work_item = self._project_state_store.mark_work_item_failed(work_item.work_item_id)
            return WorkItemRunResult(
                work_item=work_item, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
            )
        head_after_b = dev_b_result.record.git_sha_after
        changed_by_b = head_after_b != head_after_a
        self._handoff_store.create(
            handoff_id=self._id_factory(), project_id=project.project_id, mvp_id=mvp_id,
            work_item_id=work_item.work_item_id, objective=work_item.title,
            execution_id=dev_b_result.record.execution_id, worker_id=dev_b_result.record.worker_id,
            next_action=f"DEV B corrective review complete (changed_code={changed_by_b}) — proceeding to QA",
            git_sha_after=head_after_b, created_at=self._clock(),
        )
        return await self._run_lean_qa_and_finalize(
            project=project, mvp_id=mvp_id, work_item=work_item, head_sha=head_after_b, base_sha=base_sha,
        )

    async def _resume_lean_dev_b_wait(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, due: WaitRecord,
    ) -> WorkItemRunResult:
        """Resumes a DEV_B_REVIEW wait — DEV A already succeeded before
        this wait was ever recorded, so resuming re-enters RUNNING
        directly (never READY) and re-selects DEV B; DEV A is never
        re-run."""
        handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        if handoff is None:
            self._wait_coordinator.resolve(due.wait_id, resolution="gave up: no recovery handoff available")
            blocked = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id, reason="DEV B review wait due but no prior handoff to resume from",
            )
            return WorkItemRunResult(work_item=blocked, handoff=None)

        try:
            dev_b_worker = await self._worker_selector.select(
                WorkerSelectionRequest(
                    required_capabilities=work_item.required_capabilities, author_worker_id=handoff.worker_id,
                )
            )
        except NoEligibleWorkerError as exc:
            return self._requeue_or_give_up(
                project_id=project.project_id, mvp_id=mvp_id, work_item=work_item,
                due=due, diagnostics=exc.diagnostics,
            )

        self._wait_coordinator.resolve(
            due.wait_id, resolution=f"resumed with DEV B worker {dev_b_worker.worker_id!r}"
        )
        work_item = self._project_state_store.mark_work_item_running(work_item.work_item_id)
        base_sha = handoff.git_sha_after
        if self._git_governance_service is not None:
            record = self._reconcile_governed_head(work_item.work_item_id, project.workspace)
            base_sha = record.base_sha
        return await self._run_lean_dev_b_onward(
            project=project, mvp_id=mvp_id, work_item=work_item, dev_a_worker_id=handoff.worker_id,
            dev_b_worker=dev_b_worker, head_after_a=handoff.git_sha_after, base_sha=base_sha,
        )

    async def _run_lean_qa_and_finalize(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, head_sha: str, base_sha: str,
    ) -> WorkItemRunResult:
        """The single QA phase: read-only, deterministic
        (``QAPhase.FINAL_VERIFICATION`` — reused as-is, no new phase
        value). PASS -> merge + tag. Not PASS -> bounded fix-and-retry
        (``QAPolicy.max_qa_cycles``, default 3 total QA attempts): the
        1st/2nd FAIL hand off to a fresh ``DEV FIX -> QA`` rework cycle
        (no DEV B review in between); the 3rd FAIL becomes
        HUMAN_REVIEW_REQUIRED (``BLOCKED`` + a deterministic ROADMAP.md
        TODO) — never a 4th automatic QA attempt.
        """
        if self._git_governance_service is not None:
            violations = self._verify_workspace_matches(project.workspace, head_sha)
            if violations:
                blocked = self._project_state_store.mark_work_item_blocked(
                    work_item.work_item_id,
                    reason=(
                        f"workspace does not match expected head {head_sha!r} before QA "
                        f"(governance violation): {list(violations)!r}"
                    ),
                )
                return WorkItemRunResult(
                    work_item=blocked, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
                )

        request = QARequest(
            project_id=project.project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            workspace=str(project.workspace), base_sha=base_sha, head_sha=head_sha,
            objective=work_item.title, acceptance_criteria=work_item.acceptance_criteria,
            phase=QAPhase.FINAL_VERIFICATION,
        )
        run = await self._run_qa_cycle(request=request, phase=QAPhase.FINAL_VERIFICATION)

        if run.verdict is not None and run.verdict.status is QAVerdictStatus.PASS:
            work_item = self._project_state_store.mark_work_item_completed(work_item.work_item_id)
            self._maybe_finalize_git(
                project=project, work_item=work_item, gate_result=None,
                qa_passed=True, qa_git_sha=head_sha,
            )
            merged_sha = self._maybe_tag_lean_merge(project=project, work_item=work_item)
            handoff = self._handoff_store.create(
                handoff_id=self._id_factory(), project_id=project.project_id, mvp_id=mvp_id,
                work_item_id=work_item.work_item_id, objective=work_item.title,
                execution_id=run.run_id, worker_id=run.engine_id,
                next_action=(
                    "QA passed — feature merged and tagged" if merged_sha
                    else "QA passed — merge eligibility pending"
                ),
                git_sha_after=head_sha, created_at=self._clock(),
            )
            return WorkItemRunResult(work_item=work_item, handoff=handoff)

        attempts_used = self._count_qa_cycles(work_item.work_item_id, QAPhase.FINAL_VERIFICATION)
        reason = run.verdict.reason if run.verdict is not None else "QA produced no verdict"
        if attempts_used >= self._qa_policy.max_qa_cycles:
            self._append_human_review_todo(
                project=project, work_item=work_item, reason=reason, sha=head_sha, attempts=attempts_used,
            )
            blocked = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id,
                reason=(
                    f"HUMAN_REVIEW_REQUIRED: {attempts_used}/{self._qa_policy.max_qa_cycles} "
                    f"QA attempts exhausted — {reason}"
                ),
            )
            return WorkItemRunResult(
                work_item=blocked, handoff=self._handoff_store.latest_for_work_item(work_item.work_item_id),
            )

        handoff = self._create_qa_handoff(project=project, mvp_id=mvp_id, work_item=work_item, run=run, head_sha=head_sha)
        rework = self._project_state_store.mark_work_item_needs_rework(work_item.work_item_id)
        return WorkItemRunResult(work_item=rework, handoff=handoff)

    def _maybe_tag_lean_merge(self, *, project: Project, work_item: WorkItem) -> str | None:
        """Tags the exact merged SHA immediately after a real merge — never
        before. Returns the merged SHA (for the handoff message) or
        ``None`` when nothing was actually merged yet (``auto_merge=False``,
        left at MERGE_READY — no tag in that case)."""
        if self._git_governance_service is None:
            return None
        record = self._git_governance_service.try_get(work_item.work_item_id)
        if record is None or record.status is not GitWorkItemStatus.MERGED or record.merged_sha is None:
            return None
        tag_name = f"feature/{work_item.work_item_id}/done"
        LocalGitWorkspace(project.workspace).create_tag(tag_name, sha=record.merged_sha)
        return record.merged_sha

    def _append_human_review_todo(
        self, *, project: Project, work_item: WorkItem, reason: str, sha: str, attempts: int,
    ) -> None:
        """Deterministic, template-based ROADMAP.md append — never an LLM
        rewrite of the roadmap. Committed on the WorkItem's own still-
        checked-out work branch (never directly on ``main``), via the
        exact same orchestrator-controlled ``stage_and_commit`` QA
        promotion already uses — the work branch stays available for a
        human to pick up exactly where QA left off."""
        if self._git_governance_service is None:
            return
        roadmap_path = Path(project.workspace) / "ROADMAP.md"
        if not roadmap_path.is_file():
            return
        block = (
            f"\n## HUMAN REVIEW REQUIRED — {work_item.work_item_id}\n\n"
            f"QA attempts: {attempts}/{self._qa_policy.max_qa_cycles}\n\n"
            f"Last SHA:\n{sha}\n\n"
            f"Failures:\n- {reason}\n\n"
            "Action:\nHuman review required before resuming.\n"
        )
        with roadmap_path.open("a") as fh:
            fh.write(block)
        LocalGitWorkspace(project.workspace).stage_and_commit(
            ["ROADMAP.md"], message=f"chore: flag {work_item.work_item_id} for human review",
        )

    # --- QA integration (Slice 24) ------------------------------------------

    def _capture_protected_baseline(
        self, *, workspace: str, base_sha: str
    ) -> ProtectedTestBaseline | None:
        """Snapshots ``self._qa_protected_paths`` exactly as they existed
        *at* ``base_sha`` (via ``GitGovernanceService.read_file_at`` —
        never a direct git/subprocess call from this module, and never
        the current working tree, which may already reflect a later
        commit by the time this runs) — ``None`` when no protected paths
        are configured, or git governance is not configured (protected-
        test enforcement is fully opt-in and requires SHA-scoped file
        reads that only git governance currently provides). Hashing
        matches ``qa_protection.hash_file`` exactly (raw bytes, no text
        decoding), so the later working-tree comparison stays correct.
        """
        if not self._qa_protected_paths or self._git_governance_service is None:
            return None
        files = []
        for path in self._qa_protected_paths:
            blob = self._git_governance_service.read_file_at(workspace, ref=base_sha, path=path)
            digest = hashlib.sha256(blob).hexdigest() if blob is not None else None
            files.append(ProtectedTestFileState(path=path, digest=digest))
        return ProtectedTestBaseline(base_sha=base_sha, files=tuple(files))

    def _count_qa_cycles(self, work_item_id: str, phase: QAPhase) -> int:
        if self._qa_run_store is None:
            return 0
        return sum(1 for run in self._qa_run_store.list_for_work_item(work_item_id) if run.phase is phase)

    def _create_qa_handoff(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, run: QARun, head_sha: str,
    ) -> HandoffRecord:
        """Durable QA -> coding handoff (Part I) — references/evidence
        only, never a full log dump. ``execution_id``/``worker_id``
        reference the QA run itself (``run.run_id``/``run.engine_id``),
        not a worker execution — no worker ran here, only the QA engine.
        """
        result = self._qa_run_store.get_result(run.run_id)
        regressions = result.regressions if result is not None else ()
        classifications = (
            tuple(c.value for c in result.failure_classifications) if result is not None else ()
        )
        recommended = result.recommended_actions if result is not None else ()
        open_issues = (
            f"QA verdict={run.verdict.status.value if run.verdict else 'unknown'}: "
            f"{run.verdict.reason if run.verdict else '(no verdict)'}; "
            f"regressions={list(regressions)!r}; failure_classifications={list(classifications)!r}; "
            f"recommended_actions={list(recommended)!r}"
        )
        return self._handoff_store.create(
            handoff_id=self._id_factory(), project_id=project.project_id, mvp_id=mvp_id,
            work_item_id=work_item.work_item_id, objective=work_item.title,
            execution_id=run.run_id, worker_id=run.engine_id,
            open_issues=open_issues,
            next_action=(
                "Fix the product behavior per the acceptance criteria without weakening, "
                "skipping, or removing any existing/protected test — do not simply rewrite a "
                "failing test's expectations to make it pass."
            ),
            git_sha_after=head_sha, created_at=self._clock(),
        )

    async def _run_qa_cycle(self, *, request: QARequest, phase: QAPhase) -> QARun:
        """Provider-independent QA orchestration — the ``MVPManager``-side
        analog of ``internal_qa_engine.run_qa_cycle``, but built purely on
        the ``QAEngine`` Protocol (``.run(request) -> QAResult``) so a
        future non-``InternalQAEngine`` implementation needs no changes
        here. Sequences exactly Part I: create -> RUNNING -> engine ->
        QAResult insert -> deterministic QAVerdict -> terminal status;
        evidence is always persisted before this returns.

        ``QAEngine.run`` is synchronous per the Protocol (an external
        engine may simply block on an HTTP call) — called via
        ``asyncio.to_thread`` so a *sync* engine (like
        ``InternalQAEngine.run``, which itself wraps ``asyncio.run()``)
        never raises "cannot be called from a running event loop" when
        invoked from this already-async method.

        Manifest: if the configured engine exposes ``build_plan``/
        ``build_manifest`` (duck-typed, never an ``isinstance`` check —
        ``InternalQAEngine`` happens to have them), reuses them for a
        manifest that genuinely reflects the selected tests/invariants;
        otherwise falls back to a manifest built from
        ``request``/``self._qa_policy`` alone — still real, provider-
        independent data, never a lie.
        """
        build_plan = getattr(self._qa_engine, "build_plan", None)
        build_manifest = getattr(self._qa_engine, "build_manifest", None)
        manifest: QAEvidenceManifest | None = None
        if callable(build_plan) and callable(build_manifest):
            plan = build_plan(request, phase=phase)
            manifest = build_manifest(plan)
        if manifest is None:
            manifest = QAEvidenceManifest(
                required_test_ids=request.required_test_ids,
                required_invariant_ids=request.required_invariants or self._qa_policy.required_invariant_ids,
                required_engines=self._qa_policy.required_engines,
            )

        run = new_qa_run(
            project_id=request.project_id, mvp_id=request.mvp_id, work_item_id=request.work_item_id,
            engine_id=getattr(self._qa_engine, "engine_id", "external"), phase=phase,
            expected_base_sha=request.base_sha, expected_head_sha=request.head_sha,
            policy=self._qa_policy, manifest=manifest, clock=self._clock, id_factory=self._id_factory,
        )
        self._qa_run_store.create(run)
        self._qa_run_store.update_status(run.run_id, QARunStatus.RUNNING)

        try:
            result = await asyncio.to_thread(self._qa_engine.run, request)
        except Exception:  # noqa: BLE001 - any engine failure is an infra outcome, never a fabricated PASS
            self._qa_run_store.update_status(run.run_id, QARunStatus.FAILED)
            run = self._qa_run_store.get(run.run_id)
            verdict = evaluate_qa_verdict(run=run, result=None, now=self._clock())
            self._qa_run_store.record_verdict(run.run_id, verdict)
            return self._qa_run_store.get(run.run_id)

        self._qa_run_store.record_result(run.run_id, result)
        self._qa_run_store.update_status(run.run_id, QARunStatus.COMPLETED)
        run = self._qa_run_store.get(run.run_id)

        unauthorized = False
        baseline = self._capture_protected_baseline(workspace=request.workspace, base_sha=request.base_sha)
        if baseline is not None:
            changes = compare_protected_test_baseline(baseline, request.workspace)
            unauthorized = has_unauthorized_change(changes)

        verdict = evaluate_qa_verdict(
            run=run, result=result, now=self._clock(),
            unauthorized_protected_change=unauthorized,
            read_only_violation=result.read_only_violation, read_only_unprovable=result.read_only_unprovable,
        )
        self._qa_run_store.record_verdict(run.run_id, verdict)
        return self._qa_run_store.get(run.run_id)

    def _build_resume_context(self, work_item_id: str) -> str | None:
        """Grounds a fresh (possibly different) worker in the last handoff.

        Only produces text when a handoff already exists for this exact
        WorkItem (i.e. a previous attempt actually ran) — a genuinely
        first-ever attempt has none, and this stays silent, unchanged from
        before Slice 11.
        """
        handoff = self._handoff_store.latest_for_work_item(work_item_id)
        if handoff is None:
            return None
        text = (
            "This work item was previously attempted (a different worker may have "
            f"been involved). Last handoff — next action: {handoff.next_action}; "
            f"git SHA after that attempt: {handoff.git_sha_after or '(none)'}."
        )
        if handoff.open_issues:
            text += f" Open issues from that attempt: {handoff.open_issues}"
        return text

    def _find_last_developer_handoff(self, work_item_id: str) -> HandoffRecord | None:
        """The most recent handoff that came out of an actual development-role execution.

        Deliberately NOT ``HandoffStore.latest_for_work_item``: a recovery
        handoff created for an orphaned/interrupted *review* execution
        records the reviewer's own identity as an audit fact and can be
        the most recent handoff overall — using it as "the author" would
        be wrong (and could even make a reviewer equal its own author).
        Requires ``execution_store`` (only called when recovery is
        configured).
        """
        for handoff in reversed(self._handoff_store.list_for_work_item(work_item_id)):
            if handoff.execution_id is None:
                continue
            try:
                execution = self._execution_store.get(handoff.execution_id)
            except UnknownExecutionError:
                continue
            if execution.role == DEFAULT_WORK_ITEM_ROLE:
                return handoff
        return None

    def _record_dev_wait(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem, is_rework: bool,
        diagnostics: tuple[ProviderSelectionDiagnostic, ...],
    ) -> WorkItemRunResult | None:
        """Turns a developer-selection failure into a durable wait, if diagnosable.

        Returns ``None`` (never raises) when no reliable reset is known —
        the caller must then re-raise the original exception unchanged,
        preserving pre-Slice-11 behavior exactly.
        """
        wait = self._wait_coordinator.record_wait(
            project_id=project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            phase=WaitPhase.REWORK if is_rework else WaitPhase.DEVELOPMENT,
            diagnostics=diagnostics,
        )
        if wait is None:
            return None
        waiting_item = self._project_state_store.mark_work_item_waiting(work_item.work_item_id)
        return WorkItemRunResult(work_item=waiting_item, handoff=None, wait=wait)

    async def _try_resume_due_wait(self, mvp_id: str) -> WorkItemRunResult | None:
        """Resumes the one due wait for this MVP, if any — never polls.

        Re-probes WorkerSelector right now: a theoretical reset time is
        never treated as proof of availability. On success, always starts
        a brand new execution (development/rework) or a brand new review
        attempt — never reuses the interrupted one.
        """
        due = self._wait_coordinator.find_due(mvp_id)
        if due is None:
            return None

        work_item = self._project_state_store.get_work_item(due.work_item_id)
        mvp = self._project_state_store.get_mvp(mvp_id)
        project = self._project_state_store.get_project(mvp.project_id)

        if due.phase is WaitPhase.DEV_B_REVIEW:
            return await self._resume_lean_dev_b_wait(project=project, mvp_id=mvp_id, work_item=work_item, due=due)

        is_rework = due.phase is WaitPhase.REWORK
        try:
            dev_worker, dev_model, dev_reasoning_effort = await self._select_dev_worker(
                mvp=mvp, work_item=work_item, is_rework=is_rework,
            )
        except NoEligibleWorkerError as exc:
            return self._requeue_or_give_up(
                project_id=mvp.project_id, mvp_id=mvp_id, work_item=work_item,
                due=due, diagnostics=exc.diagnostics,
            )

        self._wait_coordinator.resolve(due.wait_id, resolution=f"resumed with worker {dev_worker.worker_id!r}")
        work_item = (
            self._project_state_store.mark_work_item_needs_rework(work_item.work_item_id)
            if is_rework
            else self._project_state_store.mark_work_item_ready(work_item.work_item_id)
        )
        resume_context = self._build_resume_context(work_item.work_item_id)
        return await self._execute_work_item_lean(
            mvp_id=mvp_id, mvp=mvp, work_item=work_item, dev_worker=dev_worker,
            is_rework=is_rework, resume_context=resume_context,
            dev_model=dev_model, dev_reasoning_effort=dev_reasoning_effort,
        )

    async def _try_resume_recovery_required(self, mvp_id: str) -> WorkItemRunResult | None:
        """Resumes the one RECOVERY_REQUIRED WorkItem of this MVP, if any.

        Unlike a quota wait, there is no deadline to check — reconciliation
        already established that this WorkItem's execution is genuinely
        orphaned/interrupted, so it is immediately re-orchestrable. Both DEV
        A and DEV B executions carry the same ``DEFAULT_WORK_ITEM_ROLE`` —
        ``_run_lean_development`` (shared by both) always resumes as a
        fresh development attempt; there is no separate phase to
        distinguish here.
        """
        candidates = sorted(
            (
                wi for wi in self._project_state_store.list_work_items(mvp_id)
                if wi.status is WorkItemStatus.RECOVERY_REQUIRED
            ),
            key=lambda wi: wi.work_item_id,
        )
        if not candidates:
            return None

        work_item = candidates[0]
        mvp = self._project_state_store.get_mvp(mvp_id)

        dev_worker, dev_model, dev_reasoning_effort = await self._select_dev_worker(
            mvp=mvp, work_item=work_item, is_rework=False,
        )
        resume_context = self._build_resume_context(work_item.work_item_id)
        return await self._execute_work_item_lean(
            mvp_id=mvp_id, mvp=mvp, work_item=work_item, dev_worker=dev_worker,
            is_rework=False, resume_context=resume_context,
            dev_model=dev_model, dev_reasoning_effort=dev_reasoning_effort,
        )

    def _requeue_or_give_up(
        self, *, project_id: str, mvp_id: str, work_item: WorkItem, due: WaitRecord,
        diagnostics: tuple[ProviderSelectionDiagnostic, ...],
    ) -> WorkItemRunResult:
        """A due wait's re-probe still found no eligible worker/reviewer.

        Never blindly resumes on a theoretical reset alone: either a new
        reliable deadline is known (requeue, still WAITING) or it isn't
        (give up to BLOCKED — an explicit, visible, non-terminal-forever*
        state a human can act on). *BLOCKED itself is terminal in this
        store, matching every other unresolvable-without-intervention path
        in this codebase (Slice 9's exhausted rework cycles, for example).
        """
        new_wait = self._wait_coordinator.record_wait(
            project_id=project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            phase=due.phase, diagnostics=diagnostics,
            source_execution_id=due.source_execution_id, source_handoff_id=due.source_handoff_id,
        )
        if new_wait is not None:
            self._wait_coordinator.resolve(
                due.wait_id, resolution="still unavailable at deadline — requeued with a new reset"
            )
            return WorkItemRunResult(work_item=work_item, handoff=None, wait=new_wait)

        self._wait_coordinator.resolve(due.wait_id, resolution="gave up: no reliable reset known anymore")
        blocked = self._project_state_store.mark_work_item_blocked(
            work_item.work_item_id, reason="no eligible worker and no reliable reset known"
        )
        return WorkItemRunResult(work_item=blocked, handoff=None)

    #: Real, independently-verified evidence (Slice 24 self-dogfood
    #: acceptance) that Ralph itself always commits under this prefix as
    #: pure internal bookkeeping — never authored content. Matches the
    #: same convention already used for QA-authoring's own unauthorized-
    #: file detection (``internal_qa_engine._RUNTIME_NOISE_SEGMENTS``).
    #: References ``git_governance.RALPH_RUNTIME_NOISE_PREFIXES`` directly
    #: (found via a real external-project pilot: keeping this as its own,
    #: separately-declared tuple risked a second, silently-divergent
    #: taxonomy of "what counts as Ralph runtime noise" from the one
    #: ``ensure_runtime_exclusion`` actually installs in the target's own
    #: local git exclude).
    _RALPH_HOUSEKEEPING_PREFIX = RALPH_RUNTIME_NOISE_PREFIXES

    def _reconcile_governed_head(self, work_item_id: str, workspace: str | Path) -> GitWorkItemRecord:
        """Re-verifies the governed branch is exactly where MVPManager
        expects before a review reads it — tolerating a HEAD drift only
        when it is provably legitimate, via either of two independent,
        content-verified signals (Slice 24 fix, found via real-provider
        self-dogfood acceptance — Ralph itself commits its own ``.ralph/``
        housekeeping state on every real execution it runs, including a
        read-only complexity-estimation execution (``role="estimator"``,
        adaptive execution's own pre-flight) that this module never
        expected to move HEAD, since only development and QA authoring
        call ``capture_head`` explicitly):

        1. The new head matches a real, persisted ``ExecutionRecord`` for
           this exact WorkItem (when ``execution_store`` is configured).
           Estimation executions are keyed by content fingerprint, not
           this WorkItem's id, so in practice this alone never explains an
           estimator-caused drift — kept as a second, independent signal
           for any future/legitimate SHA-audited case, never relied upon
           alone here.
        2. Every file that actually changed between the expected and
           actual head — independently computed via ``git diff
           --name-only``, never trusted from any caller's claim — falls
           under ``.ralph/``. A single non-noise file anywhere in that
           diff still fails closed (``GitHeadDriftError``): this never
           widens what counts as legitimate, it only recognizes Ralph's
           own already-accepted (Slice 23/24) housekeeping noise.
        """
        assert self._git_governance_service is not None
        known_execution_shas: frozenset[str] = frozenset()
        if self._execution_store is not None:
            known_execution_shas = frozenset(
                record.git_sha_after
                for record in self._execution_store.list_for_task(work_item_id)
                if record.git_sha_after
            )
        return self._git_governance_service.reconcile(
            work_item_id, repository_path=workspace, known_execution_shas=known_execution_shas,
            noise_path_prefixes=self._RALPH_HOUSEKEEPING_PREFIX,
        )

    def _verify_workspace_matches(self, workspace: str | Path, expected_sha: str) -> tuple[str, ...]:
        """Returns the non-noise files that make the LIVE workspace
        diverge from ``expected_sha`` — committed drift, uncommitted
        tracked edits, and untracked files are all covered in one pass
        via ``working_tree_changed_files`` (real ``git diff``/``git
        status``, never a claim taken on trust). Empty return means the
        workspace is functionally clean at ``expected_sha`` (only
        ``.ralph/`` noise, if anything, differs).

        Added after a real external-project pilot (found via
        ``scripts/run_external_project_pilot.py`` against a genuine
        third-party repo, never reproduced by self-dogfooding this
        control plane) showed a REAL review execution — never verified
        read-only anywhere, unlike development/QA-authoring — could write
        functional, uncommitted changes that a later Quality Gate or
        Final QA Verification would then silently execute against,
        reporting a verdict bound to a SHA that did not reflect what was
        actually tested. This method never raises itself; callers decide
        the governance consequence (always ``BLOCKED`` today — a
        structural integrity problem, never something rework/retry can
        fix on its own).
        """
        changed = working_tree_changed_files(Path(workspace), expected_sha)
        return tuple(f for f in changed if not f.startswith(self._RALPH_HOUSEKEEPING_PREFIX))

    def _maybe_finalize_git(
        self, *, project: Project, work_item: WorkItem,
        gate_result: QualityGateResult | None,
        qa_passed: bool | None = None, qa_git_sha: str | None = None,
    ) -> None:
        """Computes merge eligibility once a WorkItem reaches ``COMPLETED``,
        and merges immediately if ``policy.auto_merge`` says so —
        otherwise leaves it at ``MERGE_READY`` for a later explicit merge.
        A no-op when git governance is not configured.

        Never trusts a live ``gate_result``/``qa_passed`` alone: either can
        be ``None``/absent here (a resumed completion has no live
        ``gate_result``) — in that case the latest persisted evidence is
        read back from ``validation_store``/``qa_run_store`` instead, so
        eligibility is computed identically whether the evidence came from
        this exact call or survived a cold restart.

        ``qa_passed=None`` means QA is not enabled for this MVPManager at
        all — ``qa_required`` is then never passed as True. When QA *is*
        enabled, this method's caller only ever calls it after Lean's own
        QA phase already returned PASS (a QA FAIL/INCONCLUSIVE never
        reaches this method at all — the WorkItem is REWORK/BLOCKED
        instead), so ``qa_passed`` is only ever ``True`` or ``None`` in
        practice; the restart-replay fallback below still re-derives it
        honestly from ``qa_run_store`` regardless. Review is never checked
        here (LEAN_FEATURE_FLOW has no separate reviewer phase —
        ``GitGovernancePolicy.require_review`` must be configured
        ``False`` by any caller of this codebase's lean pipeline).
        """
        if self._git_governance_service is None:
            return

        gate_passed: bool | None = None
        gate_git_sha: str | None = None
        if gate_result is not None:
            gate_passed = gate_result.passed
            gate_git_sha = gate_result.git_sha
        elif self._validation_store is not None:
            latest_gate = self._validation_store.latest_gate_result_for_work_item(work_item.work_item_id)
            if latest_gate is not None:
                gate_passed = latest_gate.passed
                gate_git_sha = latest_gate.git_sha

        qa_required = False
        qa_run_terminal: bool | None = None
        if self._qa_enabled:
            qa_required = self._qa_policy.qa_required
            if qa_passed is None and self._qa_run_store is not None:
                latest_qa = self._qa_run_store.latest_for_work_item(work_item.work_item_id)
                if latest_qa is not None and latest_qa.phase is QAPhase.FINAL_VERIFICATION:
                    qa_passed = latest_qa.verdict is not None and latest_qa.verdict.status is QAVerdictStatus.PASS
                    qa_git_sha = latest_qa.expected_head_sha
                    qa_run_terminal = latest_qa.status in (QARunStatus.COMPLETED, QARunStatus.FAILED)
            else:
                qa_run_terminal = True

        eligibility = self._git_governance_service.compute_merge_eligibility(
            work_item.work_item_id, repository_path=project.workspace,
            work_item_status=work_item.status.value,
            gate_passed=gate_passed, gate_git_sha=gate_git_sha,
            review_approved=None, review_git_sha=None,
            qa_required=qa_required, qa_passed=qa_passed, qa_git_sha=qa_git_sha,
            qa_run_terminal=qa_run_terminal, noise_path_prefixes=self._RALPH_HOUSEKEEPING_PREFIX,
        )
        if eligibility.mergeable and self._git_governance_service.policy.auto_merge:
            self._git_governance_service.merge(
                work_item.work_item_id, repository_path=project.workspace, eligibility=eligibility,
                noise_path_prefixes=self._RALPH_HOUSEKEEPING_PREFIX,
            )
