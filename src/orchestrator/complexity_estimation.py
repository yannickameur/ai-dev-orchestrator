"""Complexity pre-flight + persistent execution recommendations (Slice 16).

Before a future development/review/planning/synthesis execution, this
module answers exactly one question: "what minimum quality tier does this
work genuinely need?" — by asking an independent, specialized Worker (the
*estimator*, never the future developer/reviewer itself) exactly like
:mod:`~orchestrator.planning` already asks planner/synthesizer Workers,
and persisting the answer as a durable, auditable
:class:`ExecutionRecommendation`.

Nothing here chooses a final Worker/ExecutionProfile for the real work —
that remains Slice 17. This module only produces and caches a
recommendation; Slice 17 will consume it.

OPTION C, THE SAME WAY PLANNING/SYNTHESIS ALREADY WORK:

    context of the work
      -> WorkerSelector.select(capability=complexity_estimation)
      -> the selected worker's *configured* estimator_profile_id
      -> RalphExecutionEngine (never a direct Claude/Codex/Ralph call)
      -> ``execution.profile_recommended`` (a structured event, exactly
         like ``planning.proposed``/``synthesis.proposed``)
      -> ExecutionRecommendation, persisted

RECOMMENDS A LEVEL, NEVER A WORKER OR A MODEL: the estimator's payload
contract has no ``model``/``provider``/``worker_id`` field, by
construction (see ``_parse_recommendation_payload`` — any such field, if
present, is simply ignored, never trusted). Only ``minimum_quality_tier``
(a :class:`~orchestrator.worker_selector.QualityTier`), an optional
``recommended_reasoning`` hint (a free-form string — never a provider
enum, exactly like ``Worker.reasoning_effort`` already is), and
``reasons`` (free text, for audit) are ever accepted. Translating a tier
into an actual Worker/ExecutionProfile is Slice 17's job.

NO SEPARATE "complexity" FIELD: ``QualityTier``'s own names (SIMPLE/
STANDARD/COMPLEX/CRITICAL) already *are* a complexity classification —
adding a second free-text "complexity" label next to
``minimum_quality_tier`` would just restate the same fact under a
different name. This module deliberately has only one.

ESTIMATOR IS A NORMAL, CONFIGURED WORKER: selected via the existing
``WorkerSelector`` by capability alone (``complexity_estimation``, never
a hardcoded worker/provider name) — this module never duplicates
capability filtering, governance, or quota logic. Its execution profile
is resolved from ``Worker.estimator_profile_id`` (Slice 15) — never
``Worker.profile()``'s default, which would silently reuse whatever
profile a *developer* selection happens to default to. A Worker
declaring the ``complexity_estimation`` capability without a configured
``estimator_profile_id`` fails closed here (``EstimatorProfileNotConfiguredError``)
rather than being validated at ``WorkerRegistry`` load time: which
Worker/capability strings mean what is this module's business, not
``WorkerRegistry``'s (which stays fully generic — see
``docs/ADAPTIVE_EXECUTION.md`` and ``orchestrator.worker_registry``'s own
docstring; it never special-cases a capability name, exactly like
``WorkerSelector`` never special-cases a provider name).

FINGERPRINT + CACHE: a deterministic sha256 over the canonical facts that
matter (role, project/mvp/work_item identity, objective, acceptance
criteria, the latest relevant handoff's content, relevant review
findings, git SHA) means an identical pre-flight is never paid for twice.
``estimate(..., force_refresh=False)`` (default) returns the latest
persisted recommendation for that exact fingerprint without ever
launching Ralph again; ``force_refresh=True`` always launches a fresh
estimation and persists it as a new, separate row — the previous
recommendation is never overwritten or deleted (same "every attempt kept"
posture as ``PlanningSession``/``RoadmapApplication``).

FAIL-CLOSED, EVERYWHERE: no reliable ``execution.profile_recommended``
event, an invalid/malformed payload, or an unrecognized
``minimum_quality_tier`` all mean no ``ExecutionRecommendation`` is ever
produced — never a silent fallback to ``STANDARD``. ``exit_code`` is
never consulted (unchanged ``RalphExecutionEngine`` invariant): a
non-zero exit alongside a valid business event can still be a valid
recommendation, exactly as for planner/synthesizer/review executions.

NO MVPManager INTEGRATION YET: this module is a usable, standalone
service. Wiring it into the normal development/review cycle is Slice 17
— MVPManager, WorkerSelector's *own* selection algorithm, and the review/
planning orchestration modules are all untouched by this slice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from orchestrator.execution_store import ExecutionStatus
from orchestrator.handoff import HandoffRecord
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.review import ReviewFinding
from orchestrator.worker_selector import QualityTier, Worker, WorkerSelectionRequest, WorkerSelector

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_ESTIMATOR_CAPABILITY = "complexity_estimation"
DEFAULT_TIMEOUT_SECONDS = 300.0

ESTIMATOR_ROLE = "estimator"

ESTIMATION_INITIAL_TOPIC = "estimation.start"
PROFILE_RECOMMENDED_TOPIC = "execution.profile_recommended"
PROFILE_RECOMMENDATION_FAILED_TOPIC = "execution.profile_failed"

_FINGERPRINT_CONTRACT_VERSION = 1

_NO_MODIFICATION_NOTICE = (
    "You must ONLY analyze — never modify project files, never write or delete code, "
    "never commit, never push, never run destructive commands. You are not implementing "
    "anything; you are estimating how demanding the work described below would be."
)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _as_tuple_of_str(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return result


@dataclass(frozen=True, slots=True)
class ComplexityEstimationRequest:
    """Only the facts a pre-flight needs — nothing speculative.

    ``role`` reuses this codebase's existing plain-string role convention
    (``mvp_manager.DEFAULT_WORK_ITEM_ROLE``/``REVIEWER_ROLE``,
    ``planning.PLANNER_ROLE``/``SYNTHESIZER_ROLE``) — never a new,
    parallel taxonomy. The same WorkItem estimated for a different role
    (e.g. development vs. code_review) is a different request and, by
    design, produces a different fingerprint/recommendation.
    """

    project_id: str
    role: str
    workspace: Path
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    mvp_id: str | None = None
    work_item_id: str | None = None
    latest_handoff: HandoffRecord | None = None
    review_findings: tuple[ReviewFinding, ...] = ()
    git_sha: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.project_id, field_name="ComplexityEstimationRequest.project_id")
        _require_non_empty_str(self.role, field_name="ComplexityEstimationRequest.role")
        _require_non_empty_str(self.objective, field_name="ComplexityEstimationRequest.objective")
        object.__setattr__(self, "workspace", Path(self.workspace))
        object.__setattr__(
            self, "acceptance_criteria",
            _as_tuple_of_str(self.acceptance_criteria, field_name="ComplexityEstimationRequest.acceptance_criteria"),
        )
        object.__setattr__(self, "review_findings", tuple(self.review_findings))
        for name in ("mvp_id", "work_item_id", "git_sha"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ComplexityEstimationRequest.{name}")
        if self.latest_handoff is not None and not isinstance(self.latest_handoff, HandoffRecord):
            raise TypeError(
                f"ComplexityEstimationRequest.latest_handoff must be a HandoffRecord or None, "
                f"got {type(self.latest_handoff)!r}"
            )
        for finding in self.review_findings:
            if not isinstance(finding, ReviewFinding):
                raise TypeError(f"ComplexityEstimationRequest.review_findings entries must be ReviewFinding, got {type(finding)!r}")


@dataclass(frozen=True, slots=True)
class ExecutionRecommendation:
    """A durable, auditable pre-flight outcome — never overwritten, never deleted."""

    recommendation_id: str
    project_id: str
    role: str
    estimator_worker_id: str
    estimator_execution_id: str
    estimator_profile_id: str
    task_fingerprint: str
    minimum_quality_tier: QualityTier
    reasons: tuple[str, ...]
    created_at: datetime
    mvp_id: str | None = None
    work_item_id: str | None = None
    recommended_reasoning: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "recommendation_id", "project_id", "role", "estimator_worker_id",
            "estimator_execution_id", "estimator_profile_id", "task_fingerprint",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"ExecutionRecommendation.{name}")
        if not isinstance(self.minimum_quality_tier, QualityTier):
            raise TypeError(
                f"ExecutionRecommendation.minimum_quality_tier must be a QualityTier, "
                f"got {type(self.minimum_quality_tier)!r}"
            )
        _require_aware(self.created_at, field_name="ExecutionRecommendation.created_at")
        object.__setattr__(self, "reasons", _as_tuple_of_str(self.reasons, field_name="ExecutionRecommendation.reasons"))
        for name in ("mvp_id", "work_item_id", "recommended_reasoning"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ExecutionRecommendation.{name}")


# --- deterministic fingerprint (pure, no LLM) --------------------------------


def _canonical_handoff(handoff: HandoffRecord | None) -> dict | None:
    if handoff is None:
        return None
    return {
        "handoff_id": handoff.handoff_id,
        "objective": handoff.objective,
        "completed_work": handoff.completed_work,
        "decisions": handoff.decisions,
        "test_results": handoff.test_results,
        "open_issues": handoff.open_issues,
        "risks": handoff.risks,
        "next_action": handoff.next_action,
        "git_sha_after": handoff.git_sha_after,
    }


def _canonical_finding(finding: ReviewFinding) -> dict:
    return {
        "finding_id": finding.finding_id, "summary": finding.summary, "severity": finding.severity,
        "detail": finding.detail, "file_path": finding.file_path, "line": finding.line,
        "category": finding.category,
    }


def compute_task_fingerprint(request: ComplexityEstimationRequest) -> str:
    """A deterministic sha256 over exactly the facts that should invalidate
    a cached recommendation — never Python's non-deterministic ``hash()``."""
    payload = {
        "contract_version": _FINGERPRINT_CONTRACT_VERSION,
        "role": request.role,
        "project_id": request.project_id,
        "mvp_id": request.mvp_id,
        "work_item_id": request.work_item_id,
        "objective": request.objective,
        "acceptance_criteria": list(request.acceptance_criteria),
        "git_sha": request.git_sha,
        "latest_handoff": _canonical_handoff(request.latest_handoff),
        "review_findings": [_canonical_finding(f) for f in request.review_findings],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- payload parsing (strict, fail-closed) ----------------------------------


def _parse_quality_tier(value: object) -> QualityTier:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"minimum_quality_tier must be a non-empty string, got {value!r}")
    try:
        return QualityTier[value.strip().upper()]
    except KeyError:
        raise ValueError(
            f"invalid minimum_quality_tier {value!r} — must be one of {[t.name for t in QualityTier]!r}"
        ) from None


