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

A plain RUNNING/REVIEWING WorkItem is never picked up again by the normal
candidate query: `run_next_work_item`'s fresh-candidate step only ever
selects from READY/NEEDS_REWORK items, and `refresh_readiness` never
touches a WorkItem that is not PLANNED. Recognizing *why* a WorkItem is
still RUNNING/REVIEWING (a live in-progress attempt vs. one orphaned by a
crash) is not something this module — or `RalphExecutionEngine` for
`ExecutionRecord` — decides implicitly; see EXECUTION-LEVEL RECOVERY below
for the explicit reconciliation this slice performs before that query
ever runs.

QUOTA WAITING / DURABLE RESUME (Slice 11a):

Supplying an optional ``wait_store`` enables a third opt-in capability,
composed the same way as the two before it: when `WorkerSelector` raises
`NoEligibleWorkerError`/`ReviewIndependenceError` for the developer or the
reviewer, this module asks the error's own ``diagnostics`` (a read-only,
`WorkerSelector`-authored fact list — never re-derived or duplicated here)
whether a plausible next retry moment is known. If — and only if — at
least one candidate provider is diagnosed ``quota_exhausted`` with a real
`QuotaWindow.reset_at`, the WorkItem moves to `WorkItemStatus.WAITING` and
a durable `orchestrator.wait.WaitRecord` is persisted with that exact
deadline. If no reliable deadline is known, behavior is unchanged from
Slice 9: the exception propagates (developer side) or the WorkItem is
`BLOCKED` (reviewer side) — this module never invents a wait.

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

EXECUTION-LEVEL RECOVERY (Slice 11b):

Distinct from quota waiting above: this handles an *execution* that never
reached a reliable terminal outcome — a RUNNING `ExecutionRecord` found
orphaned after a restart (the process that owned it is gone; its real
fate is unknown), or one that came back `INTERRUPTED` (e.g. a
`RalphExecutionEngine` timeout) whose WorkItem never got moved out of
RUNNING/REVIEWING before a crash. Supplying an optional ``execution_store``
enables a fourth opt-in capability: a `orchestrator.recovery.
RecoveryCoordinator` reconciles every RUNNING/REVIEWING WorkItem of the
MVP at the very start of every `run_next_work_item` call — cheap,
synchronous, no execution launched by the reconciliation itself. An
orphaned/interrupted execution is NEVER relaunched and NEVER flipped back
toward RUNNING; it stays exactly as `RECOVERY_REQUIRED`/`INTERRUPTED`
forever, permanent audit history. Instead, the WorkItem moves to
`WorkItemStatus.RECOVERY_REQUIRED` (immediately re-orchestrable, no
deadline to wait out, unlike `WAITING`), backed by a durable recovery
handoff (reused if one already exists for that exact execution, else
synthesized from persisted `ExecutionRecord`/`WorkItem`/prior-handoff/
review facts — never by asking the interrupted worker for a summary). The
very next thing this module does is resume it: a fresh `WorkerSelector`
selection (possibly a different worker/provider) and a **brand-new**
`execution_id` — never the orphaned/interrupted one. The same principle
applies symmetrically to a review execution: it stays in `REVIEWING` (no
fantom `APPROVED`/`REJECTED` `ReviewRecord` is ever fabricated for an
interruption), and resuming selects a fresh, still-independent reviewer.

ADAPTIVE DEVELOPMENT/REWORK SELECTION (Slice 17):

