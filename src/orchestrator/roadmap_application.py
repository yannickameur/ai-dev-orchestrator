"""Apply a decided RoadmapProposal + create/start the next MVP (Slice 14).

Closes the autonomous loop between two releases: once Slice 13 records a
terminal decision (``APPROVED``/``AUTO_APPROVED``) on a Slice 12
``RoadmapProposal``, this module is the only place that turns that
decision into real state — a rewritten ``ROADMAP.md``, a real ``MVP`` in
``ProjectStateStore``, and its real ``WorkItem``s — and, optionally, a
single controlled call into the existing ``MVPManager`` to start the first
eligible WorkItem of that new MVP.

FAIL-CLOSED ON THE DECISION: only ``ApprovalStatus.APPROVED`` and
``ApprovalStatus.AUTO_APPROVED`` ever authorize application.
``AWAITING_APPROVAL``, ``REJECTED``, and ``MODIFY_REQUESTED`` (or no
approval window at all) leave absolutely no trace here — no
``RoadmapApplication`` row, no file write, no MVP, nothing —
``apply_decided_proposal`` simply raises ``DecisionNotAuthorizedError``.
``AUTO_APPROVED`` carries exactly the same authority as ``APPROVED``: this
module never asks for a second, human confirmation of an optimistic
approval — that would silently reintroduce the blocking gate Slice 13
was built to remove.

FAIL-CLOSED ON THE ROADMAP HASH: Slice 12 captured ``roadmap_hash`` — the
sha256 of ``ROADMAP.md`` at the moment the ``PlanningSnapshot`` was built.
Before ever touching anything, this module re-reads ``ROADMAP.md`` and
recomputes that hash. A mismatch means the file changed since the
proposal was drafted (a human edit, a concurrent application) — the
proposal is NEVER applied against a roadmap it was not actually written
against. Instead a ``RoadmapApplication`` is persisted with
``status=CONFLICT`` (an explicit, audited "STALE_PROPOSAL" result) and
``RoadmapConflictError`` is raised. Nothing is ever silently overwritten.

DETERMINISTIC, NO LLM: the ``RoadmapProposal`` (planners + synthesizer,
Slice 12) and the approval decision (Slice 13) already encode every
judgment call. Turning KEEP/ADD/MOVE/DROP into new ``ROADMAP.md`` bytes,
and a proposed MVP into real ``WorkItem``s, is a pure, deterministic
transformation — this module never launches a worker, never calls
Ralph/Claude/Codex, and never asks an LLM to rewrite the roadmap file.
The only place a worker is ever invoked is the final, optional,
already-existing ``MVPManager.run_next_work_item`` call to start the
first WorkItem of the new MVP — this module never reimplements any part
of ``MVPManager``.

ROADMAP.md IS A HUMAN DOCUMENT: this module never rewrites the file
wholesale and never touches prose written by a human. It owns exactly one
clearly delimited, generated section (between ``_MANAGED_BEGIN``/
``_MANAGED_END`` markers, appended once at the end of the file the first
time this module ever runs) and regenerates *only* that section's content
— in full, from the durable ``RoadmapApplicationStore`` — on every
application. Regenerating (rather than string-splicing) that one section
is what makes its content byte-for-byte deterministic regardless of
incidental whitespace history; everything outside the markers is
preserved verbatim, forever.

IDEMPOTENT, RESTART-SAFE APPLICATION: a ``RoadmapApplication`` row is the
durable identity of one application *attempt*. ``target_mvp_id`` and the
title -> real-``work_item_id`` mapping are decided once, up front, and
persisted in that very row *before* any mutation happens — so a
reconciliation after a crash never has to re-derive them from a fresh
(and non-reproducible) id factory. A proposal already ``APPLIED`` is
never re-applied (a second ``apply_decided_proposal`` call for the same
``roadmap_proposal_id`` returns the existing outcome unchanged). An
orphaned ``APPLYING`` row is NEVER blindly replayed — ``reconcile()`` must
be called explicitly, and decides the real outcome by comparing the
current ``ROADMAP.md`` hash against the ``roadmap_hash_before``/
``roadmap_hash_after`` this module already knew, then checking whether
the target MVP/WorkItems already exist.

GIT IS OUT OF SCOPE: this module never runs `git add`/`commit`/`push`. It
only ever reads a git SHA if one happens to already be available (for
audit), and only ever touches the working tree's ``ROADMAP.md`` file.
Git/PR/merge governance is Slice 15.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from orchestrator.approval import ApprovalStatus, ApprovalStore
from orchestrator.planning import (
    ROADMAP_FILENAME,
    PlanningSession,
    PlanningStore,
    ProposedWorkItem,
    RoadmapProposal,
    render_markdown,
)
from orchestrator.project_state import (
    MVP,
    DuplicateMVPError,
    DuplicateWorkItemError,
    ProjectStateStore,
    WorkItem,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any runtime coupling
    from orchestrator.mvp_manager import MVPManager, WorkItemRunResult

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

_MANAGED_BEGIN = "<!-- orchestrator:roadmap-applications:begin -->"
_MANAGED_END = "<!-- orchestrator:roadmap-applications:end -->"
_MANAGED_HEADING = (
    "## Historique des applications automatiques "
    "(Slice 14 — généré par l'orchestrateur, ne pas éditer à la main)"
)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RoadmapApplicationStatus(str, Enum):
    APPLYING = "applying"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"


_TERMINAL_APPLICATION_STATUSES = (
    RoadmapApplicationStatus.APPLIED,
    RoadmapApplicationStatus.CONFLICT,
    RoadmapApplicationStatus.FAILED,
)


@dataclass(frozen=True, slots=True)
class RoadmapApplication:
    """One application attempt of a ``RoadmapProposal`` — never deleted,
    never overwritten in place except through the narrow, validated
    ``mark_applied``/``mark_conflict``/``mark_failed`` transitions."""

    application_id: str
    roadmap_proposal_id: str
    approval_id: str
    planning_session_id: str
    project_id: str
    source_mvp_id: str
    source_release_id: str
    decision_status: str
    decided_at: datetime
    created_at: datetime
    status: RoadmapApplicationStatus
    roadmap_hash_before: str
    target_mvp_id: str | None = None
    work_item_map: tuple[tuple[str, str], ...] = ()
    roadmap_hash_after: str | None = None
    finished_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "application_id", "roadmap_proposal_id", "approval_id", "planning_session_id",
            "project_id", "source_mvp_id", "source_release_id", "decision_status",
            "roadmap_hash_before",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"RoadmapApplication.{name}")
        if not isinstance(self.status, RoadmapApplicationStatus):
            raise TypeError(f"RoadmapApplication.status must be a RoadmapApplicationStatus, got {type(self.status)!r}")
        _require_aware(self.decided_at, field_name="RoadmapApplication.decided_at")
        _require_aware(self.created_at, field_name="RoadmapApplication.created_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, field_name="RoadmapApplication.finished_at")
        if self.target_mvp_id is not None:
            _require_non_empty_str(self.target_mvp_id, field_name="RoadmapApplication.target_mvp_id")
        if self.roadmap_hash_after is not None:
            _require_non_empty_str(self.roadmap_hash_after, field_name="RoadmapApplication.roadmap_hash_after")
        if self.error is not None:
            _require_non_empty_str(self.error, field_name="RoadmapApplication.error")
        object.__setattr__(self, "work_item_map", tuple((t, i) for t, i in self.work_item_map))


class RoadmapApplicationStoreError(Exception):
    """Base for RoadmapApplicationStore domain errors."""


class UnknownRoadmapApplicationError(RoadmapApplicationStoreError):
    def __init__(self, application_id: str) -> None:
        super().__init__(f"unknown roadmap application: {application_id!r}")
        self.application_id = application_id


class DuplicateRoadmapApplicationError(RoadmapApplicationStoreError):
    def __init__(self, application_id: str) -> None:
        super().__init__(f"roadmap application already exists: {application_id!r}")
        self.application_id = application_id


class InvalidRoadmapApplicationTransitionError(RoadmapApplicationStoreError):
    def __init__(self, application_id: str, current: RoadmapApplicationStatus) -> None:
        super().__init__(
            f"roadmap application {application_id!r} is already {current.value!r}, "
            "cannot transition again"
        )
        self.application_id = application_id
        self.current = current


class CorruptRoadmapApplicationError(RoadmapApplicationStoreError):
    def __init__(self, application_id: str, detail: str) -> None:
        super().__init__(f"corrupt roadmap application {application_id!r}: {detail}")
        self.application_id = application_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS roadmap_applications (
    application_id TEXT PRIMARY KEY,
    roadmap_proposal_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    planning_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_mvp_id TEXT NOT NULL,
    source_release_id TEXT NOT NULL,
    target_mvp_id TEXT,
    decision_status TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    work_item_map TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    roadmap_hash_before TEXT NOT NULL,
    roadmap_hash_after TEXT,
    error TEXT
)
"""


