"""Project/MVP/WorkItem orchestration state — the durable core of Slice 7.

This module holds the *high-level* orchestration state a
:class:`~orchestrator.mvp_manager.MVPManager` operates on: a
:class:`Project` (workspace), its :class:`MVP` (objective, acceptance
criteria), and the :class:`WorkItem` s that make it up. It is deliberately
separate from :mod:`~orchestrator.execution_store`, which remains the sole
source of truth for machine executions — this module never records
execution identity/audit facts itself, only which WorkItem is at which
stage.

Design invariants:

- This is not a second, fine-grained task queue: a `WorkItem` is a
  high-level unit of work delegated whole to Ralph (via
  `RalphExecutionEngine`, already its own orchestration engine once a
  WorkItem is delegated) — never a scheduler competing with Ralph's own.
- `WorkItem.dependencies` reference other `WorkItem.work_item_id`s within
  the same MVP; a WorkItem only becomes eligible (`READY`) once every
  dependency is `COMPLETED`. An unknown dependency, a dependency that is
  itself `FAILED`/`BLOCKED`, or a dependency cycle all resolve to `BLOCKED`
  — never a silent, permanently-`PLANNED` deadlock.
- Status transitions are explicit, minimal, and validated (see
  `_WORK_ITEM_TRANSITIONS`/`_MVP_TRANSITIONS`): a terminal WorkItem/MVP
  status is never silently reopened.
- No blind retry, no automatic re-run of anything: this module only
  records state, it never launches a worker or a Ralph execution itself
  (that belongs to `MVPManager`, which composes this store with
  `WorkerSelector`/`RalphExecutionEngine`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

Clock = Callable[[], datetime]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _as_frozenset_of_str(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    result = frozenset(values)
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return result


def _as_tuple_of_str(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return result


class WorkItemStatus(str, Enum):
    """Minimal WorkItem lifecycle.

    PLANNED/READY/RUNNING/NEEDS_REWORK/WAITING are non-terminal.
    NEEDS_REWORK means QA (LEAN_FEATURE_FLOW's deterministic gate) failed
    with attempts remaining — it is directly eligible for a new
    development execution (no dependency re-check needed, since the
    WorkItem was already READY once).

    REVIEWING existed historically for the independent read-only Reviewer
    phase of the now-removed ``GOVERNED_FULL`` pipeline (see ROADMAP.md's
    dated removal entry) and is kept in this enum ONLY so a value
    persisted by that old pipeline still decodes correctly from an
    existing SQLite store — no current code path ever transitions a
    WorkItem into REVIEWING.

    WAITING was added in Slice 11: an orchestration decision (never a
    provider fact) meaning no eligible worker could be selected right now
    but a plausible next retry moment is known (see
    ``orchestrator.wait.WaitRecord``, which also records *which* phase —
    development or rework — was interrupted). A WAITING WorkItem is
    neither COMPLETED nor FAILED: its dependents never become READY
    (``refresh_readiness`` only promotes on COMPLETED dependencies).
    Resuming always re-enters READY/NEEDS_REWORK — the exact state it was
    waiting to re-attempt — never resumes "in place".

    RECOVERY_REQUIRED was also added in Slice 11, and is deliberately
    distinct from WAITING: WAITING means "no eligible worker exists, but a
    reset deadline is known" (an external, quota-driven wait);
    RECOVERY_REQUIRED means "the *execution itself* never reached a
    reliable terminal outcome" — a RUNNING execution found orphaned after
    a restart, or one that came back INTERRUPTED — with no deadline
    involved at all (see ``orchestrator.recovery.RecoveryCoordinator``,
    which also ensures a durable recovery handoff exists before this
    transition). It is immediately, unconditionally re-orchestrable (no
    "eligible_at" to wait out): resuming re-enters RUNNING — always via a
    **new** execution, never the interrupted/orphaned one.
    """

    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    #: Historical/decode-only — see class docstring. Never entered by
    #: current code.
    REVIEWING = "reviewing"
    NEEDS_REWORK = "needs_rework"
    WAITING = "waiting"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MVPStatus(str, Enum):
    """Minimal MVP lifecycle. Only PLANNED->RUNNING is exercised in Slice 7;
    VALIDATING/RELEASED are reserved for the future release gate (Slice 10).
    """

    PLANNED = "planned"
    RUNNING = "running"
    VALIDATING = "validating"
    RELEASED = "released"
    FAILED = "failed"
    BLOCKED = "blocked"


_WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.PLANNED: frozenset({WorkItemStatus.READY, WorkItemStatus.BLOCKED}),
    WorkItemStatus.READY: frozenset({WorkItemStatus.RUNNING, WorkItemStatus.WAITING}),
    WorkItemStatus.RUNNING: frozenset(
        {
            WorkItemStatus.COMPLETED,
            WorkItemStatus.FAILED,
            WorkItemStatus.REVIEWING,
            WorkItemStatus.RECOVERY_REQUIRED,
            WorkItemStatus.WAITING,
            WorkItemStatus.NEEDS_REWORK,
            WorkItemStatus.BLOCKED,
        }
    ),
    # RUNNING -> REVIEWING and everything below that still mentions
    # REVIEWING exist only so a WorkItem already sitting in REVIEWING in
    # an existing SQLite store (from the removed GOVERNED_FULL pipeline —
    # see class docstring) can still be transitioned out of it correctly.
    # No current code path enters REVIEWING.
    WorkItemStatus.REVIEWING: frozenset(
        {
            WorkItemStatus.COMPLETED,
            WorkItemStatus.NEEDS_REWORK,
            WorkItemStatus.BLOCKED,
            WorkItemStatus.WAITING,
            WorkItemStatus.RECOVERY_REQUIRED,
        }
    ),
    WorkItemStatus.NEEDS_REWORK: frozenset({WorkItemStatus.RUNNING, WorkItemStatus.WAITING}),
    # WAITING only ever resumes into the exact state it was waiting to
    # re-attempt (READY/NEEDS_REWORK for a new development-side selection,
    # RUNNING for LEAN_FEATURE_FLOW's own DEV B review) or gives up to
    # BLOCKED when no reliable retry moment remains — never "in place".
    WorkItemStatus.WAITING: frozenset(
        {
            WorkItemStatus.READY, WorkItemStatus.NEEDS_REWORK, WorkItemStatus.REVIEWING,
            WorkItemStatus.RUNNING, WorkItemStatus.BLOCKED,
        }
    ),
    # RECOVERY_REQUIRED has no deadline to wait out (unlike WAITING): it is
    # immediately re-orchestrable, always via a *new* execution — resuming
    # always re-enters RUNNING (see RecoveryCoordinator). BLOCKED is only
    # a defensive escape hatch for an invariant violation (e.g. no
    # recovery handoff found), never the expected path.
    WorkItemStatus.RECOVERY_REQUIRED: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.REVIEWING, WorkItemStatus.BLOCKED}
    ),
    WorkItemStatus.COMPLETED: frozenset(),
    WorkItemStatus.FAILED: frozenset(),
    WorkItemStatus.BLOCKED: frozenset(),
}

_MVP_TRANSITIONS: dict[MVPStatus, frozenset[MVPStatus]] = {
    MVPStatus.PLANNED: frozenset({MVPStatus.RUNNING}),
    MVPStatus.RUNNING: frozenset({MVPStatus.VALIDATING}),
    # A failed release gate is correctable (more rework, a later retry):
    # VALIDATING only ever moves forward to RELEASED, it is never itself a
    # dead end — re-attempting release evaluation simply re-enters
    # VALIDATING (idempotent, see mark_mvp_validating).
    MVPStatus.VALIDATING: frozenset({MVPStatus.RELEASED}),
    MVPStatus.RELEASED: frozenset(),
    MVPStatus.FAILED: frozenset(),
    MVPStatus.BLOCKED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Project:
    """A project this orchestrator drives: a workspace plus its current MVP."""

    project_id: str
    name: str
    workspace: Path
    current_mvp_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.project_id, field_name="Project.project_id")
        _require_non_empty_str(self.name, field_name="Project.name")
        object.__setattr__(self, "workspace", Path(self.workspace))
        if self.current_mvp_id is not None:
            _require_non_empty_str(self.current_mvp_id, field_name="Project.current_mvp_id")


@dataclass(frozen=True, slots=True)
class MVP:
    """An MVP's objective and acceptance criteria, scoped to one project.

    Deliberately does not embed a list of WorkItem ids: WorkItems already
    reference their `mvp_id`, and `ProjectStateStore.list_work_items(mvp_id)`
    is the single source of that relationship — duplicating it here would
    be exactly the kind of speculative, sync-prone field this slice avoids.
    """

    mvp_id: str
    project_id: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    status: MVPStatus = MVPStatus.PLANNED

    def __post_init__(self) -> None:
        _require_non_empty_str(self.mvp_id, field_name="MVP.mvp_id")
        _require_non_empty_str(self.project_id, field_name="MVP.project_id")
        _require_non_empty_str(self.objective, field_name="MVP.objective")
        object.__setattr__(
            self,
            "acceptance_criteria",
            _as_tuple_of_str(self.acceptance_criteria, field_name="MVP.acceptance_criteria"),
        )
        if not isinstance(self.status, MVPStatus):
            raise TypeError(f"MVP.status must be an MVPStatus, got {type(self.status)!r}")


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A high-level unit of work delegated whole to Ralph once eligible.

    ``blocked_reason`` is populated only when ``status`` is ``BLOCKED`` —
    it exists because a `BLOCKED` status without knowing why (unknown
    dependency? a failed one? a cycle?) would not be "detecting cleanly",
    per this slice's explicit requirement.
    """

    work_item_id: str
    mvp_id: str
    title: str
    required_capabilities: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()
    acceptance_criteria: tuple[str, ...] = ()
    status: WorkItemStatus = WorkItemStatus.PLANNED
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.work_item_id, field_name="WorkItem.work_item_id")
        _require_non_empty_str(self.mvp_id, field_name="WorkItem.mvp_id")
        _require_non_empty_str(self.title, field_name="WorkItem.title")
        object.__setattr__(
            self,
            "required_capabilities",
            _as_frozenset_of_str(
                self.required_capabilities, field_name="WorkItem.required_capabilities"
            ),
        )
        object.__setattr__(
            self,
            "dependencies",
            _as_frozenset_of_str(self.dependencies, field_name="WorkItem.dependencies"),
        )
        object.__setattr__(
            self,
            "acceptance_criteria",
            _as_tuple_of_str(self.acceptance_criteria, field_name="WorkItem.acceptance_criteria"),
        )
        if not isinstance(self.status, WorkItemStatus):
            raise TypeError(f"WorkItem.status must be a WorkItemStatus, got {type(self.status)!r}")
        if self.blocked_reason is not None:
            _require_non_empty_str(self.blocked_reason, field_name="WorkItem.blocked_reason")