def _parse_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError("reasons must be a list of non-empty strings")
    return tuple(value)


def _parse_recommendation_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{PROFILE_RECOMMENDED_TOPIC} payload must be a JSON object")
    if "minimum_quality_tier" not in data:
        raise ValueError("missing required field 'minimum_quality_tier'")
    minimum_quality_tier = _parse_quality_tier(data["minimum_quality_tier"])
    recommended_reasoning = data.get("recommended_reasoning")
    if recommended_reasoning is not None and (
        not isinstance(recommended_reasoning, str) or not recommended_reasoning.strip()
    ):
        raise ValueError("recommended_reasoning must be a non-empty string or null")
    reasons = _parse_reasons(data.get("reasons", []))
    return {
        "minimum_quality_tier": minimum_quality_tier,
        "recommended_reasoning": recommended_reasoning,
        "reasons": reasons,
    }


# --- prompt (deterministic, no free-form template drift) --------------------


def _build_estimator_instructions(request: ComplexityEstimationRequest) -> str:
    criteria_block = "\n".join(f"- {c}" for c in request.acceptance_criteria) or "- (none recorded)"
    handoff = request.latest_handoff
    handoff_block = (
        "\n".join(
            f"- {label}: {value}"
            for label, value in (
                ("completed_work", handoff.completed_work), ("decisions", handoff.decisions),
                ("test_results", handoff.test_results), ("open_issues", handoff.open_issues),
                ("risks", handoff.risks), ("next_action", handoff.next_action),
            )
            if value
        )
        if handoff is not None else ""
    ) or "(no prior handoff recorded)"
    findings_block = "\n".join(
        f"- [{f.severity}] {f.summary}" + (f" ({f.file_path})" if f.file_path else "")
        for f in request.review_findings
    ) or "(none recorded)"

    return (
        f"You are an independent complexity estimator for the {request.role!r} role. "
        "You do not develop, review, or fix anything yourself — you only estimate how "
        "demanding the work described below would be for whoever does it next.\n\n"
        f"{_NO_MODIFICATION_NOTICE}\n\n"
        f"Objective:\n{request.objective}\n\n"
        f"Acceptance criteria:\n{criteria_block}\n\n"
        f"Most recent relevant handoff:\n{handoff_block}\n\n"
        f"Relevant review findings:\n{findings_block}\n\n"
        f"Git SHA of the code being estimated: {request.git_sha or '(none)'}\n\n"
        "Consider, as relevant: size of the change, number of modules touched, "
        "architecture impact, persistence/concurrency concerns, security, migrations, "
        "regression risk, ambiguity of the requirements, algorithmic complexity, and "
        "(for a review role) the criticality of what is being reviewed and any existing "
        "findings above. Recommend the MINIMUM quality tier genuinely sufficient — never "
        "default to the highest tier out of caution, and never name a specific model or "
        "provider.\n\n"
        f'Emit exactly:\n\nralph emit "{PROFILE_RECOMMENDED_TOPIC}" \'<JSON object>\'\n\n'
        "The JSON object must have exactly these fields: minimum_quality_tier (one of "
        f"{[t.name for t in QualityTier]!r}), recommended_reasoning (string or null — a "
        "generic hint such as \"low\"/\"medium\"/\"high\", never a provider-specific "
        "value), reasons (array of short strings explaining the recommendation).\n\n"
        f'If you cannot produce a reliable recommendation, emit exactly:\n\nralph emit '
        f'"{PROFILE_RECOMMENDATION_FAILED_TOPIC}" "<short reason>"\n\nThen output:\n\nLOOP_COMPLETE\n'
    )


