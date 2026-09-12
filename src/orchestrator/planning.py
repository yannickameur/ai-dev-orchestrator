"""Multi-agent release planning + roadmap synthesis (Slice 12).

After a release PASSES (Slice 10's ``ActivityReport``/``ReleaseRecord``),
this module answers: "given the roadmap and the real state of the project,
what should be built next?" — by asking several *independent* Workers
(never hardcoded — always via ``WorkerSelector``, exactly like development/
review/reviewer selection in earlier slices), then having a separate
Worker synthesize their proposals into one structured roadmap diff
(KEEP/ADD/MOVE/DROP) and a proposed next MVP.

This slice stops at producing a durable, structured ``RoadmapProposal``.
It never mutates ``ROADMAP.md``, never creates a real MVP in
``ProjectStateStore``, never notifies anyone, and never approves anything
— that is Slice 13's job, operating on the ``PROPOSED`` proposal this
module persists.

INDEPENDENCE IS STRUCTURAL, NOT A CONVENTION:
every planner's instructions are built from ``PlanningSnapshot`` alone
(see ``_build_planner_instructions``) — the function signature has no
parameter through which a sibling proposal could leak in, by construction.
Comparing proposals happens exclusively in the synthesis step, which
receives every planner proposal, verbatim, in a deterministic order
(sorted by ``worker_id``, never by execution/wall-clock order).

SOURCES OF TRUTH: a ``PlanningSnapshot`` is built once per session, purely
from already-persisted facts — the ``ActivityReport`` of the release being
planned from (never re-probed/re-generated; loaded via
``ActivityReportStore``), and the raw ``ROADMAP.md`` content read from the
project workspace (a SHA-256 ``roadmap_hash`` is captured so a later slice
can detect the roadmap changed between proposal and application — this
slice does not build that application mechanism). Nothing here is ever
derived from a worker's conversational memory or from scraping logs.

RALPH EXECUTION ONLY: planners and the synthesizer are real Workers,
selected via the existing ``WorkerSelector`` (a ``release_planning``/
``roadmap_synthesis`` capability — never a hardcoded provider/worker name)
and run via the existing ``RalphExecutionEngine`` — never a direct
Claude/Codex call, never a raw Ralph subprocess. Their instructions
explicitly forbid modifying project files, committing, pushing, or
changing the roadmap directly: they analyze and propose, nothing else.
Their business events are dedicated (``planning.proposed``/
``planning.failed``, ``synthesis.proposed``/``synthesis.failed``) — never
``task.start``/``task.resume``.

FAIL-CLOSED, EVERYWHERE:
- a planner execution without a reliable ``planning.proposed`` event, or
  with a structurally invalid payload, is recorded as an INVALID
  ``PlannerProposal`` (audit trail) — never silently dropped, and never
  treated as if it had succeeded;
- if fewer than ``PlanningPolicy.planner_count`` planners produce a VALID
  proposal, synthesis is never attempted with a partial set passed off as
  multi-agent — the session moves to ``FAILED`` explicitly;
- the same applies to the synthesizer: a failure leaves every
  ``PlannerProposal`` untouched, produces no ``RoadmapProposal``, and
  changes nothing;
- no automatic retry anywhere in this module — Slice 11's quota-wait/
  recovery mechanisms are not wired into planning execution in this
  slice (a deliberately small scope: "ne construis pas une nouvelle
  boucle complexe ici"); a failed session is corrected by an explicit,
  later call, never an automatic loop.

RESTART: every session/snapshot/proposal/synthesis is durable
(``PlanningStore``, sqlite3 stdlib) and re-readable after a restart.
``run_planners`` is naturally resumable: it only ever launches the
planners still missing (by worker_id) up to ``planner_count`` — it never
re-runs a worker that already has a proposal (VALID or INVALID) recorded
for this exact session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from orchestrator.activity_report import ActivityReport, ActivityReportStore
from orchestrator.execution_store import ExecutionStatus
from orchestrator.project_state import ProjectStateStore
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.worker_selector import NoEligibleWorkerError, Worker, WorkerSelectionRequest, WorkerSelector

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_PLANNING_CAPABILITY = "release_planning"
DEFAULT_SYNTHESIS_CAPABILITY = "roadmap_synthesis"
DEFAULT_TIMEOUT_SECONDS = 900.0

PLANNER_ROLE = "planner"
SYNTHESIZER_ROLE = "synthesizer"

PLANNING_INITIAL_TOPIC = "planning.start"
PLANNING_PROPOSED_TOPIC = "planning.proposed"
PLANNING_FAILED_TOPIC = "planning.failed"
SYNTHESIS_INITIAL_TOPIC = "synthesis.start"
SYNTHESIS_PROPOSED_TOPIC = "synthesis.proposed"
SYNTHESIS_FAILED_TOPIC = "synthesis.failed"

ROADMAP_FILENAME = "ROADMAP.md"

_MAX_PLANNER_SELECTION_ATTEMPTS = 64
_NO_MODIFICATION_NOTICE = (
    "You must ONLY analyze — never modify project files, never commit, never push, "
    "never run destructive commands, and never change the roadmap file directly. "
    "Your output is a proposal only."
)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class RoadmapChangeType(str, Enum):
    KEEP = "keep"
    ADD = "add"
    MOVE = "move"
    DROP = "drop"


class PlanningSessionStatus(str, Enum):
    """Deliberately minimal — approval/notification states belong to Slice 13."""

    COLLECTING = "collecting"
    SYNTHESIZING = "synthesizing"
    PROPOSAL_READY = "proposal_ready"
    FAILED = "failed"


class PlannerProposalStatus(str, Enum):
    """VALID means a structurally-validated ``planning.proposed`` payload was
    obtained; INVALID covers every other outcome (no reliable terminal
    event, missing payload, malformed payload) — recorded for audit, never
    treated as a successful independent proposal."""

    VALID = "valid"
    INVALID = "invalid"


class RoadmapProposalStatus(str, Enum):
    """A single value in this slice — Slice 13 adds AWAITING_APPROVAL/
    APPROVED/REJECTED/AUTO_APPROVED on top of this, never here."""

    PROPOSED = "proposed"


_TERMINAL_SESSION_STATUSES = (PlanningSessionStatus.PROPOSAL_READY, PlanningSessionStatus.FAILED)


@dataclass(frozen=True, slots=True)
class ProposedWorkItem:
    """One structured unit of proposed future work — never free-text."""

    title: str
    objective: str
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        _require_non_empty_str(self.title, field_name="ProposedWorkItem.title")
        _require_non_empty_str(self.objective, field_name="ProposedWorkItem.objective")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))


@dataclass(frozen=True, slots=True)
class RoadmapChange:
    """One KEEP/ADD/MOVE/DROP change against the current roadmap — never a
    real mutation, only a structured proposal."""

    change_type: RoadmapChangeType
    item_reference: str
    reason: str
    target_mvp: str | None = None
    proposed_item: ProposedWorkItem | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.change_type, RoadmapChangeType):
            raise TypeError(f"RoadmapChange.change_type must be a RoadmapChangeType, got {type(self.change_type)!r}")
        _require_non_empty_str(self.item_reference, field_name="RoadmapChange.item_reference")
        _require_non_empty_str(self.reason, field_name="RoadmapChange.reason")
        if self.target_mvp is not None:
            _require_non_empty_str(self.target_mvp, field_name="RoadmapChange.target_mvp")


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One explicit divergence between planners, and how synthesis resolved it."""

    topic: str
    positions: tuple[str, ...]
    resolution: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.topic, field_name="Disagreement.topic")
        _require_non_empty_str(self.resolution, field_name="Disagreement.resolution")
        object.__setattr__(self, "positions", tuple(self.positions))