class ProjectStateError(Exception):
    """Base for project/MVP/work-item domain errors."""


class UnknownProjectError(ProjectStateError):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"unknown project: {project_id!r}")
        self.project_id = project_id


class DuplicateProjectError(ProjectStateError):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"project already exists: {project_id!r}")
        self.project_id = project_id


class UnknownMVPError(ProjectStateError):
    def __init__(self, mvp_id: str) -> None:
        super().__init__(f"unknown MVP: {mvp_id!r}")
        self.mvp_id = mvp_id


class DuplicateMVPError(ProjectStateError):
    def __init__(self, mvp_id: str) -> None:
        super().__init__(f"MVP already exists: {mvp_id!r}")
        self.mvp_id = mvp_id


class UnknownWorkItemError(ProjectStateError):
    def __init__(self, work_item_id: str) -> None:
        super().__init__(f"unknown work item: {work_item_id!r}")
        self.work_item_id = work_item_id


class DuplicateWorkItemError(ProjectStateError):
    def __init__(self, work_item_id: str) -> None:
        super().__init__(f"work item already exists: {work_item_id!r}")
        self.work_item_id = work_item_id


class InvalidWorkItemTransitionError(ProjectStateError):
    def __init__(self, work_item_id: str, current: WorkItemStatus, attempted: WorkItemStatus) -> None:
        super().__init__(
            f"work item {work_item_id!r} cannot move from {current.value!r} to {attempted.value!r}"
        )
        self.work_item_id = work_item_id
        self.current = current
        self.attempted = attempted