Supplying an optional ``adaptive_execution_selector`` enables a fifth
opt-in capability, composed the same way as the ones before it: every
DEVELOPMENT/REWORK path that resolves a developer —  the fresh-candidate
path in `run_next_work_item`, the quota-wait resume
(`_try_resume_due_wait`), and the execution-recovery resume
(`_try_resume_recovery_required`) — shares one helper,
`_select_dev_worker`, so a *resumed* attempt is held to exactly the same
rules as a fresh one. Each call runs a complexity pre-flight
(`orchestrator.complexity_estimation`, Slice 16) from *current* persisted
facts (latest handoff, latest review findings, git SHA — re-read every
time, never cached in a `WaitRecord`/recovery handoff) and resolves a
concrete `Worker`/`ExecutionProfile` (`orchestrator.adaptive_execution`,
Slice 17) instead of a plain `WorkerSelector.select(...)` +
`Worker.profile()` default. A resume is NEVER allowed to reason "it's
just a resume, use the default profile" — that would silently violate
the no-downgrade invariant (e.g. a COMPLEX-tier wait must never resume
into a STANDARD-only worker just because it happens to be available).
Because the pre-flight is rebuilt from current facts on every resume, the
Slice 16 fingerprint/cache does the right thing automatically: unchanged
facts reuse the existing recommendation (no new Ralph estimator call, no
tier drift), while a changed handoff/git SHA/review findings naturally
produce a fresh fingerprint and a fresh recommendation — this module
never forces or forbids that either way.

Every invariant from Slices 15/16/17 stays enforced end to end: no silent
downgrade below the recommended quality tier, no fallback to a default
profile on any pre-flight/selection failure (those exceptions propagate
unchanged — this module never catches
`NoReliableRecommendationError`/`EstimatorProfileNotConfiguredError`/
`InvalidRecommendationPayloadError`/`NoCapableProfileError` to paper over
them with a default), and the resulting `AdaptiveExecutionDecision` is
what actually parameterizes the `ExecutionRequest` (`model`/
`reasoning_effort`), never `Worker.profile()`'s own default. A worker can
freely differ between the original attempt and a resume (e.g. Victor/deep
timed out, Alice/deep resumes) — no worker-continuity affinity is ever
assumed or enforced.

ADAPTIVE REVIEW SELECTION (Slice 19):

`_run_review` — the single method already shared by every path that
resolves a reviewer (the fresh path via `_execute_work_item`, the
quota-wait resume via `_resume_review_wait`, and the execution-recovery
resume via `_try_resume_recovery_required`'s review branch, both of the
latter through `_run_review_from_handoff`) — now goes through
`_select_reviewer_worker`, the exact same pattern as `_select_dev_worker`:
when `adaptive_execution_selector` is configured, a fresh
`ComplexityEstimationRequest` (role=`REVIEWER_ROLE`, never
`DEFAULT_WORK_ITEM_ROLE` — so the Slice 16 fingerprint/cache is
independent of development's own pre-flight by construction:
development=SIMPLE and review=COMPLEX, or the reverse, are both possible
real outcomes) drives a real `AdaptiveExecutionSelector.select()` call
instead of a plain `WorkerSelector.select()` + `Worker.profile()` default.
Because all three call sites already shared `_run_review`, no separate
wiring was needed per resume path — a resumed review is automatically
held to the same rules as a fresh one, exactly mirroring Slice 17's
development-resume fix.

`author_worker_id` is always forwarded through to
`AdaptiveExecutionSelector.select()` (which forwards it, in turn, to
`WorkerSelectionRequest.author_worker_id`) — `ReviewPolicy`'s author!=
reviewer guarantee and the existing cross-provider review-independence
policy (`prefer_distinct_provider_for_review`/
`require_distinct_provider_for_review`) are never bypassed or weakened by
the adaptive layer; they are the same `WorkerSelector`-owned checks as
before, merely reached through one more layer. `ReviewRecord`'s own
`__post_init__` invariant (`reviewer_worker_id != author_worker_id`) is
the final, structural backstop either way.

A quota-exhausted-for-the-required-tier reviewer never falls back to the
author or to a weaker tier: `NoEligibleWorkerError`/`ReviewIndependenceError`/
`UnknownWorkerError` from the adaptive selector's own `WorkerSelector`
call are still the only exceptions `_run_review` catches to consider a
`WAITING`/`BLOCKED` outcome — exactly as before Slice 19. A structural
incapacity discovered by the adaptive layer itself
(`NoReliableRecommendationError`/`EstimatorProfileNotConfiguredError`/
`InvalidRecommendationPayloadError`/`NoCapableProfileError`) is never
caught here either, symmetric with development: it propagates, fail-
closed, never mislabeled as a diagnosable quota wait.