@dataclass(frozen=True, slots=True)
class ProjectStateSnapshotItem:
    work_item_id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class PlanningSnapshot:
    """The durable, factual context every planner (and the synthesizer) sees.

    Answers, forever: "on which exact information was this roadmap
    proposal produced?" — ``roadmap_hash`` in particular lets a later
    slice detect the roadmap changed since this snapshot was taken.
    """

    snapshot_id: str
    project_id: str
    source_mvp_id: str
    source_release_id: str
    created_at: datetime
    roadmap_content: str
    roadmap_hash: str
    activity_report_id: str
    project_state_summary: tuple[ProjectStateSnapshotItem, ...] = ()
    open_issues: tuple[str, ...] = ()
    git_sha: str | None = None
    roadmap_path: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id", "project_id", "source_mvp_id", "source_release_id",
            "roadmap_hash", "activity_report_id",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"PlanningSnapshot.{name}")
        _require_aware(self.created_at, field_name="PlanningSnapshot.created_at")
        object.__setattr__(self, "project_state_summary", tuple(self.project_state_summary))
        object.__setattr__(self, "open_issues", tuple(self.open_issues))


@dataclass(frozen=True, slots=True)
class PlanningSession:
    """One independent planning cycle for a release — never overwritten;
    a release may have several planning attempts, all kept."""

    planning_session_id: str
    project_id: str
    mvp_id: str
    release_id: str
    snapshot_id: str
    created_at: datetime
    status: PlanningSessionStatus
    policy_planner_count: int
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("planning_session_id", "project_id", "mvp_id", "release_id", "snapshot_id"):
            _require_non_empty_str(getattr(self, name), field_name=f"PlanningSession.{name}")
        _require_aware(self.created_at, field_name="PlanningSession.created_at")
        if not isinstance(self.status, PlanningSessionStatus):
            raise TypeError(f"PlanningSession.status must be a PlanningSessionStatus, got {type(self.status)!r}")
        if not isinstance(self.policy_planner_count, int) or self.policy_planner_count < 1:
            raise ValueError("PlanningSession.policy_planner_count must be a positive int")
        if self.failure_reason is not None:
            _require_non_empty_str(self.failure_reason, field_name="PlanningSession.failure_reason")


@dataclass(frozen=True, slots=True)
class PlannerProposal:
    """One planner's independent output — VALID (structured proposal) or
    INVALID (audit record of a failed/malformed attempt)."""

    proposal_id: str
    planning_session_id: str
    snapshot_id: str
    worker_id: str
    provider: str
    model: str
    execution_id: str
    created_at: datetime
    status: PlannerProposalStatus
    reasoning_effort: str | None = None
    proposed_mvp_objective: str | None = None
    rationale: str | None = None
    proposed_work_items: tuple[ProposedWorkItem, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    deferred_items: tuple[str, ...] = ()
    roadmap_changes: tuple[RoadmapChange, ...] = ()
    error_summary: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "proposal_id", "planning_session_id", "snapshot_id", "worker_id",
            "provider", "model", "execution_id",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"PlannerProposal.{name}")
        _require_aware(self.created_at, field_name="PlannerProposal.created_at")
        if not isinstance(self.status, PlannerProposalStatus):
            raise TypeError(f"PlannerProposal.status must be a PlannerProposalStatus, got {type(self.status)!r}")
        object.__setattr__(self, "proposed_work_items", tuple(self.proposed_work_items))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "deferred_items", tuple(self.deferred_items))
        object.__setattr__(self, "roadmap_changes", tuple(self.roadmap_changes))


@dataclass(frozen=True, slots=True)
class RoadmapProposal:
    """The synthesized outcome of one planning session — a structured
    diff (KEEP/ADD/MOVE/DROP) plus a proposed next MVP. Never a real
    ROADMAP.md rewrite, never a real MVP in ProjectStateStore."""

    roadmap_proposal_id: str
    planning_session_id: str
    snapshot_id: str
    synthesizer_worker_id: str
    synthesizer_execution_id: str
    created_at: datetime
    source_proposal_ids: tuple[str, ...]
    next_mvp_objective: str
    next_mvp_rationale: str
    next_mvp_work_items: tuple[ProposedWorkItem, ...] = ()
    next_mvp_acceptance_criteria: tuple[str, ...] = ()
    next_mvp_deferred_items: tuple[str, ...] = ()
    roadmap_changes: tuple[RoadmapChange, ...] = ()
    risks: tuple[str, ...] = ()
    agreements: tuple[str, ...] = ()
    disagreements: tuple[Disagreement, ...] = ()
    rationale: str = ""
    status: RoadmapProposalStatus = RoadmapProposalStatus.PROPOSED

    def __post_init__(self) -> None:
        for name in (
            "roadmap_proposal_id", "planning_session_id", "snapshot_id",
            "synthesizer_worker_id", "synthesizer_execution_id",
            "next_mvp_objective", "next_mvp_rationale",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"RoadmapProposal.{name}")
        _require_aware(self.created_at, field_name="RoadmapProposal.created_at")
        if not isinstance(self.status, RoadmapProposalStatus):
            raise TypeError(f"RoadmapProposal.status must be a RoadmapProposalStatus, got {type(self.status)!r}")
        object.__setattr__(self, "source_proposal_ids", tuple(self.source_proposal_ids))
        object.__setattr__(self, "next_mvp_work_items", tuple(self.next_mvp_work_items))
        object.__setattr__(self, "next_mvp_acceptance_criteria", tuple(self.next_mvp_acceptance_criteria))
        object.__setattr__(self, "next_mvp_deferred_items", tuple(self.next_mvp_deferred_items))
        object.__setattr__(self, "roadmap_changes", tuple(self.roadmap_changes))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "agreements", tuple(self.agreements))
        object.__setattr__(self, "disagreements", tuple(self.disagreements))


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    """Small, explicit policy — no algorithm change needed to add more planners.

    ``require_distinct_workers`` is pinned ``True``: the same worker must
    never produce two proposals in the same session (structurally
    guaranteed anyway by ``PlanningCoordinator``'s exclusion loop, but kept
    explicit here for documentation/readability, matching
    ``WorkerSelectionPolicy.require_distinct_worker_for_review``'s pattern).
    """

    planner_count: int = 2
    prefer_distinct_providers: bool = True
    require_distinct_workers: bool = True
    prefer_distinct_synthesizer_worker: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.planner_count, int) or self.planner_count < 1:
            raise ValueError("PlanningPolicy.planner_count must be a positive int")
        if self.require_distinct_workers is not True:
            raise ValueError(
                "PlanningPolicy.require_distinct_workers must be True: the same worker must "
                "never produce two proposals in one planning session"
            )