class InvalidMVPTransitionError(ProjectStateError):
    def __init__(self, mvp_id: str, current: MVPStatus, attempted: MVPStatus) -> None:
        super().__init__(f"MVP {mvp_id!r} cannot move from {current.value!r} to {attempted.value!r}")
        self.mvp_id = mvp_id
        self.current = current
        self.attempted = attempted


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workspace TEXT NOT NULL,
    current_mvp_id TEXT
);
CREATE TABLE IF NOT EXISTS mvps (
    mvp_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    work_item_id TEXT PRIMARY KEY,
    mvp_id TEXT NOT NULL,
    title TEXT NOT NULL,
    required_capabilities TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    status TEXT NOT NULL,
    blocked_reason TEXT
);
"""

_CSV_SEP = "\x1f"  # unit separator: safe, none of our string fields use it


def _encode_str_list(values: Iterable[str]) -> str:
    return _CSV_SEP.join(values)


def _decode_str_list(value: str) -> tuple[str, ...]:
    return tuple(v for v in value.split(_CSV_SEP) if v)


def _decode_project_row(row: sqlite3.Row) -> Project:
    return Project(
        project_id=row["project_id"],
        name=row["name"],
        workspace=Path(row["workspace"]),
        current_mvp_id=row["current_mvp_id"],
    )


def _decode_mvp_row(row: sqlite3.Row) -> MVP:
    return MVP(
        mvp_id=row["mvp_id"],
        project_id=row["project_id"],
        objective=row["objective"],
        acceptance_criteria=_decode_str_list(row["acceptance_criteria"]),
        status=MVPStatus(row["status"]),
    )


def _decode_work_item_row(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        work_item_id=row["work_item_id"],
        mvp_id=row["mvp_id"],
        title=row["title"],
        required_capabilities=_decode_str_list(row["required_capabilities"]),
        dependencies=_decode_str_list(row["dependencies"]),
        acceptance_criteria=_decode_str_list(row["acceptance_criteria"]),
        status=WorkItemStatus(row["status"]),
        blocked_reason=row["blocked_reason"],
    )


def _find_cycle_members(work_items: dict[str, WorkItem]) -> set[str]:
    """Detects WorkItems that are part of a dependency cycle.

    Deliberately simple (no topological-sort library, no complex graph
    engine): for each item, walk its dependency chain and check whether it
    is reachable from itself. Fine for the small graphs a single MVP has.
    """
    members: set[str] = set()
    for start_id, item in work_items.items():
        seen: set[str] = set()
        frontier = list(item.dependencies)
        while frontier:
            current = frontier.pop()
            if current == start_id:
                members.add(start_id)
                break
            if current in seen or current not in work_items:
                continue
            seen.add(current)
            frontier.extend(work_items[current].dependencies)
    return members


class ProjectStateStore:
    """Synchronous, sqlite3-backed store for Project/MVP/WorkItem state."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_CREATE_TABLES_SQL)

    def close(self) -> None:
        self._conn.close()

    # --- Project -----------------------------------------------------

    def create_project(self, *, project_id: str, name: str, workspace: str | Path) -> Project:
        project = Project(project_id=project_id, name=name, workspace=workspace)
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO projects (project_id, name, workspace, current_mvp_id) "
                    "VALUES (?, ?, ?, ?)",
                    (project.project_id, project.name, str(project.workspace), None),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateProjectError(project_id) from exc
        return project

    def get_project(self, project_id: str) -> Project:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise UnknownProjectError(project_id)
        return _decode_project_row(row)

    def set_current_mvp(self, project_id: str, mvp_id: str) -> Project:
        self.get_project(project_id)  # raises UnknownProjectError if absent
        self.get_mvp(mvp_id)  # raises UnknownMVPError if absent
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET current_mvp_id = ? WHERE project_id = ?",
                (mvp_id, project_id),
            )
        return self.get_project(project_id)

    # --- MVP -----------------------------------------------------------

    def create_mvp(
        self,
        *,
        mvp_id: str,
        project_id: str,
        objective: str,
        acceptance_criteria: Iterable[str] = (),
    ) -> MVP:
        self.get_project(project_id)  # raises UnknownProjectError if absent
        mvp = MVP(
            mvp_id=mvp_id, project_id=project_id, objective=objective,
            acceptance_criteria=tuple(acceptance_criteria),
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO mvps (mvp_id, project_id, objective, acceptance_criteria, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        mvp.mvp_id, mvp.project_id, mvp.objective,
                        _encode_str_list(mvp.acceptance_criteria), mvp.status.value,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMVPError(mvp_id) from exc
        return mvp

    def get_mvp(self, mvp_id: str) -> MVP:
        row = self._conn.execute("SELECT * FROM mvps WHERE mvp_id = ?", (mvp_id,)).fetchone()
        if row is None:
            raise UnknownMVPError(mvp_id)
        return _decode_mvp_row(row)

    def mark_mvp_running(self, mvp_id: str) -> MVP:
        current = self.get_mvp(mvp_id)
        if current.status is MVPStatus.RUNNING:
            return current  # idempotent: already running
        allowed = _MVP_TRANSITIONS.get(current.status, frozenset())
        if MVPStatus.RUNNING not in allowed:
            raise InvalidMVPTransitionError(mvp_id, current.status, MVPStatus.RUNNING)
        with self._conn:
            self._conn.execute(
                "UPDATE mvps SET status = ? WHERE mvp_id = ?", (MVPStatus.RUNNING.value, mvp_id)
            )
        return replace(current, status=MVPStatus.RUNNING)

    def mark_mvp_validating(self, mvp_id: str) -> MVP:
        """Enters (or re-enters) the release-evaluation state.

        Idempotent: a failed release gate leaves the MVP in VALIDATING
        (correctable, never a dead end), so re-attempting release
        evaluation later must not fail just because it is already there.
        """
        current = self.get_mvp(mvp_id)
        if current.status is MVPStatus.VALIDATING:
            return current
        allowed = _MVP_TRANSITIONS.get(current.status, frozenset())
        if MVPStatus.VALIDATING not in allowed:
            raise InvalidMVPTransitionError(mvp_id, current.status, MVPStatus.VALIDATING)
        with self._conn:
            self._conn.execute(
                "UPDATE mvps SET status = ? WHERE mvp_id = ?", (MVPStatus.VALIDATING.value, mvp_id)
            )
        return replace(current, status=MVPStatus.VALIDATING)

    def mark_mvp_released(self, mvp_id: str) -> MVP:
        current = self.get_mvp(mvp_id)
        allowed = _MVP_TRANSITIONS.get(current.status, frozenset())
        if MVPStatus.RELEASED not in allowed:
            raise InvalidMVPTransitionError(mvp_id, current.status, MVPStatus.RELEASED)
        with self._conn:
            self._conn.execute(
                "UPDATE mvps SET status = ? WHERE mvp_id = ?", (MVPStatus.RELEASED.value, mvp_id)
            )
        return replace(current, status=MVPStatus.RELEASED)

    # --- WorkItem --------------------------------------------------------

    def create_work_item(
        self,
        *,
        work_item_id: str,
        mvp_id: str,
        title: str,
        required_capabilities: Iterable[str] = (),
        dependencies: Iterable[str] = (),
        acceptance_criteria: Iterable[str] = (),
    ) -> WorkItem:
        self.get_mvp(mvp_id)  # raises UnknownMVPError if absent
        work_item = WorkItem(
            work_item_id=work_item_id, mvp_id=mvp_id, title=title,
            required_capabilities=frozenset(required_capabilities),
            dependencies=frozenset(dependencies),
            acceptance_criteria=tuple(acceptance_criteria),
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO work_items (work_item_id, mvp_id, title, required_capabilities, "
                    "dependencies, acceptance_criteria, status, blocked_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        work_item.work_item_id, work_item.mvp_id, work_item.title,
                        _encode_str_list(work_item.required_capabilities),
                        _encode_str_list(work_item.dependencies),
                        _encode_str_list(work_item.acceptance_criteria),
                        work_item.status.value, None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateWorkItemError(work_item_id) from exc
        return work_item

    def get_work_item(self, work_item_id: str) -> WorkItem:
        row = self._conn.execute(
            "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise UnknownWorkItemError(work_item_id)
        return _decode_work_item_row(row)

    def list_work_items(self, mvp_id: str) -> list[WorkItem]:
        """All WorkItems of an MVP, in a stable, deterministic order (work_item_id)."""
        rows = self._conn.execute(
            "SELECT * FROM work_items WHERE mvp_id = ? ORDER BY work_item_id ASC", (mvp_id,)
        ).fetchall()
        return [_decode_work_item_row(row) for row in rows]

    def refresh_readiness(self, mvp_id: str) -> list[WorkItem]:
        """Promotes PLANNED WorkItems to READY or BLOCKED based on dependencies.

        - Unknown dependency, or a dependency that is FAILED/BLOCKED, or a
          dependency cycle -> BLOCKED (with a reason), never a silent,
          forever-PLANNED deadlock.
        - Every dependency COMPLETED -> READY.
        - Otherwise (a dependency still PLANNED/READY/RUNNING) -> stays
          PLANNED.

        Returns every WorkItem actually transitioned (READY or BLOCKED).
        """
        items = {wi.work_item_id: wi for wi in self.list_work_items(mvp_id)}
        cycle_members = _find_cycle_members(items)
        updated: list[WorkItem] = []

        for work_item_id, item in items.items():
            if item.status is not WorkItemStatus.PLANNED:
                continue

            if work_item_id in cycle_members:
                updated.append(
                    self._transition_work_item(
                        work_item_id, WorkItemStatus.BLOCKED, reason="circular dependency"
                    )
                )
                continue

            unknown = sorted(d for d in item.dependencies if d not in items)
            if unknown:
                updated.append(
                    self._transition_work_item(
                        work_item_id, WorkItemStatus.BLOCKED,
                        reason=f"unknown dependency: {unknown!r}",
                    )
                )
                continue

            unresolvable = sorted(
                d for d in item.dependencies
                if items[d].status in (WorkItemStatus.FAILED, WorkItemStatus.BLOCKED)
            )
            if unresolvable:
                updated.append(
                    self._transition_work_item(
                        work_item_id, WorkItemStatus.BLOCKED,
                        reason=f"dependency cannot complete: {unresolvable!r}",
                    )
                )
                continue

            if all(items[d].status is WorkItemStatus.COMPLETED for d in item.dependencies):
                updated.append(self._transition_work_item(work_item_id, WorkItemStatus.READY))

        return updated

    def mark_work_item_running(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, WorkItemStatus.RUNNING)

    def mark_work_item_completed(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, WorkItemStatus.COMPLETED)

    def mark_work_item_failed(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, WorkItemStatus.FAILED)

    def mark_work_item_reviewing(self, work_item_id: str) -> WorkItem:
        """Historical/decode-support transition only — see
        ``WorkItemStatus.REVIEWING``'s own docstring. No current
        orchestration code path calls this; kept as a plain state-machine
        primitive so a WorkItem that reached REVIEWING under the removed
        GOVERNED_FULL pipeline (or a test reconstructing that scenario)
        can still be transitioned out of it correctly."""
        return self._transition_work_item(work_item_id, WorkItemStatus.REVIEWING)

    def mark_work_item_needs_rework(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, WorkItemStatus.NEEDS_REWORK)

    def mark_work_item_waiting(self, work_item_id: str) -> WorkItem:
        """Enters WAITING from READY, NEEDS_REWORK, or REVIEWING.

        Which of those it came from is exactly the phase a caller must
        record in a ``orchestrator.wait.WaitRecord`` alongside this call —
        this store deliberately does not duplicate that reason itself
        (see ``WorkItemStatus.WAITING``'s docstring).
        """
        return self._transition_work_item(work_item_id, WorkItemStatus.WAITING)

    def mark_work_item_ready(self, work_item_id: str) -> WorkItem:
        """Resumes a WAITING WorkItem back into READY for a fresh development selection.

        Never resumes "in place": the caller must still go through the
        normal ``run_next_work_item`` candidate selection (a new worker may
        be chosen) and will get a brand new execution — this only makes
        the WorkItem eligible for that again.
        """
        return self._transition_work_item(work_item_id, WorkItemStatus.READY)

    def mark_work_item_recovery_required(self, work_item_id: str) -> WorkItem:
        """Enters RECOVERY_REQUIRED from RUNNING or REVIEWING.

        Used when an execution never reached a reliable terminal outcome —
        an orphaned RUNNING execution found after a restart, or one that
        came back INTERRUPTED — never for a quota/availability wait (that
        is ``mark_work_item_waiting``). The caller (``RecoveryCoordinator``)
        must ensure a durable recovery handoff exists before calling this,
        so the WorkItem is immediately re-orchestrable afterward.
        """
        return self._transition_work_item(work_item_id, WorkItemStatus.RECOVERY_REQUIRED)

    def mark_work_item_blocked(self, work_item_id: str, *, reason: str) -> WorkItem:
        """Blocks a WorkItem for a reason other than dependency resolution.

        Unlike ``refresh_readiness``'s own BLOCKED transitions (dependency
        graph problems, only ever applied from PLANNED), this is the
        public entry point callers (MVPManager) use for other legitimate
        reasons that must fail-closed rather than dangle — e.g. no
        eligible independent reviewer available, or a bounded review/rework
        loop exhausted without approval.
        """
        _require_non_empty_str(reason, field_name="reason")
        return self._transition_work_item(work_item_id, WorkItemStatus.BLOCKED, reason=reason)

    def _transition_work_item(
        self, work_item_id: str, new_status: WorkItemStatus, *, reason: str | None = None
    ) -> WorkItem:
        current = self.get_work_item(work_item_id)
        allowed = _WORK_ITEM_TRANSITIONS.get(current.status, frozenset())
        if new_status not in allowed:
            raise InvalidWorkItemTransitionError(work_item_id, current.status, new_status)
        updated = replace(current, status=new_status, blocked_reason=reason)
        with self._conn:
            self._conn.execute(
                "UPDATE work_items SET status = ?, blocked_reason = ? WHERE work_item_id = ?",
                (updated.status.value, updated.blocked_reason, work_item_id),
            )
        return updated
