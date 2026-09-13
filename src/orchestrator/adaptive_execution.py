"""Adaptive Worker/Profile selection for development and rework (Slice 17).

Turns an :class:`~orchestrator.complexity_estimation.ExecutionRecommendation`
(Slice 16 — "what minimum quality tier does this work need?") into a real,
concrete choice: which ``Worker`` runs it, and with which
``ExecutionProfile`` (model/reasoning_effort). This module never launches
anything itself and never reimplements any part of ``WorkerSelector`` or
``RalphExecutionEngine`` — it is the one small, new layer between "we know
the tier we need" and "here is the model/reasoning_effort to actually run
with".

RESPONSIBILITIES, NOT MIXED:

- ``ExecutionRecommendationService`` (Slice 16) recommends a level. It is
  used here exactly as-is, unchanged.
- ``WorkerSelector`` (Slice 4, extended minimally in Slice 17 with
  ``WorkerSelectionRequest.minimum_quality_tier``) continues to own
  capability/governance/quota/priority — this module never duplicates
  that pipeline. It is only ever asked "which worker, among those with at
  least one profile reaching this tier" — never "which profile".
- This module picks *which of the selected worker's own profiles* to use
  — the one part of the picture ``WorkerSelector`` deliberately does not
  own (see ``WorkerSelectionRequest``'s own docstring).
- ``RalphExecutionEngine`` executes the final choice, unchanged.

NEVER A SILENT DOWNGRADE: ``resolve_profile`` only ever considers profiles
whose ``quality_tier >= minimum_quality_tier`` — never a fallback to a
weaker, merely-available profile. If a selected worker somehow has none
(should not happen: ``WorkerSelector`` already filtered on this), it fails
closed (``NoCapableProfileError``) rather than guessing.

PROFILE CHOICE, DETERMINISTIC: among a worker's tier-capable profiles,
prefer (1) an exact ``reasoning_effort`` match for the recommendation's
``recommended_reasoning`` hint, if one exists among them — never at the
cost of tier; (2) lowest ``cost_rank``; (3) smallest tier *above* the
minimum (don't over-provision); (4) ``profile_id`` lexical order, as the
final, stable tie-break. ``recommended_reasoning`` is a hint, never a
provider-specific enum — a backend/profile with no matching
``reasoning_effort`` (or none configured at all, e.g. many Claude
profiles) is simply not preferred, never rejected.

NO DOWNGRADE VIA QUOTA EITHER: because the tier filter is applied inside
``WorkerSelector`` itself (before quota is ever probed), a worker with no
tier-capable profile is never even diagnosed for quota — it simply isn't
a candidate. That distinguishes, for free, "some tier-capable provider is
just quota-exhausted right now" (``WorkerSelector`` raises with
diagnostics a caller can turn into a ``WAITING``, exactly as before Slice
17) from "no worker configured can reach this tier at all" (empty
diagnostics — a caller must never manufacture a wait for that; see
``orchestrator.wait.WaitCoordinator.record_wait``, which already returns
``None`` when no diagnosable reset exists, unchanged by this slice).

NO ESTIMATOR RECURSION: the estimator Worker (chosen via
``complexity_estimation``, Slice 16) never goes through this module —
its own configured ``estimator_profile_id`` is used directly, never a
tier-based resolution (that would mean pre-flighting the pre-flight).

AUDITABLE, INSERT-ONLY: every call to ``select()`` persists one
``AdaptiveExecutionDecision`` — never updated, never deleted — capturing
the exact snapshot actually used (worker/provider/backend/profile/model/
reasoning_effort) independently of whatever ``config/workers.yaml`` says
later. A WorkItem re-estimated after rework gets a *new* decision; the
previous one is kept for audit exactly like
``ExecutionRecommendation``/``RoadmapApplication`` already are.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.complexity_estimation import (
    ComplexityEstimationRequest,
    ExecutionRecommendation,
    ExecutionRecommendationService,
)
from orchestrator.worker_selector import (
    ExecutionProfile,
    QualityTier,
    Worker,
    WorkerSelectionRequest,
    WorkerSelector,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class AdaptiveExecutionError(Exception):
    """Base for adaptive execution domain errors."""


class NoCapableProfileError(AdaptiveExecutionError):
    """Defensive fail-closed guard: the worker WorkerSelector returned has
    no profile reaching ``minimum_quality_tier`` after all.

    Should not happen — ``WorkerSelector`` already filters on this — but
    this module never trusts that silently; it refuses rather than
    guessing a profile.
    """

    def __init__(self, worker_id: str, minimum_quality_tier: QualityTier) -> None:
        super().__init__(
            f"worker {worker_id!r} has no execution profile reaching "
            f"{minimum_quality_tier.name} — refusing to guess one"
        )
        self.worker_id = worker_id
        self.minimum_quality_tier = minimum_quality_tier


def resolve_profile(
    worker: Worker, *, minimum_quality_tier: QualityTier, recommended_reasoning: str | None
) -> tuple[ExecutionProfile, str]:
    """Deterministically picks one of ``worker``'s profiles.

    Returns ``(profile, rationale)``. Never returns a profile whose
    ``quality_tier < minimum_quality_tier``. See module docstring for the
    exact preference order.
    """
    capable = [p for p in worker.profiles if p.quality_tier >= minimum_quality_tier]
    if not capable:
        raise NoCapableProfileError(worker.worker_id, minimum_quality_tier)

    reasoning_honored = False
    pool = capable
    if recommended_reasoning is not None:
        exact = [p for p in capable if p.reasoning_effort == recommended_reasoning]
        if exact:
            pool = exact
            reasoning_honored = True

    chosen = sorted(pool, key=lambda p: (p.cost_rank, p.quality_tier.value, p.profile_id))[0]

    rationale_parts = [
        f"minimum_quality_tier={minimum_quality_tier.name}",
        f"chosen_tier={chosen.quality_tier.name}",
        f"cost_rank={chosen.cost_rank}",
    ]
    if recommended_reasoning is not None:
        rationale_parts.append(
            f"recommended_reasoning={recommended_reasoning!r} "
            + ("honored exactly" if reasoning_honored else "could not be matched exactly — tier never degraded")
        )
    return chosen, "; ".join(rationale_parts)


@dataclass(frozen=True, slots=True)
class AdaptiveExecutionDecision:
    """The durable, auditable outcome of one adaptive selection — never
    updated, never deleted."""

    decision_id: str
    recommendation_id: str
    project_id: str
    role: str
    worker_id: str
    provider: str
    backend: str
    profile_id: str
    quality_tier: QualityTier
    model: str
    created_at: datetime
    reasoning_effort: str | None = None
    mvp_id: str | None = None
    work_item_id: str | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        for name in (
            "decision_id", "recommendation_id", "project_id", "role", "worker_id",
            "provider", "backend", "profile_id", "model",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"AdaptiveExecutionDecision.{name}")
        if not isinstance(self.quality_tier, QualityTier):
            raise TypeError(
                f"AdaptiveExecutionDecision.quality_tier must be a QualityTier, got {type(self.quality_tier)!r}"
            )
        _require_aware(self.created_at, field_name="AdaptiveExecutionDecision.created_at")
        for name in ("reasoning_effort", "mvp_id", "work_item_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"AdaptiveExecutionDecision.{name}")


@dataclass(frozen=True, slots=True)
class AdaptiveSelection:
    """What ``AdaptiveExecutionSelector.select()`` returns: the actual
    ``Worker`` object (needed by ``ExecutionRequest``) alongside the
    persisted decision describing it."""

    worker: Worker
    decision: AdaptiveExecutionDecision


class AdaptiveExecutionDecisionStoreError(Exception):
    """Base for AdaptiveExecutionDecisionStore domain errors."""


class UnknownDecisionError(AdaptiveExecutionDecisionStoreError):
    def __init__(self, decision_id: str) -> None:
        super().__init__(f"unknown adaptive execution decision: {decision_id!r}")
        self.decision_id = decision_id


class CorruptDecisionError(AdaptiveExecutionDecisionStoreError):
    def __init__(self, decision_id: str, detail: str) -> None:
        super().__init__(f"corrupt adaptive execution decision {decision_id!r}: {detail}")
        self.decision_id = decision_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS adaptive_execution_decisions (
    decision_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    mvp_id TEXT,
    work_item_id TEXT,
    role TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    backend TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    quality_tier TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _decode_row(row: sqlite3.Row) -> AdaptiveExecutionDecision:
    decision_id = row["decision_id"]
    try:
        return AdaptiveExecutionDecision(
            decision_id=decision_id, recommendation_id=row["recommendation_id"], project_id=row["project_id"],
            mvp_id=row["mvp_id"], work_item_id=row["work_item_id"], role=row["role"],
            worker_id=row["worker_id"], provider=row["provider"], backend=row["backend"],
            profile_id=row["profile_id"], quality_tier=QualityTier[row["quality_tier"]], model=row["model"],
            reasoning_effort=row["reasoning_effort"], rationale=row["rationale"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise CorruptDecisionError(decision_id, str(exc)) from exc


class AdaptiveExecutionDecisionStore:
    """Synchronous, sqlite3-backed store for durable AdaptiveExecutionDecisions."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def record(self, decision: AdaptiveExecutionDecision) -> AdaptiveExecutionDecision:
        """Inserts a new row — a decision is never updated or deleted; a
        later re-selection (e.g. after rework) always produces a
        separate, distinct decision_id."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO adaptive_execution_decisions (decision_id, recommendation_id, project_id, "
                "mvp_id, work_item_id, role, worker_id, provider, backend, profile_id, quality_tier, "
                "model, reasoning_effort, rationale, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.decision_id, decision.recommendation_id, decision.project_id, decision.mvp_id,
                    decision.work_item_id, decision.role, decision.worker_id, decision.provider,
                    decision.backend, decision.profile_id, decision.quality_tier.name, decision.model,
                    decision.reasoning_effort, decision.rationale, decision.created_at.isoformat(),
                ),
            )
        return decision

    def get(self, decision_id: str) -> AdaptiveExecutionDecision:
        row = self._conn.execute(
            "SELECT * FROM adaptive_execution_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise UnknownDecisionError(decision_id)
        return _decode_row(row)

    def list_for_work_item(self, work_item_id: str) -> list[AdaptiveExecutionDecision]:
        """Every decision ever made for a WorkItem, oldest first — none ever overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM adaptive_execution_decisions WHERE work_item_id = ? ORDER BY created_at ASC",
            (work_item_id,),
        ).fetchall()
        return [_decode_row(row) for row in rows]