class PlanningError(Exception):
    """Base for planning domain errors."""


class RoadmapFileNotFoundError(PlanningError):
    def __init__(self, path: str) -> None:
        super().__init__(f"roadmap file not found: {path!r}")
        self.path = path


class NoActivityReportForReleaseError(PlanningError):
    def __init__(self, mvp_id: str, release_id: str) -> None:
        super().__init__(f"no activity report found for mvp={mvp_id!r} release={release_id!r}")
        self.mvp_id = mvp_id
        self.release_id = release_id


class PlanningSessionFailedError(PlanningError):
    def __init__(self, planning_session_id: str, reason: str) -> None:
        super().__init__(f"planning session {planning_session_id!r} failed: {reason}")
        self.planning_session_id = planning_session_id
        self.reason = reason


class PlanningStoreError(Exception):
    """Base for PlanningStore domain errors."""


class UnknownPlanningSessionError(PlanningStoreError):
    def __init__(self, planning_session_id: str) -> None:
        super().__init__(f"unknown planning session: {planning_session_id!r}")
        self.planning_session_id = planning_session_id


class DuplicatePlanningSessionError(PlanningStoreError):
    def __init__(self, planning_session_id: str) -> None:
        super().__init__(f"planning session already exists: {planning_session_id!r}")
        self.planning_session_id = planning_session_id


class UnknownPlanningSnapshotError(PlanningStoreError):
    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"unknown planning snapshot: {snapshot_id!r}")
        self.snapshot_id = snapshot_id


class UnknownRoadmapProposalError(PlanningStoreError):
    def __init__(self, roadmap_proposal_id: str) -> None:
        super().__init__(f"unknown roadmap proposal: {roadmap_proposal_id!r}")
        self.roadmap_proposal_id = roadmap_proposal_id


class InvalidPlanningSessionTransitionError(PlanningStoreError):
    def __init__(self, planning_session_id: str, current: PlanningSessionStatus, attempted: PlanningSessionStatus) -> None:
        super().__init__(
            f"planning session {planning_session_id!r} cannot move from "
            f"{current.value!r} to {attempted.value!r}: already terminal"
        )
        self.planning_session_id = planning_session_id
        self.current = current
        self.attempted = attempted


# --- serialization helpers -------------------------------------------------


def _encode_work_item(item: ProposedWorkItem) -> dict:
    return {
        "title": item.title, "objective": item.objective, "dependencies": list(item.dependencies),
        "acceptance_criteria": list(item.acceptance_criteria),
        "required_capabilities": list(item.required_capabilities), "rationale": item.rationale,
    }


def _decode_work_item(data: dict) -> ProposedWorkItem:
    return ProposedWorkItem(
        title=data["title"], objective=data["objective"], dependencies=tuple(data["dependencies"]),
        acceptance_criteria=tuple(data["acceptance_criteria"]),
        required_capabilities=tuple(data["required_capabilities"]), rationale=data["rationale"],
    )


def _encode_change(change: RoadmapChange) -> dict:
    return {
        "change_type": change.change_type.value, "item_reference": change.item_reference,
        "reason": change.reason, "target_mvp": change.target_mvp,
        "proposed_item": _encode_work_item(change.proposed_item) if change.proposed_item else None,
    }


def _decode_change(data: dict) -> RoadmapChange:
    return RoadmapChange(
        change_type=RoadmapChangeType(data["change_type"]), item_reference=data["item_reference"],
        reason=data["reason"], target_mvp=data["target_mvp"],
        proposed_item=_decode_work_item(data["proposed_item"]) if data["proposed_item"] else None,
    )


def _encode_disagreement(d: Disagreement) -> dict:
    return {"topic": d.topic, "positions": list(d.positions), "resolution": d.resolution}


def _decode_disagreement(data: dict) -> Disagreement:
    return Disagreement(topic=data["topic"], positions=tuple(data["positions"]), resolution=data["resolution"])


def _serialize_snapshot(snapshot: PlanningSnapshot) -> str:
    return json.dumps({
        "snapshot_id": snapshot.snapshot_id, "project_id": snapshot.project_id,
        "source_mvp_id": snapshot.source_mvp_id, "source_release_id": snapshot.source_release_id,
        "created_at": snapshot.created_at.isoformat(), "roadmap_content": snapshot.roadmap_content,
        "roadmap_hash": snapshot.roadmap_hash, "activity_report_id": snapshot.activity_report_id,
        "project_state_summary": [
            {"work_item_id": i.work_item_id, "title": i.title, "status": i.status}
            for i in snapshot.project_state_summary
        ],
        "open_issues": list(snapshot.open_issues), "git_sha": snapshot.git_sha,
        "roadmap_path": snapshot.roadmap_path,
    })


def _deserialize_snapshot(raw: str) -> PlanningSnapshot:
    data = json.loads(raw)
    return PlanningSnapshot(
        snapshot_id=data["snapshot_id"], project_id=data["project_id"],
        source_mvp_id=data["source_mvp_id"], source_release_id=data["source_release_id"],
        created_at=datetime.fromisoformat(data["created_at"]), roadmap_content=data["roadmap_content"],
        roadmap_hash=data["roadmap_hash"], activity_report_id=data["activity_report_id"],
        project_state_summary=tuple(
            ProjectStateSnapshotItem(work_item_id=i["work_item_id"], title=i["title"], status=i["status"])
            for i in data["project_state_summary"]
        ),
        open_issues=tuple(data["open_issues"]), git_sha=data["git_sha"], roadmap_path=data["roadmap_path"],
    )