Release planning and roadmap synthesis are made adaptive by this slice
too, but entirely within `orchestrator.planning` — see that module's own
docstring; nothing in `MVPManager` changes for either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from orchestrator.adaptive_execution import AdaptiveExecutionSelector
from orchestrator.complexity_estimation import ComplexityEstimationRequest
from orchestrator.execution_store import ExecutionStatus, ExecutionStore, UnknownExecutionError
from orchestrator.git_governance import GitGovernanceService
from orchestrator.handoff import HandoffRecord, HandoffStore
from orchestrator.project_state import MVP, Project, ProjectStateStore, WorkItem, WorkItemStatus
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    ExecutionResult,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.recovery import RecoveryCoordinator
from orchestrator.review import (
    ReviewFinding,
    ReviewPolicy,
    ReviewRecord,
    ReviewStatus,
    ReviewStore,
    parse_findings,
)
from orchestrator.validation import QualityGateResult, QualityGateRunner, ValidationStore
from orchestrator.wait import WaitCoordinator, WaitPhase, WaitRecord, WaitStore
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ProviderSelectionDiagnostic,
    ReviewIndependenceError,
    UnknownWorkerError,
    Worker,
    WorkerSelectionRequest,
    WorkerSelector,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_WORK_ITEM_ROLE = "developer"
REVIEWER_ROLE = "reviewer"
# The reviewer *role* (above) and the reviewer *capability* (below) are
# deliberately distinct: role is a logical label used for
# ExecutionRequest/ExecutionRecord identification and logging,
# capability is what WorkerSelector actually matches against a Worker's
# configured ``capabilities`` (config/workers.yaml). The canonical
# capability string is "code_review" — it must match config/workers.yaml
# exactly, never a synonym, and never both at once.
REVIEW_CAPABILITY = "code_review"
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


def _build_dev_instructions(work_item: WorkItem, *, resume_context: str | None) -> str:
    criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "- (none specified)"
    rework_block = f"{resume_context}\n\n" if resume_context else ""
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
class _RecoveredExecutionRecordFacts:
    """The 3 fields `_run_review` actually reads off `ExecutionResult.record`.

    Used only when resuming a REVIEW-phase wait after a restart: there is
    no live `ExecutionResult` to pass anymore (the process that produced
    it may no longer exist), only the durable facts a past `HandoffRecord`
    already captured. Duck-typed so `_run_review` needs no special-casing.
    """

    execution_id: str
    worker_id: str
    git_sha_after: str | None


@dataclass(frozen=True, slots=True)
class _RecoveredExecutionResult:
    record: _RecoveredExecutionRecordFacts