class AdaptiveExecutionSelector:
    """Composes ``ExecutionRecommendationService`` + ``WorkerSelector`` into
    one concrete, persisted ``AdaptiveExecutionDecision``.

    Never selects the estimator itself (that stays entirely
    ``ExecutionRecommendationService``'s job, with its own configured
    ``estimator_profile_id`` — never routed through ``resolve_profile``).
    """

    def __init__(
        self,
        decision_store: AdaptiveExecutionDecisionStore,
        recommendation_service: ExecutionRecommendationService,
        worker_selector: WorkerSelector,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._decision_store = decision_store
        self._recommendation_service = recommendation_service
        self._worker_selector = worker_selector
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory

    async def select(
        self,
        *,
        estimation_request: ComplexityEstimationRequest,
        required_capabilities: frozenset[str],
        excluded_worker_ids: frozenset[str] = frozenset(),
        author_worker_id: str | None = None,
        force_refresh: bool = False,
    ) -> AdaptiveSelection:
        """Pre-flight, then a real Worker + ExecutionProfile choice, persisted.

        ``author_worker_id`` (Slice 19), when supplied, is forwarded as-is
        to ``WorkerSelectionRequest.author_worker_id`` — this is what makes
        a call a *review* selection as far as ``WorkerSelector`` is
        concerned: the resulting worker can never be the author, and
        ``WorkerSelectionPolicy``'s cross-provider preferred/required
        review-independence policy applies exactly as it already does for
        non-adaptive review selection. This module never reimplements or
        weakens that policy — it only ever forwards the one field
        ``WorkerSelector`` already uses to activate it.

        Fail-closed end to end: no exception here is ever caught to fall
        back to a default profile — an unhandled ``NoReliableRecommendationError``/
        ``EstimatorProfileNotConfiguredError`` (Slice 16),
        ``NoEligibleWorkerError``/``ReviewIndependenceError``/
        ``UnknownWorkerError`` (WorkerSelector), or ``NoCapableProfileError``
        (this module) must all simply propagate to the caller: "no
        execution happens this attempt".
        """
        recommendation = await self._recommendation_service.estimate(
            estimation_request, force_refresh=force_refresh
        )

        worker = await self._worker_selector.select(
            WorkerSelectionRequest(
                required_capabilities=required_capabilities,
                excluded_worker_ids=excluded_worker_ids,
                author_worker_id=author_worker_id,
                minimum_quality_tier=recommendation.minimum_quality_tier,
            )
        )
        profile, rationale = resolve_profile(
            worker, minimum_quality_tier=recommendation.minimum_quality_tier,
            recommended_reasoning=recommendation.recommended_reasoning,
        )

        decision = AdaptiveExecutionDecision(
            decision_id=self._id_factory(), recommendation_id=recommendation.recommendation_id,
            project_id=estimation_request.project_id, mvp_id=estimation_request.mvp_id,
            work_item_id=estimation_request.work_item_id, role=estimation_request.role,
            worker_id=worker.worker_id, provider=worker.provider, backend=worker.backend,
            profile_id=profile.profile_id, quality_tier=profile.quality_tier, model=profile.model,
            reasoning_effort=profile.reasoning_effort, created_at=self._clock(), rationale=rationale,
        )
        self._decision_store.record(decision)
        return AdaptiveSelection(worker=worker, decision=decision)