def _serialize_proposal(p: PlannerProposal) -> str:
    return json.dumps({
        "proposal_id": p.proposal_id, "planning_session_id": p.planning_session_id,
        "snapshot_id": p.snapshot_id, "worker_id": p.worker_id, "provider": p.provider,
        "model": p.model, "reasoning_effort": p.reasoning_effort, "execution_id": p.execution_id,
        "created_at": p.created_at.isoformat(), "status": p.status.value,
        "proposed_mvp_objective": p.proposed_mvp_objective, "rationale": p.rationale,
        "proposed_work_items": [_encode_work_item(w) for w in p.proposed_work_items],
        "acceptance_criteria": list(p.acceptance_criteria), "risks": list(p.risks),
        "deferred_items": list(p.deferred_items),
        "roadmap_changes": [_encode_change(c) for c in p.roadmap_changes],
        "error_summary": p.error_summary,
    })


def _deserialize_proposal(raw: str) -> PlannerProposal:
    data = json.loads(raw)
    return PlannerProposal(
        proposal_id=data["proposal_id"], planning_session_id=data["planning_session_id"],
        snapshot_id=data["snapshot_id"], worker_id=data["worker_id"], provider=data["provider"],
        model=data["model"], reasoning_effort=data["reasoning_effort"], execution_id=data["execution_id"],
        created_at=datetime.fromisoformat(data["created_at"]), status=PlannerProposalStatus(data["status"]),
        proposed_mvp_objective=data["proposed_mvp_objective"], rationale=data["rationale"],
        proposed_work_items=tuple(_decode_work_item(w) for w in data["proposed_work_items"]),
        acceptance_criteria=tuple(data["acceptance_criteria"]), risks=tuple(data["risks"]),
        deferred_items=tuple(data["deferred_items"]),
        roadmap_changes=tuple(_decode_change(c) for c in data["roadmap_changes"]),
        error_summary=data["error_summary"],
    )


def _serialize_roadmap_proposal(r: RoadmapProposal) -> str:
    return json.dumps({
        "roadmap_proposal_id": r.roadmap_proposal_id, "planning_session_id": r.planning_session_id,
        "snapshot_id": r.snapshot_id, "synthesizer_worker_id": r.synthesizer_worker_id,
        "synthesizer_execution_id": r.synthesizer_execution_id, "created_at": r.created_at.isoformat(),
        "source_proposal_ids": list(r.source_proposal_ids), "next_mvp_objective": r.next_mvp_objective,
        "next_mvp_rationale": r.next_mvp_rationale,
        "next_mvp_work_items": [_encode_work_item(w) for w in r.next_mvp_work_items],
        "next_mvp_acceptance_criteria": list(r.next_mvp_acceptance_criteria),
        "next_mvp_deferred_items": list(r.next_mvp_deferred_items),
        "roadmap_changes": [_encode_change(c) for c in r.roadmap_changes],
        "risks": list(r.risks), "agreements": list(r.agreements),
        "disagreements": [_encode_disagreement(d) for d in r.disagreements],
        "rationale": r.rationale, "status": r.status.value,
    })