def _decode_row(row: sqlite3.Row) -> RoadmapApplication:
    application_id = row["application_id"]
    try:
        return RoadmapApplication(
            application_id=application_id, roadmap_proposal_id=row["roadmap_proposal_id"],
            approval_id=row["approval_id"], planning_session_id=row["planning_session_id"],
            project_id=row["project_id"], source_mvp_id=row["source_mvp_id"],
            source_release_id=row["source_release_id"], target_mvp_id=row["target_mvp_id"],
            decision_status=row["decision_status"], decided_at=datetime.fromisoformat(row["decided_at"]),
            work_item_map=tuple(tuple(pair) for pair in json.loads(row["work_item_map"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            status=RoadmapApplicationStatus(row["status"]), roadmap_hash_before=row["roadmap_hash_before"],
            roadmap_hash_after=row["roadmap_hash_after"], error=row["error"],
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CorruptRoadmapApplicationError(application_id, str(exc)) from exc


class RoadmapApplicationStore:
    """Synchronous, sqlite3-backed store for durable RoadmapApplications."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def create(self, application: RoadmapApplication) -> RoadmapApplication:
        """Persists a fully-formed ``RoadmapApplication`` as-is — the caller
        (``RoadmapApplicationService``) builds the object first (so the
        exact same instance can be used to render the roadmap entry before
        it is ever persisted), this only inserts it verbatim."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO roadmap_applications (application_id, roadmap_proposal_id, "
                    "approval_id, planning_session_id, project_id, source_mvp_id, "
                    "source_release_id, target_mvp_id, decision_status, decided_at, "
                    "work_item_map, created_at, finished_at, status, roadmap_hash_before, "
                    "roadmap_hash_after, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        application.application_id, application.roadmap_proposal_id,
                        application.approval_id, application.planning_session_id,
                        application.project_id, application.source_mvp_id,
                        application.source_release_id, application.target_mvp_id,
                        application.decision_status, application.decided_at.isoformat(),
                        json.dumps(list(application.work_item_map)), application.created_at.isoformat(),
                        application.finished_at.isoformat() if application.finished_at else None,
                        application.status.value, application.roadmap_hash_before,
                        application.roadmap_hash_after, application.error,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRoadmapApplicationError(application.application_id) from exc
        return application

    def get(self, application_id: str) -> RoadmapApplication:
        row = self._conn.execute(
            "SELECT * FROM roadmap_applications WHERE application_id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise UnknownRoadmapApplicationError(application_id)
        return _decode_row(row)

    def list_for_proposal(self, roadmap_proposal_id: str) -> list[RoadmapApplication]:
        """Every attempt ever made for a proposal, oldest first — none ever overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM roadmap_applications WHERE roadmap_proposal_id = ? ORDER BY created_at ASC",
            (roadmap_proposal_id,),
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def latest_for_proposal(self, roadmap_proposal_id: str) -> RoadmapApplication | None:
        attempts = self.list_for_proposal(roadmap_proposal_id)
        return attempts[-1] if attempts else None

    def list_applied_for_project(self, project_id: str) -> list[RoadmapApplication]:
        """Every successfully-APPLIED application for a project, oldest
        first — the exact, deterministic source of truth the managed
        ROADMAP.md section is regenerated from on every application."""
        rows = self._conn.execute(
            "SELECT * FROM roadmap_applications WHERE project_id = ? AND status = ? "
            "ORDER BY created_at ASC",
            (project_id, RoadmapApplicationStatus.APPLIED.value),
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def _finish(
        self, application_id: str, *, status: RoadmapApplicationStatus,
        finished_at: datetime, error: str | None,
    ) -> RoadmapApplication:
        current = self.get(application_id)
        if current.status is not RoadmapApplicationStatus.APPLYING:
            raise InvalidRoadmapApplicationTransitionError(application_id, current.status)
        with self._conn:
            self._conn.execute(
                "UPDATE roadmap_applications SET status = ?, finished_at = ?, error = ? "
                "WHERE application_id = ?",
                (status.value, finished_at.isoformat(), error, application_id),
            )
        return self.get(application_id)

    def mark_applied(self, application_id: str, *, finished_at: datetime) -> RoadmapApplication:
        return self._finish(
            application_id, status=RoadmapApplicationStatus.APPLIED, finished_at=finished_at, error=None
        )

    def mark_conflict(self, application_id: str, *, error: str, finished_at: datetime) -> RoadmapApplication:
        return self._finish(
            application_id, status=RoadmapApplicationStatus.CONFLICT, finished_at=finished_at, error=error
        )

    def mark_failed(self, application_id: str, *, error: str, finished_at: datetime) -> RoadmapApplication:
        return self._finish(
            application_id, status=RoadmapApplicationStatus.FAILED, finished_at=finished_at, error=error
        )


# --- roadmap dependency validation (fail-closed, before any mutation) -------


class InvalidRoadmapProposalError(ValueError):
    """A structurally invalid proposed-MVP work item graph — never
    partially applied."""


def _validate_proposed_work_items(items: Sequence[ProposedWorkItem]) -> None:
    titles = [item.title for item in items]
    seen = set()
    duplicates = sorted({t for t in titles if t in seen or seen.add(t)})
    if duplicates:
        raise InvalidRoadmapProposalError(f"duplicate proposed work item titles: {duplicates!r}")

    title_set = set(titles)
    for item in items:
        unknown = sorted(d for d in item.dependencies if d not in title_set)
        if unknown:
            raise InvalidRoadmapProposalError(
                f"proposed work item {item.title!r} depends on unknown title(s): {unknown!r}"
            )

    graph = {item.title: item.dependencies for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(title: str) -> None:
        if title in visited:
            return
        if title in visiting:
            raise InvalidRoadmapProposalError(f"dependency cycle detected involving {title!r}")
        visiting.add(title)
        for dep in graph[title]:
            _visit(dep)
        visiting.discard(title)
        visited.add(title)

    for title in titles:
        _visit(title)


# --- deterministic ROADMAP.md rendering (pure, no LLM) ----------------------


def _render_application_entry(application: RoadmapApplication, proposal: RoadmapProposal) -> str:
    work_items_block = (
        "\n".join(f"  - `{work_item_id}` — {title}" for title, work_item_id in application.work_item_map)
        or "  (none)"
    )
    diff_md = render_markdown(proposal).rstrip("\n")
    lines = [
        f"### Application `{application.application_id}` — MVP `{application.target_mvp_id}` "
        f"(proposal `{application.roadmap_proposal_id}`, {application.decision_status} "
        f"on {application.decided_at.isoformat()})",
        "",
        f"- Source release: `{application.source_release_id}` (from MVP `{application.source_mvp_id}`)",
        "- WorkItems created:",
        work_items_block,
        "",
        diff_md,
        "",
    ]
    return "\n".join(lines)


def _split_managed_section(content: str) -> tuple[str, str]:
    """Returns (prefix, suffix) — everything outside the managed markers,
    preserved verbatim. The managed content itself is never read back from
    the file: it is always regenerated in full from the durable store."""
    begin_idx = content.find(_MANAGED_BEGIN)
    end_idx = content.find(_MANAGED_END)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        prefix = content if content.endswith("\n") or content == "" else content + "\n"
        return prefix, ""
    prefix = content[:begin_idx]
    suffix = content[end_idx + len(_MANAGED_END):]
    if suffix.startswith("\n"):
        suffix = suffix[1:]
    return prefix, suffix


def render_roadmap_content(
    current_content: str, applied_entries: Sequence[tuple[RoadmapApplication, RoadmapProposal]]
) -> str:
    """Pure, deterministic: rebuilds ``ROADMAP.md`` by preserving everything
    outside the managed markers verbatim and regenerating the managed
    section in full from ``applied_entries`` (already-APPLIED applications,
    oldest first, plus — when preparing a new one — the in-flight
    candidate appended last). Never depends on the managed section's
    previous on-disk text, only on durable, already-persisted facts."""
    prefix, suffix = _split_managed_section(current_content)
    body = "\n\n".join(_render_application_entry(app, proposal) for app, proposal in applied_entries)
    managed = f"{_MANAGED_HEADING}\n\n{body}\n" if body else f"{_MANAGED_HEADING}\n"
    return f"{prefix}\n{_MANAGED_BEGIN}\n{managed}{_MANAGED_END}\n{suffix}"


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- service-level errors ----------------------------------------------------


class RoadmapApplicationServiceError(Exception):
    """Base for RoadmapApplicationService domain errors."""


class DecisionNotAuthorizedError(RoadmapApplicationServiceError):
    """Raised for anything other than APPROVED/AUTO_APPROVED — including no
    approval window at all. No RoadmapApplication row is ever created."""

    def __init__(self, roadmap_proposal_id: str, status: ApprovalStatus | None) -> None:
        detail = status.value if status is not None else "no approval window recorded"
        super().__init__(
            f"roadmap proposal {roadmap_proposal_id!r} is not authorized for application: {detail}"
        )
        self.roadmap_proposal_id = roadmap_proposal_id
        self.status = status


class RoadmapApplicationInProgressError(RoadmapApplicationServiceError):
    """An orphaned APPLYING attempt exists — must be resolved via
    ``RoadmapApplicationService.reconcile()``, never blindly replayed."""

    def __init__(self, application_id: str) -> None:
        super().__init__(
            f"roadmap application {application_id!r} is still APPLYING — call reconcile() "
            "explicitly, it is never blindly retried"
        )
        self.application_id = application_id


class RoadmapConflictError(RoadmapApplicationServiceError):
    """STALE_PROPOSAL: ROADMAP.md changed since the PlanningSnapshot this
    proposal was built from was captured. Carries the persisted CONFLICT
    ``RoadmapApplication`` record — nothing was ever applied."""

    def __init__(self, application: RoadmapApplication) -> None:
        super().__init__(f"STALE_PROPOSAL: {application.error}")
        self.application = application


class RoadmapApplicationFailedError(RoadmapApplicationServiceError):
    """The proposal itself is structurally invalid (bad dependency graph).
    Carries the persisted FAILED ``RoadmapApplication`` record — nothing
    was ever applied."""

    def __init__(self, application: RoadmapApplication) -> None:
        super().__init__(str(application.error))
        self.application = application


@dataclass(frozen=True, slots=True)
class RoadmapApplicationOutcome:
    """What a caller gets back from a successful (or already-applied)
    ``apply_decided_proposal``/``reconcile`` call."""

    application: RoadmapApplication
    mvp: MVP
    work_items: tuple[WorkItem, ...]
    run_result: "WorkItemRunResult | None" = None


class RoadmapApplicationService:
    """Turns a decided ``RoadmapProposal`` into real roadmap/MVP/WorkItem state.

    Composes existing services only — never reimplements ``MVPManager``,
    never launches a worker itself, never runs git. See module docstring
    for the full set of invariants (fail-closed decision/hash checks,
    idempotence, restart-safe reconciliation, deterministic rendering).
    """

    def __init__(
        self,
        application_store: RoadmapApplicationStore,
        planning_store: PlanningStore,
        approval_store: ApprovalStore,
        project_state_store: ProjectStateStore,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._application_store = application_store
        self._planning_store = planning_store
        self._approval_store = approval_store
        self._project_state_store = project_state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory

    async def apply_decided_proposal(
        self, roadmap_proposal_id: str, *, mvp_manager: "MVPManager | None" = None
    ) -> RoadmapApplicationOutcome:
        """Applies ``roadmap_proposal_id`` if — and only if — it carries an
        authorized terminal decision. Idempotent: a proposal already
        APPLIED is returned unchanged, never re-applied."""
        proposal = self._planning_store.get_roadmap_proposal(roadmap_proposal_id)
        approval = self._approval_store.get_for_proposal(roadmap_proposal_id)
        if approval is None or approval.status not in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED):
            raise DecisionNotAuthorizedError(
                roadmap_proposal_id, approval.status if approval is not None else None
            )

        existing = self._application_store.latest_for_proposal(roadmap_proposal_id)
        if existing is not None:
            if existing.status is RoadmapApplicationStatus.APPLIED:
                return self._outcome_for(existing, mvp_manager=None)
            if existing.status is RoadmapApplicationStatus.APPLYING:
                raise RoadmapApplicationInProgressError(existing.application_id)
            # CONFLICT/FAILED: a brand-new attempt is allowed (never
            # overwritten — every attempt is kept, exactly like PlanningSession).

        session = self._planning_store.get_session(proposal.planning_session_id)
        project = self._project_state_store.get_project(session.project_id)
        roadmap_path = project.workspace / ROADMAP_FILENAME
        current_content = roadmap_path.read_text()
        hash_before = _sha256(current_content)
        snapshot = self._planning_store.get_snapshot(proposal.snapshot_id)

        application_id = self._id_factory()
        now = self._clock()

        if hash_before != snapshot.roadmap_hash:
            conflict = RoadmapApplication(
                application_id=application_id, roadmap_proposal_id=roadmap_proposal_id,
                approval_id=approval.approval_id, planning_session_id=session.planning_session_id,
                project_id=session.project_id, source_mvp_id=session.mvp_id,
                source_release_id=session.release_id, decision_status=approval.status.value,
                decided_at=approval.decided_at, created_at=now, finished_at=now,
                status=RoadmapApplicationStatus.CONFLICT, roadmap_hash_before=hash_before,
                error=(
                    "STALE_PROPOSAL: ROADMAP.md changed since this proposal's snapshot was "
                    f"captured (expected sha256={snapshot.roadmap_hash}, found sha256={hash_before})"
                ),
            )
            self._application_store.create(conflict)
            raise RoadmapConflictError(conflict)

        try:
            _validate_proposed_work_items(proposal.next_mvp_work_items)
        except InvalidRoadmapProposalError as exc:
            failed = RoadmapApplication(
                application_id=application_id, roadmap_proposal_id=roadmap_proposal_id,
                approval_id=approval.approval_id, planning_session_id=session.planning_session_id,
                project_id=session.project_id, source_mvp_id=session.mvp_id,
                source_release_id=session.release_id, decision_status=approval.status.value,
                decided_at=approval.decided_at, created_at=now, finished_at=now,
                status=RoadmapApplicationStatus.FAILED, roadmap_hash_before=hash_before, error=str(exc),
            )
            self._application_store.create(failed)
            raise RoadmapApplicationFailedError(failed) from exc

        target_mvp_id = self._id_factory()
        work_item_map = tuple((item.title, self._id_factory()) for item in proposal.next_mvp_work_items)

        applying = RoadmapApplication(
            application_id=application_id, roadmap_proposal_id=roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id=session.planning_session_id,
            project_id=session.project_id, source_mvp_id=session.mvp_id,
            source_release_id=session.release_id, target_mvp_id=target_mvp_id,
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=work_item_map, created_at=now, status=RoadmapApplicationStatus.APPLYING,
            roadmap_hash_before=hash_before,
        )
        new_content = self._build_new_content(current_content, project_id=session.project_id, pending=applying, pending_proposal=proposal)
        applying = replace(applying, roadmap_hash_after=_sha256(new_content))
        self._application_store.create(applying)

        _atomic_write(roadmap_path, new_content)
        mvp, work_items = self._create_mvp_and_work_items(
            project_id=session.project_id, target_mvp_id=target_mvp_id,
            proposal=proposal, work_item_map=work_item_map,
        )
        finished = self._application_store.mark_applied(application_id, finished_at=self._clock())

        return await self._outcome_for_async(finished, mvp_manager=mvp_manager, mvp=mvp, work_items=work_items)

    def reconcile(self, application_id: str) -> RoadmapApplication:
        """Resolves an orphaned APPLYING attempt after a restart.

        Never blindly replays: inspects the current ``ROADMAP.md`` hash
        against what this exact attempt already knew
        (``roadmap_hash_before``/``roadmap_hash_after``) to determine
        whether the file write happened before the crash, then ensures
        the target MVP/WorkItems exist (idempotently) before marking
        APPLIED — or gives up to CONFLICT if the file was changed by
        something else in between.
        """
        application = self._application_store.get(application_id)
        if application.status is not RoadmapApplicationStatus.APPLYING:
            return application  # already terminal — nothing to reconcile

        project = self._project_state_store.get_project(application.project_id)
        roadmap_path = project.workspace / ROADMAP_FILENAME
        current_content = roadmap_path.read_text()
        current_hash = _sha256(current_content)

        if current_hash == application.roadmap_hash_before:
            proposal = self._planning_store.get_roadmap_proposal(application.roadmap_proposal_id)
            new_content = self._build_new_content(
                current_content, project_id=application.project_id, pending=application, pending_proposal=proposal
            )
            _atomic_write(roadmap_path, new_content)
        elif current_hash != application.roadmap_hash_after:
            return self._application_store.mark_conflict(
                application_id,
                error=(
                    "ROADMAP.md was modified by something else while this application was "
                    "in flight — cannot be safely resumed automatically"
                ),
                finished_at=self._clock(),
            )
        # else: current_hash already matches roadmap_hash_after — the
        # write already happened before the crash, nothing left to do
        # for the file itself.

        proposal = self._planning_store.get_roadmap_proposal(application.roadmap_proposal_id)
        self._create_mvp_and_work_items(
            project_id=application.project_id, target_mvp_id=application.target_mvp_id,
            proposal=proposal, work_item_map=application.work_item_map,
        )
        return self._application_store.mark_applied(application_id, finished_at=self._clock())

    # --- internals -----------------------------------------------------

    def _build_new_content(
        self, current_content: str, *, project_id: str, pending: RoadmapApplication, pending_proposal: RoadmapProposal
    ) -> str:
        applied = self._application_store.list_applied_for_project(project_id)
        entries: list[tuple[RoadmapApplication, RoadmapProposal]] = [
            (app, self._planning_store.get_roadmap_proposal(app.roadmap_proposal_id)) for app in applied
        ]
        entries.append((pending, pending_proposal))
        return render_roadmap_content(current_content, entries)

    def _create_mvp_and_work_items(
        self, *, project_id: str, target_mvp_id: str, proposal: RoadmapProposal,
        work_item_map: Sequence[tuple[str, str]],
    ) -> tuple[MVP, tuple[WorkItem, ...]]:
        try:
            mvp = self._project_state_store.create_mvp(
                mvp_id=target_mvp_id, project_id=project_id, objective=proposal.next_mvp_objective,
                acceptance_criteria=proposal.next_mvp_acceptance_criteria,
            )
        except DuplicateMVPError:
            mvp = self._project_state_store.get_mvp(target_mvp_id)
        self._project_state_store.set_current_mvp(project_id, target_mvp_id)
        mvp = self._project_state_store.mark_mvp_running(target_mvp_id)

        title_to_id = dict(work_item_map)
        work_items: list[WorkItem] = []
        for item in proposal.next_mvp_work_items:
            work_item_id = title_to_id[item.title]
            dependencies = tuple(sorted(title_to_id[dep] for dep in item.dependencies))
            try:
                work_item = self._project_state_store.create_work_item(
                    work_item_id=work_item_id, mvp_id=target_mvp_id, title=item.title,
                    required_capabilities=item.required_capabilities, dependencies=dependencies,
                    acceptance_criteria=item.acceptance_criteria,
                )
            except DuplicateWorkItemError:
                work_item = self._project_state_store.get_work_item(work_item_id)
            work_items.append(work_item)
        return mvp, tuple(work_items)

    def _outcome_for(
        self, application: RoadmapApplication, *, mvp_manager: "MVPManager | None"
    ) -> RoadmapApplicationOutcome:
        assert application.target_mvp_id is not None
        mvp = self._project_state_store.get_mvp(application.target_mvp_id)
        work_items = tuple(self._project_state_store.list_work_items(application.target_mvp_id))
        return RoadmapApplicationOutcome(application=application, mvp=mvp, work_items=work_items, run_result=None)

    async def _outcome_for_async(
        self, application: RoadmapApplication, *, mvp_manager: "MVPManager | None",
        mvp: MVP, work_items: tuple[WorkItem, ...],
    ) -> RoadmapApplicationOutcome:
        run_result = None
        if mvp_manager is not None:
            run_result = await mvp_manager.run_next_work_item(application.target_mvp_id)
        return RoadmapApplicationOutcome(application=application, mvp=mvp, work_items=work_items, run_result=run_result)