# --- persistence --------------------------------------------------------------


class ExecutionRecommendationStoreError(Exception):
    """Base for ExecutionRecommendationStore domain errors."""


class UnknownRecommendationError(ExecutionRecommendationStoreError):
    def __init__(self, recommendation_id: str) -> None:
        super().__init__(f"unknown execution recommendation: {recommendation_id!r}")
        self.recommendation_id = recommendation_id


class CorruptRecommendationError(ExecutionRecommendationStoreError):
    def __init__(self, recommendation_id: str, detail: str) -> None:
        super().__init__(f"corrupt execution recommendation {recommendation_id!r}: {detail}")
        self.recommendation_id = recommendation_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS execution_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT,
    work_item_id TEXT,
    role TEXT NOT NULL,
    estimator_worker_id TEXT NOT NULL,
    estimator_execution_id TEXT NOT NULL,
    estimator_profile_id TEXT NOT NULL,
    task_fingerprint TEXT NOT NULL,
    minimum_quality_tier TEXT NOT NULL,
    recommended_reasoning TEXT,
    reasons TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _decode_row(row: sqlite3.Row) -> ExecutionRecommendation:
    recommendation_id = row["recommendation_id"]
    try:
        return ExecutionRecommendation(
            recommendation_id=recommendation_id, project_id=row["project_id"], mvp_id=row["mvp_id"],
            work_item_id=row["work_item_id"], role=row["role"], estimator_worker_id=row["estimator_worker_id"],
            estimator_execution_id=row["estimator_execution_id"], estimator_profile_id=row["estimator_profile_id"],
            task_fingerprint=row["task_fingerprint"], minimum_quality_tier=QualityTier[row["minimum_quality_tier"]],
            recommended_reasoning=row["recommended_reasoning"], reasons=tuple(json.loads(row["reasons"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise CorruptRecommendationError(recommendation_id, str(exc)) from exc


class ExecutionRecommendationStore:
    """Synchronous, sqlite3-backed store for durable ExecutionRecommendations."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def record(self, recommendation: ExecutionRecommendation) -> ExecutionRecommendation:
        """Inserts a new row — a recommendation is never updated or deleted;
        a re-estimation (``force_refresh=True``) always produces a
        separate, distinct recommendation_id."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO execution_recommendations (recommendation_id, project_id, mvp_id, "
                "work_item_id, role, estimator_worker_id, estimator_execution_id, "
                "estimator_profile_id, task_fingerprint, minimum_quality_tier, "
                "recommended_reasoning, reasons, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recommendation.recommendation_id, recommendation.project_id, recommendation.mvp_id,
                    recommendation.work_item_id, recommendation.role, recommendation.estimator_worker_id,
                    recommendation.estimator_execution_id, recommendation.estimator_profile_id,
                    recommendation.task_fingerprint, recommendation.minimum_quality_tier.name,
                    recommendation.recommended_reasoning, json.dumps(list(recommendation.reasons)),
                    recommendation.created_at.isoformat(),
                ),
            )
        return recommendation

    def get(self, recommendation_id: str) -> ExecutionRecommendation:
        row = self._conn.execute(
            "SELECT * FROM execution_recommendations WHERE recommendation_id = ?", (recommendation_id,)
        ).fetchone()
        if row is None:
            raise UnknownRecommendationError(recommendation_id)
        return _decode_row(row)

    def latest_for_fingerprint(self, task_fingerprint: str) -> ExecutionRecommendation | None:
        row = self._conn.execute(
            "SELECT * FROM execution_recommendations WHERE task_fingerprint = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_fingerprint,),
        ).fetchone()
        return _decode_row(row) if row is not None else None

    def list_for_work_item(self, work_item_id: str) -> list[ExecutionRecommendation]:
        """Every recommendation ever produced for a WorkItem, oldest first — none ever overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM execution_recommendations WHERE work_item_id = ? ORDER BY created_at ASC",
            (work_item_id,),
        ).fetchall()
        return [_decode_row(row) for row in rows]


# --- service ------------------------------------------------------------------


class ExecutionRecommendationError(Exception):
    """Base for ExecutionRecommendationService domain errors."""


class EstimatorProfileNotConfiguredError(ExecutionRecommendationError):
    """The selected estimator Worker has no ``estimator_profile_id`` configured.

    Deliberately a service-level, fail-closed check rather than a
    ``WorkerRegistry``-level structural rule: which capability strings
    require which configuration is this module's business, not
    ``WorkerRegistry``'s (kept fully generic — see module docstring).
    """

    def __init__(self, worker_id: str) -> None:
        super().__init__(
            f"worker {worker_id!r} declares the estimator capability but has no "
            "estimator_profile_id configured — refusing to guess a profile"
        )
        self.worker_id = worker_id


class NoReliableRecommendationError(ExecutionRecommendationError):
    """No reliable ``execution.profile_recommended`` event was produced."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"no reliable {PROFILE_RECOMMENDED_TOPIC} event: {reason}")


class InvalidRecommendationPayloadError(ExecutionRecommendationError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid {PROFILE_RECOMMENDED_TOPIC} payload: {detail}")


class ExecutionRecommendationService:
    """Produces (or reuses a cached) :class:`ExecutionRecommendation`.

    Composes existing services only — ``WorkerSelector`` for estimator
    selection (capability/governance/quota, never duplicated here),
    ``RalphExecutionEngine`` for the actual execution (never a direct
    Claude/Codex/Ralph call). Never selects a final development/review
    Worker or ExecutionProfile — that is Slice 17.
    """

    def __init__(
        self,
        recommendation_store: ExecutionRecommendationStore,
        worker_selector: WorkerSelector,
        execution_engine: RalphExecutionEngine,
        *,
        estimator_capability: str = DEFAULT_ESTIMATOR_CAPABILITY,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._recommendation_store = recommendation_store
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._estimator_capability = estimator_capability
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._timeout_seconds = timeout_seconds

    async def estimate(
        self, request: ComplexityEstimationRequest, *, force_refresh: bool = False
    ) -> ExecutionRecommendation:
        """Returns a recommendation for ``request``.

        Cache hit (same fingerprint, ``force_refresh=False``): returns the
        latest persisted recommendation, launching nothing. Otherwise
        selects an estimator, runs it via Ralph, validates its payload
        strictly, and persists a brand-new recommendation — the previous
        one (if any) is never overwritten.
        """
        fingerprint = compute_task_fingerprint(request)
        if not force_refresh:
            cached = self._recommendation_store.latest_for_fingerprint(fingerprint)
            if cached is not None:
                return cached

        worker = await self._select_estimator()
        if worker.estimator_profile_id is None:
            raise EstimatorProfileNotConfiguredError(worker.worker_id)
        profile = worker.profile(worker.estimator_profile_id)

        execution_id = self._id_factory()
        exec_request = ExecutionRequest(
            execution_id=execution_id, task_id=f"estimation:{fingerprint}",
            worker=worker, role=ESTIMATOR_ROLE, workspace=request.workspace,
            instructions=_build_estimator_instructions(request),
            initial_event_topic=ESTIMATION_INITIAL_TOPIC,
            success_topics=frozenset({PROFILE_RECOMMENDED_TOPIC}),
            failure_topics=frozenset({PROFILE_RECOMMENDATION_FAILED_TOPIC}),
            timeout_seconds=self._timeout_seconds,
            model=profile.model, reasoning_effort=profile.reasoning_effort,
        )

        try:
            result = await self._execution_engine.execute(exec_request)
        except RalphExecutionEngineError as exc:
            raise NoReliableRecommendationError(f"estimator execution failed to run: {exc}") from exc

        if result.record.status is not ExecutionStatus.SUCCEEDED:
            raise NoReliableRecommendationError(f"status={result.record.status.value}")

        event = next((e for e in result.events if e.topic == PROFILE_RECOMMENDED_TOPIC), None)
        if event is None or not event.payload:
            raise NoReliableRecommendationError(f"missing {PROFILE_RECOMMENDED_TOPIC} payload")

        try:
            data = json.loads(event.payload)
            parsed = _parse_recommendation_payload(data)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise InvalidRecommendationPayloadError(str(exc)) from exc

        recommendation = ExecutionRecommendation(
            recommendation_id=self._id_factory(), project_id=request.project_id, mvp_id=request.mvp_id,
            work_item_id=request.work_item_id, role=request.role, estimator_worker_id=worker.worker_id,
            estimator_execution_id=execution_id, estimator_profile_id=profile.profile_id,
            task_fingerprint=fingerprint, created_at=self._clock(), **parsed,
        )
        return self._recommendation_store.record(recommendation)

    async def _select_estimator(self) -> Worker:
        # Fail-closed by construction: NoEligibleWorkerError from
        # WorkerSelector propagates unchanged — never a fallback to an
        # arbitrary other Worker. Capability/governance/quota filtering is
        # entirely WorkerSelector's, never duplicated here.
        return await self._worker_selector.select(
            WorkerSelectionRequest(required_capabilities=frozenset({self._estimator_capability}))
        )