def _deserialize_roadmap_proposal(raw: str) -> RoadmapProposal:
    data = json.loads(raw)
    return RoadmapProposal(
        roadmap_proposal_id=data["roadmap_proposal_id"], planning_session_id=data["planning_session_id"],
        snapshot_id=data["snapshot_id"], synthesizer_worker_id=data["synthesizer_worker_id"],
        synthesizer_execution_id=data["synthesizer_execution_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        source_proposal_ids=tuple(data["source_proposal_ids"]), next_mvp_objective=data["next_mvp_objective"],
        next_mvp_rationale=data["next_mvp_rationale"],
        next_mvp_work_items=tuple(_decode_work_item(w) for w in data["next_mvp_work_items"]),
        next_mvp_acceptance_criteria=tuple(data["next_mvp_acceptance_criteria"]),
        next_mvp_deferred_items=tuple(data["next_mvp_deferred_items"]),
        roadmap_changes=tuple(_decode_change(c) for c in data["roadmap_changes"]),
        risks=tuple(data["risks"]), agreements=tuple(data["agreements"]),
        disagreements=tuple(_decode_disagreement(d) for d in data["disagreements"]),
        rationale=data["rationale"], status=RoadmapProposalStatus(data["status"]),
    )


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS planning_sessions (
    planning_session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    policy_planner_count INTEGER NOT NULL,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS planning_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planner_proposals (
    proposal_id TEXT PRIMARY KEY,
    planning_session_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadmap_proposals (
    roadmap_proposal_id TEXT PRIMARY KEY,
    planning_session_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _decode_session_row(row: sqlite3.Row) -> PlanningSession:
    return PlanningSession(
        planning_session_id=row["planning_session_id"], project_id=row["project_id"],
        mvp_id=row["mvp_id"], release_id=row["release_id"], snapshot_id=row["snapshot_id"],
        created_at=datetime.fromisoformat(row["created_at"]), status=PlanningSessionStatus(row["status"]),
        policy_planner_count=row["policy_planner_count"], failure_reason=row["failure_reason"],
    )


class PlanningStore:
    """Synchronous, sqlite3-backed store for planning sessions/snapshots/proposals."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_CREATE_TABLES_SQL)

    def close(self) -> None:
        self._conn.close()

    # --- sessions --------------------------------------------------------

    def create_session(self, session: PlanningSession) -> PlanningSession:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO planning_sessions (planning_session_id, project_id, mvp_id, "
                    "release_id, snapshot_id, created_at, status, policy_planner_count, "
                    "failure_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session.planning_session_id, session.project_id, session.mvp_id,
                        session.release_id, session.snapshot_id, session.created_at.isoformat(),
                        session.status.value, session.policy_planner_count, session.failure_reason,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicatePlanningSessionError(session.planning_session_id) from exc
        return session

    def get_session(self, planning_session_id: str) -> PlanningSession:
        row = self._conn.execute(
            "SELECT * FROM planning_sessions WHERE planning_session_id = ?", (planning_session_id,)
        ).fetchone()
        if row is None:
            raise UnknownPlanningSessionError(planning_session_id)
        return _decode_session_row(row)

    def update_session_status(
        self, planning_session_id: str, status: PlanningSessionStatus, *, failure_reason: str | None = None
    ) -> PlanningSession:
        current = self.get_session(planning_session_id)
        if current.status in _TERMINAL_SESSION_STATUSES:
            raise InvalidPlanningSessionTransitionError(planning_session_id, current.status, status)
        updated = replace(current, status=status, failure_reason=failure_reason)
        with self._conn:
            self._conn.execute(
                "UPDATE planning_sessions SET status = ?, failure_reason = ? WHERE planning_session_id = ?",
                (updated.status.value, updated.failure_reason, planning_session_id),
            )
        return updated

    def list_sessions_for_release(self, release_id: str) -> list[PlanningSession]:
        """Every planning attempt for a release, oldest first — none ever overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM planning_sessions WHERE release_id = ? ORDER BY created_at ASC", (release_id,)
        ).fetchall()
        return [_decode_session_row(row) for row in rows]

    def latest_session_for_release(self, release_id: str) -> PlanningSession | None:
        sessions = self.list_sessions_for_release(release_id)
        return sessions[-1] if sessions else None

    # --- snapshots ---------------------------------------------------------

    def record_snapshot(self, snapshot: PlanningSnapshot) -> PlanningSnapshot:
        with self._conn:
            self._conn.execute(
                "INSERT INTO planning_snapshots (snapshot_id, project_id, payload) VALUES (?, ?, ?)",
                (snapshot.snapshot_id, snapshot.project_id, _serialize_snapshot(snapshot)),
            )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> PlanningSnapshot:
        row = self._conn.execute(
            "SELECT * FROM planning_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise UnknownPlanningSnapshotError(snapshot_id)
        return _deserialize_snapshot(row["payload"])

    # --- planner proposals ---------------------------------------------------

    def record_planner_proposal(self, proposal: PlannerProposal) -> PlannerProposal:
        with self._conn:
            self._conn.execute(
                "INSERT INTO planner_proposals (proposal_id, planning_session_id, worker_id, "
                "created_at, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id, proposal.planning_session_id, proposal.worker_id,
                    proposal.created_at.isoformat(), _serialize_proposal(proposal),
                ),
            )
        return proposal

    def list_planner_proposals(self, planning_session_id: str) -> list[PlannerProposal]:
        """Deterministic order (worker_id) — never dependent on execution/wall-clock order."""
        rows = self._conn.execute(
            "SELECT * FROM planner_proposals WHERE planning_session_id = ? ORDER BY worker_id ASC",
            (planning_session_id,),
        ).fetchall()
        return [_deserialize_proposal(row["payload"]) for row in rows]

    # --- roadmap proposals ---------------------------------------------------

    def record_roadmap_proposal(self, proposal: RoadmapProposal) -> RoadmapProposal:
        with self._conn:
            self._conn.execute(
                "INSERT INTO roadmap_proposals (roadmap_proposal_id, planning_session_id, payload) "
                "VALUES (?, ?, ?)",
                (
                    proposal.roadmap_proposal_id, proposal.planning_session_id,
                    _serialize_roadmap_proposal(proposal),
                ),
            )
        return proposal

    def get_roadmap_proposal(self, roadmap_proposal_id: str) -> RoadmapProposal:
        row = self._conn.execute(
            "SELECT * FROM roadmap_proposals WHERE roadmap_proposal_id = ?", (roadmap_proposal_id,)
        ).fetchone()
        if row is None:
            raise UnknownRoadmapProposalError(roadmap_proposal_id)
        return _deserialize_roadmap_proposal(row["payload"])

    def latest_roadmap_proposal_for_session(self, planning_session_id: str) -> RoadmapProposal | None:
        row = self._conn.execute(
            "SELECT * FROM roadmap_proposals WHERE planning_session_id = ? ORDER BY rowid DESC LIMIT 1",
            (planning_session_id,),
        ).fetchone()
        if row is None:
            return None
        return _deserialize_roadmap_proposal(row["payload"])


# --- payload parsing (strict, fail-closed) ----------------------------------


def _require_str(data: dict, key: str) -> str:
    if key not in data:
        raise ValueError(f"missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"field {key!r} must be a non-empty string")
    return value


def _require_str_list(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"field {key!r} must be a list of strings")
    return tuple(value)


def _require_list(data: dict, key: str) -> list:
    if key not in data:
        raise ValueError(f"missing required field {key!r}")
    value = data[key]
    if not isinstance(value, list):
        raise ValueError(f"field {key!r} must be a list")
    return value


def _parse_proposed_work_item(data: object) -> ProposedWorkItem:
    if not isinstance(data, dict):
        raise ValueError("a proposed work item must be a JSON object")
    return ProposedWorkItem(
        title=_require_str(data, "title"), objective=_require_str(data, "objective"),
        dependencies=_require_str_list(data, "dependencies"),
        acceptance_criteria=_require_str_list(data, "acceptance_criteria"),
        required_capabilities=_require_str_list(data, "required_capabilities"),
        rationale=data.get("rationale") or "",
    )


def _parse_roadmap_change(data: object) -> RoadmapChange:
    if not isinstance(data, dict):
        raise ValueError("a roadmap change must be a JSON object")
    change_type = RoadmapChangeType(_require_str(data, "type").lower())
    target_mvp = data.get("target_mvp")
    if target_mvp is not None and not isinstance(target_mvp, str):
        raise ValueError("field 'target_mvp' must be a string or null")
    proposed_item_data = data.get("proposed_item")
    proposed_item = _parse_proposed_work_item(proposed_item_data) if proposed_item_data is not None else None
    return RoadmapChange(
        change_type=change_type, item_reference=_require_str(data, "item_reference"),
        reason=_require_str(data, "reason"), target_mvp=target_mvp, proposed_item=proposed_item,
    )


def _parse_disagreement(data: object) -> Disagreement:
    if not isinstance(data, dict):
        raise ValueError("a disagreement must be a JSON object")
    return Disagreement(
        topic=_require_str(data, "topic"), positions=_require_str_list(data, "positions"),
        resolution=_require_str(data, "resolution"),
    )


def _parse_planner_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("planning.proposed payload must be a JSON object")
    return {
        "proposed_mvp_objective": _require_str(data, "proposed_mvp_objective"),
        "rationale": _require_str(data, "rationale"),
        "proposed_work_items": tuple(
            _parse_proposed_work_item(item) for item in _require_list(data, "proposed_work_items")
        ),
        "acceptance_criteria": _require_str_list(data, "acceptance_criteria"),
        "risks": _require_str_list(data, "risks"),
        "deferred_items": _require_str_list(data, "deferred_items"),
        "roadmap_changes": tuple(_parse_roadmap_change(item) for item in data.get("roadmap_changes", [])),
    }


def _parse_synthesizer_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("synthesis.proposed payload must be a JSON object")
    return {
        "next_mvp_objective": _require_str(data, "next_mvp_objective"),
        "next_mvp_rationale": _require_str(data, "next_mvp_rationale"),
        "next_mvp_work_items": tuple(
            _parse_proposed_work_item(item) for item in _require_list(data, "next_mvp_work_items")
        ),
        "next_mvp_acceptance_criteria": _require_str_list(data, "next_mvp_acceptance_criteria"),
        "next_mvp_deferred_items": _require_str_list(data, "next_mvp_deferred_items"),
        "roadmap_changes": tuple(_parse_roadmap_change(item) for item in data.get("roadmap_changes", [])),
        "risks": _require_str_list(data, "risks"),
        "agreements": _require_str_list(data, "agreements"),
        "disagreements": tuple(_parse_disagreement(item) for item in data.get("disagreements", [])),
        "rationale": _require_str(data, "rationale"),
    }


# --- instructions (never leak sibling proposals into a planner's prompt) ---


def _build_planner_instructions(snapshot: PlanningSnapshot) -> str:
    state_block = "\n".join(
        f"- `{i.work_item_id}` {i.title} — status={i.status}" for i in snapshot.project_state_summary
    ) or "- (no work items recorded)"
    issues_block = "\n".join(f"- {issue}" for issue in snapshot.open_issues) or "- (none recorded)"
    return (
        "You are an independent release planner. You do not know whether any other planner "
        "exists for this session, and you have no access to any other analysis of this "
        "project — only the facts below.\n\n"
        f"{_NO_MODIFICATION_NOTICE}\n\n"
        f"Current {ROADMAP_FILENAME} (verbatim, sha256={snapshot.roadmap_hash}):\n\n"
        f"{snapshot.roadmap_content}\n\n"
        f"Project state at the end of the just-completed release (activity report "
        f"{snapshot.activity_report_id}, git SHA {snapshot.git_sha or '(none)'}):\n\n{state_block}\n\n"
        f"Open issues / blockers recorded for this release:\n\n{issues_block}\n\n"
        "Given the roadmap and the real state above, propose what should be built next. "
        f'Emit exactly:\n\nralph emit "{PLANNING_PROPOSED_TOPIC}" \'<JSON object>\'\n\n'
        "The JSON object must have exactly these fields: proposed_mvp_objective (string), "
        "rationale (string), proposed_work_items (array of objects with title, objective, "
        "dependencies [array of other proposed titles], acceptance_criteria [array of "
        "strings], required_capabilities [array of strings], rationale), acceptance_criteria "
        "(array of strings, for the MVP as a whole), risks (array of strings), deferred_items "
        "(array of strings), roadmap_changes (array of objects with type [keep/add/move/drop], "
        "item_reference, target_mvp [string or null], reason, proposed_item [object shaped "
        "like a proposed_work_items entry, or null]).\n\n"
        f'If you cannot produce a reliable proposal, emit exactly:\n\nralph emit '
        f'"{PLANNING_FAILED_TOPIC}" "<short reason>"\n\nThen output:\n\nLOOP_COMPLETE\n'
    )


def _render_proposal_for_synthesis(p: PlannerProposal) -> str:
    items = "\n".join(f"  - {w.title}: {w.objective}" for w in p.proposed_work_items) or "  (none)"
    changes = "\n".join(
        f"  - {c.change_type.value} {c.item_reference}"
        f"{f' -> {c.target_mvp}' if c.target_mvp else ''}: {c.reason}"
        for c in p.roadmap_changes
    ) or "  (none)"
    return (
        f"Proposal from worker {p.worker_id} (provider={p.provider}, model={p.model}):\n"
        f"- Objective: {p.proposed_mvp_objective}\n"
        f"- Rationale: {p.rationale}\n"
        f"- Work items:\n{items}\n"
        f"- Roadmap changes:\n{changes}\n"
        f"- Risks: {'; '.join(p.risks) or '(none)'}\n"
        f"- Deferred: {'; '.join(p.deferred_items) or '(none)'}"
    )


def _build_synthesizer_instructions(snapshot: PlanningSnapshot, proposals: Sequence[PlannerProposal]) -> str:
    proposals_block = "\n\n".join(_render_proposal_for_synthesis(p) for p in proposals)
    return (
        "You are the roadmap synthesizer. You receive the exact same factual snapshot the "
        "planners received, plus every independent planner proposal below, verbatim and "
        "unfiltered — never a summary prepared by the orchestrator.\n\n"
        f"{_NO_MODIFICATION_NOTICE}\n\n"
        f"Current {ROADMAP_FILENAME} (sha256={snapshot.roadmap_hash}):\n\n{snapshot.roadmap_content}\n\n"
        f"Independent planner proposals ({len(proposals)}):\n\n{proposals_block}\n\n"
        "Compare the proposals explicitly. Identify agreements and disagreements, and resolve "
        "each disagreement with a stated rationale — never silently drop one proposal's view.\n\n"
        f'Emit exactly:\n\nralph emit "{SYNTHESIS_PROPOSED_TOPIC}" \'<JSON object>\'\n\n'
        "Fields: next_mvp_objective (string), next_mvp_rationale (string), next_mvp_work_items "
        "(array, same shape as a proposed_work_items entry), next_mvp_acceptance_criteria "
        "(array of strings), next_mvp_deferred_items (array of strings), roadmap_changes "
        "(array, same shape as a planner's roadmap_changes), risks (array of strings), "
        "agreements (array of strings), disagreements (array of objects with topic, positions "
        "[array of strings], resolution), rationale (string, overall synthesis rationale).\n\n"
        f'If you cannot produce a reliable synthesis, emit exactly:\n\nralph emit '
        f'"{SYNTHESIS_FAILED_TOPIC}" "<short reason>"\n\nThen output:\n\nLOOP_COMPLETE\n'
    )


def _read_roadmap(workspace: Path) -> tuple[str, str, str]:
    roadmap_path = workspace / ROADMAP_FILENAME
    try:
        content = roadmap_path.read_text()
    except OSError as exc:
        raise RoadmapFileNotFoundError(str(roadmap_path)) from exc
    roadmap_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return content, roadmap_hash, str(roadmap_path)


def render_markdown(proposal: RoadmapProposal) -> str:
    """Deterministic, pure Markdown rendering of a RoadmapProposal — no LLM involved."""
    lines = ["# Proposed roadmap update", ""]
    for change_type in (RoadmapChangeType.KEEP, RoadmapChangeType.ADD, RoadmapChangeType.MOVE, RoadmapChangeType.DROP):
        lines += [f"## {change_type.value.upper()}", ""]
        matching = [c for c in proposal.roadmap_changes if c.change_type is change_type]
        if matching:
            for c in matching:
                target = f" -> {c.target_mvp}" if c.target_mvp else ""
                lines.append(f"- {c.item_reference}{target}: {c.reason}")
        else:
            lines.append("(none)")
        lines.append("")

    lines += ["## Proposed next MVP", ""]
    lines.append(f"- Objective: {proposal.next_mvp_objective}")
    lines.append(f"- Rationale: {proposal.next_mvp_rationale}")
    lines.append("- Work items:")
    for wi in proposal.next_mvp_work_items:
        lines.append(f"  - {wi.title}: {wi.objective}")
    lines.append("- Acceptance criteria:")
    for ac in proposal.next_mvp_acceptance_criteria:
        lines.append(f"  - {ac}")
    lines.append("- Deferred items:")
    for d in proposal.next_mvp_deferred_items:
        lines.append(f"  - {d}")

    lines += ["", "## Risks", ""]
    lines += [f"- {r}" for r in proposal.risks] or ["(none)"]

    lines += ["", "## Agreements", ""]
    lines += [f"- {a}" for a in proposal.agreements] or ["(none)"]

    lines += ["", "## Disagreements", ""]
    if proposal.disagreements:
        for d in proposal.disagreements:
            lines.append(f"- {d.topic}")
            for pos in d.positions:
                lines.append(f"  - {pos}")
            lines.append(f"  - resolution: {d.resolution}")
    else:
        lines.append("(none)")

    lines += ["", "## Rationale", "", proposal.rationale, ""]
    return "\n".join(lines) + "\n"


class PlanningCoordinator:
    """Runs one release-planning cycle: independent planners, then synthesis.

    Composes existing services only — ``WorkerSelector`` for planner/
    synthesizer selection (capability/governance/availability, never
    duplicated here), ``RalphExecutionEngine`` for the actual execution
    (never a direct Claude/Codex/Ralph call), ``ActivityReportStore`` for
    the factual release snapshot (never re-generated), ``ProjectStateStore``
    only to resolve the project workspace path for reading ``ROADMAP.md``.
    """

    def __init__(
        self,
        planning_store: PlanningStore,
        worker_selector: WorkerSelector,
        execution_engine: RalphExecutionEngine,
        project_state_store: ProjectStateStore,
        activity_report_store: ActivityReportStore,
        *,
        policy: PlanningPolicy | None = None,
        planning_capability: str = DEFAULT_PLANNING_CAPABILITY,
        synthesis_capability: str = DEFAULT_SYNTHESIS_CAPABILITY,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._planning_store = planning_store
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._project_state_store = project_state_store
        self._activity_report_store = activity_report_store
        self._policy = policy or PlanningPolicy()
        self._planning_capability = planning_capability
        self._synthesis_capability = synthesis_capability
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._timeout_seconds = timeout_seconds

    async def start_planning_session(self, *, project_id: str, mvp_id: str, release_id: str) -> PlanningSession:
        """Builds the durable PlanningSnapshot and creates a new COLLECTING session.

        Always a fresh session (a release may have several planning
        attempts, all kept) — never overwrites a previous one.
        """
        report = self._resolve_activity_report(mvp_id=mvp_id, release_id=release_id)
        project = self._project_state_store.get_project(project_id)
        roadmap_content, roadmap_hash, roadmap_path = _read_roadmap(project.workspace)

        now = self._clock()
        snapshot = PlanningSnapshot(
            snapshot_id=self._id_factory(), project_id=project_id, source_mvp_id=mvp_id,
            source_release_id=release_id, created_at=now, roadmap_content=roadmap_content,
            roadmap_hash=roadmap_hash, roadmap_path=roadmap_path, activity_report_id=report.report_id,
            git_sha=report.git_sha,
            project_state_summary=tuple(
                ProjectStateSnapshotItem(work_item_id=w.work_item_id, title=w.title, status=w.status)
                for w in sorted(report.work_items, key=lambda w: w.work_item_id)
            ),
            open_issues=tuple(f"[{i.kind}] {i.work_item_id}: {i.summary}" for i in report.incidents),
        )
        self._planning_store.record_snapshot(snapshot)

        session = PlanningSession(
            planning_session_id=self._id_factory(), project_id=project_id, mvp_id=mvp_id,
            release_id=release_id, snapshot_id=snapshot.snapshot_id, created_at=now,
            status=PlanningSessionStatus.COLLECTING, policy_planner_count=self._policy.planner_count,
        )
        return self._planning_store.create_session(session)

    def _resolve_activity_report(self, *, mvp_id: str, release_id: str) -> ActivityReport:
        report = self._activity_report_store.latest_for_mvp(mvp_id)
        if report is None or report.release_id != release_id:
            raise NoActivityReportForReleaseError(mvp_id, release_id)
        return report

    async def run_planners(self, planning_session_id: str) -> list[PlannerProposal]:
        """Runs every planner still missing for this session, up to ``planner_count``.

        Resumable by construction: a worker with an existing proposal
        (VALID or INVALID) for this session is never re-run automatically.
        """
        session = self._planning_store.get_session(planning_session_id)
        snapshot = self._planning_store.get_snapshot(session.snapshot_id)
        existing = self._planning_store.list_planner_proposals(planning_session_id)
        remaining = session.policy_planner_count - len(existing)
        if remaining <= 0:
            return existing

        excluded_worker_ids = {p.worker_id for p in existing}
        used_providers = {p.provider for p in existing}
        new_proposals: list[PlannerProposal] = []
        try:
            for _ in range(remaining):
                worker = await self._select_planner(
                    excluded_worker_ids=excluded_worker_ids, used_providers=used_providers
                )
                excluded_worker_ids.add(worker.worker_id)
                used_providers.add(worker.provider)
                proposal = await self._run_one_planner(session=session, worker=worker, snapshot=snapshot)
                self._planning_store.record_planner_proposal(proposal)
                new_proposals.append(proposal)
        except NoEligibleWorkerError as exc:
            reason = (
                f"could not select {session.policy_planner_count} distinct eligible planners "
                f"(capability={self._planning_capability!r}): {exc}"
            )
            self._planning_store.update_session_status(
                planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason
            )
            raise PlanningSessionFailedError(planning_session_id, reason) from exc

        return existing + new_proposals

    async def _select_planner(self, *, excluded_worker_ids: set[str], used_providers: set[str]) -> Worker:
        excluded = set(excluded_worker_ids)
        fallback_same_provider: Worker | None = None
        for _ in range(_MAX_PLANNER_SELECTION_ATTEMPTS):
            try:
                candidate = await self._worker_selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({self._planning_capability}),
                        excluded_worker_ids=frozenset(excluded),
                    )
                )
            except NoEligibleWorkerError:
                if fallback_same_provider is not None:
                    return fallback_same_provider
                raise
            if not self._policy.prefer_distinct_providers or candidate.provider not in used_providers:
                return candidate
            if fallback_same_provider is None:
                fallback_same_provider = candidate
            excluded.add(candidate.worker_id)
        if fallback_same_provider is not None:
            return fallback_same_provider
        raise NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset({self._planning_capability}))
        )

    async def _run_one_planner(
        self, *, session: PlanningSession, worker: Worker, snapshot: PlanningSnapshot
    ) -> PlannerProposal:
        execution_id = self._id_factory()
        project = self._project_state_store.get_project(session.project_id)
        request = ExecutionRequest(
            execution_id=execution_id, task_id=f"planning:{session.planning_session_id}",
            worker=worker, role=PLANNER_ROLE, workspace=project.workspace,
            instructions=_build_planner_instructions(snapshot),
            initial_event_topic=PLANNING_INITIAL_TOPIC,
            success_topics=frozenset({PLANNING_PROPOSED_TOPIC}),
            failure_topics=frozenset({PLANNING_FAILED_TOPIC}),
            timeout_seconds=self._timeout_seconds,
        )
        try:
            result = await self._execution_engine.execute(request)
        except RalphExecutionEngineError as exc:
            return self._invalid_proposal(session, worker, execution_id, error_summary=f"planner execution failed to run: {exc}")

        if result.record.status is not ExecutionStatus.SUCCEEDED:
            return self._invalid_proposal(
                session, worker, execution_id,
                error_summary=f"no reliable {PLANNING_PROPOSED_TOPIC} event (status={result.record.status.value})",
            )

        proposed_event = next((e for e in result.events if e.topic == PLANNING_PROPOSED_TOPIC), None)
        if proposed_event is None or not proposed_event.payload:
            return self._invalid_proposal(session, worker, execution_id, error_summary="missing planning.proposed payload")

        try:
            data = json.loads(proposed_event.payload)
            parsed = _parse_planner_payload(data)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._invalid_proposal(session, worker, execution_id, error_summary=f"invalid planning.proposed payload: {exc}")

        return PlannerProposal(
            proposal_id=self._id_factory(), planning_session_id=session.planning_session_id,
            snapshot_id=session.snapshot_id, worker_id=worker.worker_id, provider=worker.provider,
            model=worker.model, reasoning_effort=worker.reasoning_effort, execution_id=execution_id,
            created_at=self._clock(), status=PlannerProposalStatus.VALID, **parsed,
        )

    def _invalid_proposal(
        self, session: PlanningSession, worker: Worker, execution_id: str, *, error_summary: str
    ) -> PlannerProposal:
        return PlannerProposal(
            proposal_id=self._id_factory(), planning_session_id=session.planning_session_id,
            snapshot_id=session.snapshot_id, worker_id=worker.worker_id, provider=worker.provider,
            model=worker.model, reasoning_effort=worker.reasoning_effort, execution_id=execution_id,
            created_at=self._clock(), status=PlannerProposalStatus.INVALID, error_summary=error_summary,
        )

    async def _select_synthesizer(self, *, planner_worker_ids: set[str]) -> Worker:
        if self._policy.prefer_distinct_synthesizer_worker:
            try:
                return await self._worker_selector.select(
                    WorkerSelectionRequest(
                        required_capabilities=frozenset({self._synthesis_capability}),
                        excluded_worker_ids=frozenset(planner_worker_ids),
                    )
                )
            except NoEligibleWorkerError:
                pass  # fall through: reusing a planner worker is acceptable, never an absolute block
        return await self._worker_selector.select(
            WorkerSelectionRequest(required_capabilities=frozenset({self._synthesis_capability}))
        )

    async def synthesize(self, planning_session_id: str) -> RoadmapProposal:
        """Synthesizes every VALID PlannerProposal into one RoadmapProposal.

        Refuses (fails the session) unless exactly ``planner_count`` VALID
        proposals exist — never synthesizes a partial set as if it were
        the full multi-agent comparison.
        """
        session = self._planning_store.get_session(planning_session_id)
        proposals = self._planning_store.list_planner_proposals(planning_session_id)
        valid = sorted(
            (p for p in proposals if p.status is PlannerProposalStatus.VALID), key=lambda p: p.worker_id
        )

        if len(valid) < session.policy_planner_count:
            reason = f"only {len(valid)}/{session.policy_planner_count} planners produced a valid proposal"
            self._planning_store.update_session_status(
                planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason
            )
            raise PlanningSessionFailedError(planning_session_id, reason)

        self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.SYNTHESIZING)
        snapshot = self._planning_store.get_snapshot(session.snapshot_id)
        synthesizer = await self._select_synthesizer(planner_worker_ids={p.worker_id for p in valid})

        execution_id = self._id_factory()
        project = self._project_state_store.get_project(session.project_id)
        request = ExecutionRequest(
            execution_id=execution_id, task_id=f"planning-synthesis:{planning_session_id}",
            worker=synthesizer, role=SYNTHESIZER_ROLE, workspace=project.workspace,
            instructions=_build_synthesizer_instructions(snapshot, valid),
            initial_event_topic=SYNTHESIS_INITIAL_TOPIC,
            success_topics=frozenset({SYNTHESIS_PROPOSED_TOPIC}),
            failure_topics=frozenset({SYNTHESIS_FAILED_TOPIC}),
            timeout_seconds=self._timeout_seconds,
        )

        try:
            result = await self._execution_engine.execute(request)
        except RalphExecutionEngineError as exc:
            reason = f"synthesizer execution failed to run: {exc}"
            self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason)
            raise PlanningSessionFailedError(planning_session_id, reason) from exc

        if result.record.status is not ExecutionStatus.SUCCEEDED:
            reason = f"no reliable {SYNTHESIS_PROPOSED_TOPIC} event (status={result.record.status.value})"
            self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason)
            raise PlanningSessionFailedError(planning_session_id, reason)

        event = next((e for e in result.events if e.topic == SYNTHESIS_PROPOSED_TOPIC), None)
        if event is None or not event.payload:
            reason = "missing synthesis.proposed payload"
            self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason)
            raise PlanningSessionFailedError(planning_session_id, reason)

        try:
            data = json.loads(event.payload)
            parsed = _parse_synthesizer_payload(data)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            reason = f"invalid synthesis.proposed payload: {exc}"
            self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.FAILED, failure_reason=reason)
            raise PlanningSessionFailedError(planning_session_id, reason) from exc

        roadmap_proposal = RoadmapProposal(
            roadmap_proposal_id=self._id_factory(), planning_session_id=planning_session_id,
            snapshot_id=session.snapshot_id, synthesizer_worker_id=synthesizer.worker_id,
            synthesizer_execution_id=execution_id, created_at=self._clock(),
            source_proposal_ids=tuple(sorted(p.proposal_id for p in valid)),
            status=RoadmapProposalStatus.PROPOSED, **parsed,
        )
        self._planning_store.record_roadmap_proposal(roadmap_proposal)
        self._planning_store.update_session_status(planning_session_id, PlanningSessionStatus.PROPOSAL_READY)
        return roadmap_proposal

    async def run_full_session(self, *, project_id: str, mvp_id: str, release_id: str) -> RoadmapProposal:
        """Convenience: start a session, run all planners, then synthesize — in one call."""
        session = await self.start_planning_session(project_id=project_id, mvp_id=mvp_id, release_id=release_id)
        await self.run_planners(session.planning_session_id)
        return await self.synthesize(session.planning_session_id)