@dataclass(frozen=True, slots=True)
class WorkItemRunResult:
    """What happened when MVPManager ran one WorkItem.

    ``handoff`` is ``None`` exactly when nothing was executed this call —
    a fresh WAITING (Slice 11: no eligible worker at all, but a reset is
    known) or a give-up to BLOCKED with no execution attempted. ``wait``
    is set only when this call's outcome was recording or updating a
    durable wait (never set on a resumed, executed, or terminal outcome).
    """

    work_item: WorkItem
    handoff: HandoffRecord | None
    gate_result: QualityGateResult | None = None
    review_result: ReviewRecord | None = None
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
        quality_gate_runner: QualityGateRunner | None = None,
        review_store: ReviewStore | None = None,
        review_policy: ReviewPolicy | None = None,
        wait_store: WaitStore | None = None,
        execution_store: ExecutionStore | None = None,
        adaptive_execution_selector: AdaptiveExecutionSelector | None = None,
        validation_store: ValidationStore | None = None,
        git_governance_service: GitGovernanceService | None = None,
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
                execution_store, handoff_store, project_state_store, review_store,
                clock=self._clock, id_factory=self._id_factory,
            )
            if execution_store is not None
            else None
        )

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
        NEEDS_REWORK (a previous review rejected it and rework cycles
        remain) — the latter skips dependency re-checking since it was
        already established. Returns ``None`` when nothing is currently
        eligible and nothing is due.
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
        return await self._execute_work_item(
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

    async def _select_reviewer_worker(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, author_worker_id: str,
        git_sha: str | None, previous_findings: tuple[ReviewFinding, ...] | None,
    ) -> tuple[Worker, str | None, str | None]:
        """Resolves the reviewer for one review attempt (Slice 19).

        Mirrors ``_select_dev_worker`` exactly: shared by every path that
        resolves a reviewer — the fresh path (``_run_review`` called from
        ``_execute_work_item``), the quota-wait resume
        (``_resume_review_wait``), and the execution-recovery resume
        (``_try_resume_recovery_required``'s review branch) — because all
        three already funnel through this same ``_select_reviewer_worker``
        (via ``_run_review``/``_run_review_from_handoff``), a resumed
        review is held to exactly the same adaptive rules as a fresh one,
        with no separate wiring needed per resume path.

        ``author_worker_id`` is always forwarded to
        ``AdaptiveExecutionSelector.select()``/``WorkerSelector.select()``
        so the existing author!=reviewer guarantee and cross-provider
        review-independence policy apply exactly as before — this method
        never weakens or bypasses either.

        Returns ``(worker, model, reasoning_effort)``; ``model``/
        ``reasoning_effort`` are ``None`` when adaptive execution is not
        configured (caller then falls back to ``Worker.profile()``'s
        default, exactly as pre-Slice-19).
        """
        if self._adaptive_execution_selector is not None:
            estimation_request = self._build_review_estimation_request(
                project=project, mvp_id=mvp_id, work_item=work_item,
                git_sha=git_sha, previous_findings=previous_findings,
            )
            selection = await self._adaptive_execution_selector.select(
                estimation_request=estimation_request,
                required_capabilities=frozenset({REVIEW_CAPABILITY}),
                author_worker_id=author_worker_id,
            )
            return selection.worker, selection.decision.model, selection.decision.reasoning_effort
        reviewer = await self._worker_selector.select(
            WorkerSelectionRequest(
                required_capabilities=frozenset({REVIEW_CAPABILITY}), author_worker_id=author_worker_id,
            )
        )
        return reviewer, None, None

    def _build_review_estimation_request(
        self, *, project: Project, mvp_id: str, work_item: WorkItem,
        git_sha: str | None, previous_findings: tuple[ReviewFinding, ...] | None,
    ) -> ComplexityEstimationRequest:
        """Facts available right now for a review pre-flight.

        Deliberately its own ``role`` (``REVIEWER_ROLE``, never
        ``DEFAULT_WORK_ITEM_ROLE``) — the Slice 16 fingerprint already
        includes ``role``, so this is independently estimated/cached from
        development's own pre-flight by construction: development=SIMPLE
        and review=COMPLEX (or the reverse) are both possible real
        outcomes, never one copied from the other. ``review_findings`` is
        populated whenever a previous review exists (a re-review after
        rework), never gated on a separate "is this rework" flag the way
        the development pre-flight is — a review pre-flight always wants
        whatever review history exists for this exact WorkItem.
        """
        latest_handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        return ComplexityEstimationRequest(
            project_id=project.project_id, role=REVIEWER_ROLE, workspace=project.workspace,
            objective=work_item.title, acceptance_criteria=work_item.acceptance_criteria,
            mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            latest_handoff=latest_handoff, review_findings=previous_findings or (), git_sha=git_sha,
        )

    def _build_estimation_request(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, is_rework: bool,
    ) -> ComplexityEstimationRequest:
        """Facts available right now for a development/rework pre-flight.

        REWORK includes the latest review's findings (never for a first
        attempt); both include the latest handoff/git SHA if one already
        exists. The Slice 16 fingerprint decides on its own whether an
        existing recommendation is still reusable — this method never
        second-guesses that by forcing ``force_refresh``, whether called
        fresh or from a resume path.
        """
        latest_handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        review_findings: tuple[ReviewFinding, ...] = ()
        if is_rework and self._review_store is not None:
            previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
            if previous_review is not None:
                review_findings = previous_review.findings
        git_sha = latest_handoff.git_sha_after if latest_handoff is not None else None
        return ComplexityEstimationRequest(
            project_id=project.project_id, role=DEFAULT_WORK_ITEM_ROLE, workspace=project.workspace,
            objective=work_item.title, acceptance_criteria=work_item.acceptance_criteria,
            mvp_id=mvp_id, work_item_id=work_item.work_item_id,
            latest_handoff=latest_handoff, review_findings=review_findings, git_sha=git_sha,
        )

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

        if due.phase is WaitPhase.REVIEW:
            return await self._resume_review_wait(project=project, mvp_id=mvp_id, work_item=work_item, due=due)

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
        return await self._execute_work_item(
            mvp_id=mvp_id, mvp=mvp, work_item=work_item, dev_worker=dev_worker,
            is_rework=is_rework, resume_context=resume_context,
            dev_model=dev_model, dev_reasoning_effort=dev_reasoning_effort,
        )

    async def _try_resume_recovery_required(self, mvp_id: str) -> WorkItemRunResult | None:
        """Resumes the one RECOVERY_REQUIRED WorkItem of this MVP, if any.

        Unlike a quota wait, there is no deadline to check — reconciliation
        already established that this WorkItem's execution is genuinely
        orphaned/interrupted, so it is immediately re-orchestrable. Which
        phase to resume (development/rework vs. review) is read off the
        role of the execution that was actually reconciled (persisted in
        ExecutionStore) — never re-derived heuristically, never guessed.
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
        project = self._project_state_store.get_project(mvp.project_id)

        executions = self._execution_store.list_for_task(work_item.work_item_id)
        was_review_phase = bool(executions) and executions[-1].role == REVIEWER_ROLE

        if was_review_phase:
            # NOT `latest_for_work_item`: reconciliation may just have
            # created a *newer* recovery handoff recording the interrupted
            # reviewer's own identity (an audit fact about that execution)
            # — using it here would corrupt "who is the author" and could
            # even make the reviewer equal the author. The author is
            # always read from the last handoff that came out of an actual
            # development-role execution.
            handoff = self._find_last_developer_handoff(work_item.work_item_id)
            if handoff is None:
                # RecoveryCoordinator.reconcile_work_item always ensures a
                # recovery handoff exists before this status is ever
                # reached, and a review-phase WorkItem always followed a
                # completed development execution — fail closed rather
                # than guess if that invariant is somehow violated.
                blocked = self._project_state_store.mark_work_item_blocked(
                    work_item.work_item_id,
                    reason="recovery required for review but no author handoff was found",
                )
                return WorkItemRunResult(work_item=blocked, handoff=None)
            work_item = self._project_state_store.mark_work_item_reviewing(work_item.work_item_id)
            return await self._run_review_from_handoff(
                project=project, mvp_id=mvp_id, work_item=work_item, handoff=handoff
            )

        dev_worker, dev_model, dev_reasoning_effort = await self._select_dev_worker(
            mvp=mvp, work_item=work_item, is_rework=False,
        )
        resume_context = self._build_resume_context(work_item.work_item_id)
        return await self._execute_work_item(
            mvp_id=mvp_id, mvp=mvp, work_item=work_item, dev_worker=dev_worker,
            is_rework=False, resume_context=resume_context,
            dev_model=dev_model, dev_reasoning_effort=dev_reasoning_effort,
        )

    async def _resume_review_wait(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, due: WaitRecord
    ) -> WorkItemRunResult:
        handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
        if handoff is None:
            # A review-phase wait always follows a completed development
            # execution, which always creates a handoff — this should not
            # happen, but fail closed rather than guess at recovery facts.
            self._wait_coordinator.resolve(due.wait_id, resolution="gave up: no recovery handoff available")
            blocked = self._project_state_store.mark_work_item_blocked(
                work_item.work_item_id, reason="review wait due but no prior handoff to resume from"
            )
            return WorkItemRunResult(work_item=blocked, handoff=None)

        # `_run_review` itself re-probes WorkerSelector exactly once (a
        # theoretical reset is never treated as proof of availability) and
        # already knows how to requeue-or-block if still unavailable — so
        # this resolves the due wait and delegates the actual re-selection
        # to it, rather than probing a second time here.
        self._wait_coordinator.resolve(
            due.wait_id, resolution="deadline reached — re-attempting reviewer selection"
        )
        work_item = self._project_state_store.mark_work_item_reviewing(work_item.work_item_id)
        return await self._run_review_from_handoff(
            project=project, mvp_id=mvp_id, work_item=work_item, handoff=handoff
        )

    async def _run_review_from_handoff(
        self, *, project: Project, mvp_id: str, work_item: WorkItem, handoff: HandoffRecord
    ) -> WorkItemRunResult:
        """Re-attempts a review purely from durable facts — no live ExecutionResult needed.

        Shared by both resume paths that can no longer rely on an
        in-memory ``dev_result`` (the process that produced it may not
        even be the one running now): quota-wait review resume and
        execution-level recovery review resume. Never asks the previous
        author for anything; the quality gate is never re-run here either
        (``handoff.test_results`` — from the original, still-valid PASSED
        gate — is carried forward as-is, since no new development code was
        produced).
        """
        dev_facts = _RecoveredExecutionResult(
            record=_RecoveredExecutionRecordFacts(
                execution_id=handoff.execution_id, worker_id=handoff.worker_id,
                git_sha_after=handoff.git_sha_after,
            )
        )
        review_result, work_item, next_action = await self._run_review(
            project=project, mvp_id=mvp_id, work_item=work_item,
            dev_result=dev_facts, gate_result=None, gate_summary_override=handoff.test_results,
        )

        decisions: str | None = None
        open_issues: str | None = None
        if review_result is not None:
            if review_result.status is ReviewStatus.APPROVED:
                decisions = (
                    f"review_id={review_result.review_id} approved by "
                    f"{review_result.reviewer_worker_id}"
                )
            else:
                open_issues = _summarize_review(review_result)

        new_handoff = self._handoff_store.create(
            handoff_id=self._id_factory(), project_id=project.project_id, mvp_id=mvp_id,
            work_item_id=work_item.work_item_id, objective=work_item.title,
            execution_id=handoff.execution_id, worker_id=handoff.worker_id,
            decisions=decisions, test_results=handoff.test_results, open_issues=open_issues,
            next_action=next_action, git_sha_after=handoff.git_sha_after, created_at=self._clock(),
        )
        return WorkItemRunResult(work_item=work_item, handoff=new_handoff, review_result=review_result)

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

    async def _execute_work_item(
        self,
        *,
        mvp_id: str,
        mvp: MVP,
        work_item: WorkItem,
        dev_worker: Worker,
        is_rework: bool,
        resume_context: str | None,
        dev_model: str | None = None,
        dev_reasoning_effort: str | None = None,
    ) -> WorkItemRunResult:
        """Runs one development execution through to its handoff.

        Shared by the normal candidate path and both wait-resume paths
        (development/rework) so a resumed WorkItem goes through the exact
        same gate/review/handoff pipeline as a first attempt — the only
        difference is ``resume_context`` and possibly a different worker.

        ``dev_model``/``dev_reasoning_effort``, when supplied (Slice 17
        adaptive selection), are used as-is — the actual snapshot an
        ``AdaptiveExecutionDecision`` already resolved and persisted. When
        omitted (the wait-resume/recovery-resume paths, and any caller
        without adaptive execution configured), ``dev_worker.profile()``'s
        default is used, exactly as before Slice 17.
        """
        self._project_state_store.mark_mvp_running(mvp_id)
        work_item = self._project_state_store.mark_work_item_running(work_item.work_item_id)
        project = self._project_state_store.get_project(mvp.project_id)

        dev_context = resume_context
        if is_rework and self._review_store is not None:
            previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
            if previous_review is not None:
                dev_context = (
                    "This work item was previously reviewed and rejected. "
                    f"Previous review findings: {_summarize_findings(previous_review.findings)}"
                )

        if self._git_governance_service is not None:
            self._git_governance_service.prepare_work_item(
                project_id=project.project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
                repository_path=project.workspace,
            )

        if dev_model is None:
            dev_profile = dev_worker.profile()
            dev_model = dev_profile.model
            dev_reasoning_effort = dev_profile.reasoning_effort
        dev_request = ExecutionRequest(
            execution_id=self._id_factory(),
            task_id=work_item.work_item_id,
            worker=dev_worker,
            role=DEFAULT_WORK_ITEM_ROLE,
            workspace=project.workspace,
            instructions=_build_dev_instructions(work_item, resume_context=dev_context),
            initial_event_topic=INITIAL_EVENT_TOPIC,
            success_topics=frozenset({SUCCESS_TOPIC}),
            failure_topics=frozenset({FAILURE_TOPIC}),
            timeout_seconds=self._timeout_seconds,
            model=dev_model,
            reasoning_effort=dev_reasoning_effort,
        )
        dev_result = await self._execution_engine.execute(dev_request)

        if self._git_governance_service is not None:
            self._git_governance_service.capture_head(
                work_item.work_item_id, repository_path=project.workspace,
            )

        if dev_result.record.status is ExecutionStatus.INTERRUPTED and self._recovery_coordinator is not None:
            # A timeout RalphExecutionEngine observed itself, within this
            # very call — the execution is already a genuine terminal
            # outcome (stays INTERRUPTED, permanent history), never
            # relaunched. Only the WorkItem needs to become explicitly
            # re-orchestrable: RECOVERY_REQUIRED, never a silent FAILED
            # that would forbid a normal continuation.
            self._recovery_coordinator.ensure_recovery_handoff(
                project_id=project.project_id, mvp_id=mvp_id, work_item=work_item,
                execution=dev_result.record,
            )
            work_item = self._project_state_store.mark_work_item_recovery_required(work_item.work_item_id)
            handoff = self._handoff_store.latest_for_work_item(work_item.work_item_id)
            return WorkItemRunResult(work_item=work_item, handoff=handoff)

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
            self._maybe_finalize_git(
                project=project, work_item=work_item, gate_result=gate_result, review_result=None,
            )
        else:
            work_item = self._project_state_store.mark_work_item_reviewing(work_item.work_item_id)
            review_result, work_item, next_action = await self._run_review(
                project=project, mvp_id=mvp_id, work_item=work_item,
                dev_result=dev_result, gate_result=gate_result,
            )
            if review_result is None:
                pass  # entered WAITING (Slice 11) — nothing to summarize yet
            elif review_result.status is ReviewStatus.APPROVED:
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
        gate_summary_override: str | None = None,
    ) -> tuple[ReviewRecord | None, WorkItem, str]:
        """Selects an independent reviewer and runs the review via Ralph.

        Returns (ReviewRecord, updated WorkItem, next_action). ``None`` for
        the ReviewRecord means the WorkItem moved to WAITING (Slice 11: no
        eligible reviewer right now, but a reliable reset is known) — no
        review was attempted, so nothing is recorded (recording an ERROR
        review here would wrongly consume a bounded rework cycle for an
        attempt that never happened). Otherwise this never raises for an
        expected "cannot review right now" outcome (no eligible reviewer at
        all, review execution error): those fail-close to BLOCKED with an
        explicit, persisted reason rather than leaving the WorkItem
        dangling in REVIEWING or propagating an exception that would abort
        the caller's loop over other WorkItems.
        """
        review_id = self._id_factory()
        started_at = self._clock()
        gate_summary = gate_summary_override or (
            _summarize_gate(gate_result) if gate_result is not None else "not configured"
        )

        previous_review = self._review_store.latest_for_work_item(work_item.work_item_id)
        previous_findings = previous_review.findings if previous_review is not None else None

        try:
            reviewer, reviewer_model, reviewer_reasoning_effort = await self._select_reviewer_worker(
                project=project, mvp_id=mvp_id, work_item=work_item,
                author_worker_id=dev_result.record.worker_id,
                git_sha=dev_result.record.git_sha_after, previous_findings=previous_findings,
            )
        except _REVIEWER_SELECTION_ERRORS as exc:
            if self._wait_coordinator is not None:
                wait = self._wait_coordinator.record_wait(
                    project_id=project.project_id, mvp_id=mvp_id, work_item_id=work_item.work_item_id,
                    phase=WaitPhase.REVIEW, diagnostics=getattr(exc, "diagnostics", ()),
                    source_execution_id=dev_result.record.execution_id,
                )
                if wait is not None:
                    work_item = self._project_state_store.mark_work_item_waiting(work_item.work_item_id)
                    return None, work_item, "no eligible independent reviewer — waiting for quota reset"

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

        if self._git_governance_service is not None:
            self._git_governance_service.assert_review_target(
                work_item.work_item_id, repository_path=project.workspace,
            )

        if reviewer_model is None:
            reviewer_profile = reviewer.profile()
            reviewer_model = reviewer_profile.model
            reviewer_reasoning_effort = reviewer_profile.reasoning_effort
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
            model=reviewer_model,
            reasoning_effort=reviewer_reasoning_effort,
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
                reviewer_model=reviewer_model,
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
            reviewer_model=reviewer_model,
            started_at=started_at, finished_at=self._clock(), status=status, findings=findings,
            git_sha_reviewed=dev_result.record.git_sha_after,
        )
        self._review_store.record(review)

        if status is ReviewStatus.APPROVED:
            work_item = self._project_state_store.mark_work_item_completed(work_item.work_item_id)
            self._maybe_finalize_git(
                project=project, work_item=work_item, gate_result=gate_result, review_result=review,
            )
            return review, work_item, "review approved — proceed to the next eligible WorkItem"

        if status is ReviewStatus.INTERRUPTED and self._recovery_coordinator is not None:
            # Never treated as a rejection (would wrongly consume a bounded
            # rework cycle for an attempt that never reached a verdict) and
            # never left dangling in REVIEWING — RECOVERY_REQUIRED, to be
            # resumed with a brand-new review execution, never this one.
            self._recovery_coordinator.ensure_recovery_handoff(
                project_id=project.project_id, mvp_id=mvp_id, work_item=work_item,
                execution=review_exec_result.record,
            )
            work_item = self._project_state_store.mark_work_item_recovery_required(work_item.work_item_id)
            return (
                review, work_item,
                "review execution interrupted — recovery required, will resume with a new review",
            )

        # REJECTED / ERROR / INTERRUPTED-without-recovery-configured: never
        # COMPLETED. Bounded rework (pre-Slice-11b behavior, preserved
        # exactly when execution-level recovery is not configured).
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

    def _maybe_finalize_git(
        self, *, project: Project, work_item: WorkItem,
        gate_result: QualityGateResult | None, review_result: ReviewRecord | None,
    ) -> None:
        """Computes merge eligibility (Slice 20) once a WorkItem reaches
        ``COMPLETED``, and merges immediately if ``policy.auto_merge`` says
        so — otherwise leaves it at ``MERGE_READY`` for a later explicit
        merge. A no-op when git governance is not configured (exact
        pre-Slice-20 behavior preserved).

        Never trusts a live ``gate_result``/``review_result`` alone: either
        can be ``None`` here (the review-not-configured completion path
        passes no review; a resumed-review completion has no live
        ``gate_result``) — in that case the latest persisted evidence is
        read back from ``validation_store``/``review_store`` instead, so
        eligibility is computed identically whether the evidence came from
        this exact call or survived a cold restart.
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

        review_approved: bool | None = None
        review_git_sha: str | None = None
        if review_result is not None:
            review_approved = review_result.status is ReviewStatus.APPROVED
            review_git_sha = review_result.git_sha_reviewed
        elif self._review_store is not None:
            latest_review = self._review_store.latest_for_work_item(work_item.work_item_id)
            if latest_review is not None:
                review_approved = latest_review.status is ReviewStatus.APPROVED
                review_git_sha = latest_review.git_sha_reviewed

        eligibility = self._git_governance_service.compute_merge_eligibility(
            work_item.work_item_id, repository_path=project.workspace,
            work_item_status=work_item.status.value,
            gate_passed=gate_passed, gate_git_sha=gate_git_sha,
            review_approved=review_approved, review_git_sha=review_git_sha,
        )
        if eligibility.mergeable and self._git_governance_service.policy.auto_merge:
            self._git_governance_service.merge(
                work_item.work_item_id, repository_path=project.workspace, eligibility=eligibility,
            )
