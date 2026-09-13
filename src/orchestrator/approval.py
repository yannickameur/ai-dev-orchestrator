"""Notification + optimistic approval window for roadmap proposals (Slice 13).

Once Slice 12 produces a ``RoadmapProposal`` (``PROPOSED``), this module
answers exactly one question: "should the orchestrator now treat that
proposal as approved?" — by opening a durable, time-boxed
``ApprovalWindow``, notifying whoever should look at it, and resolving to
``APPROVED``/``REJECTED``/``MODIFY_REQUESTED`` (an explicit decision) or
``AUTO_APPROVED`` (silence past the deadline).

It never mutates ``ROADMAP.md``, never creates a real MVP in
``ProjectStateStore``, and never starts a new planning session on
``MODIFY_REQUESTED`` — those remain a later slice's job, operating on the
decision this module persists.

APPROVAL STATE IS AN ORCHESTRATION DECISION, NOT PART OF ``RoadmapProposal``:
exactly like ``WaitRecord`` layers "should we retry now" on top of
``ProviderAvailability`` without ever touching it (see
:mod:`~orchestrator.wait`), an ``ApprovalWindow`` layers "has this proposal
been approved" on top of a ``RoadmapProposal`` it only references by id.
``planning.RoadmapProposalStatus`` stays pinned to its single ``PROPOSED``
value — Slice 12 explicitly reserved ``AWAITING_APPROVAL``/``APPROVED``/
``REJECTED``/``AUTO_APPROVED`` for this module, never for itself.

ONE WINDOW PER PROPOSAL, IDEMPOTENT OPEN: ``ApprovalCoordinator.open_window``
is safe to call more than once for the same ``roadmap_proposal_id`` (e.g.
after a restart replays the step that opens it) — it returns the existing
window unchanged rather than fabricating a second deadline or re-sending a
notification.

NO POLLING: exactly like :mod:`~orchestrator.wait`, this module is purely
event/time-based. It has no background thread, no loop, no sleep —
``list_due(now)``/``next_due_at()`` let a caller ask "is anything due?"
whenever it wants (e.g. once per orchestration tick), and the *decision* of
whether to actually resolve a due window belongs to
``ApprovalCoordinator.resolve_due``, which callers invoke on their own
cadence.

RESTART SAFETY: the deadline (``deadline_at``) is computed once, at window
creation time, from the policy in effect *then*
(``ApprovalWindow.auto_approval_enabled`` freezes whether optimistic
approval was even active) — never recomputed from a policy read later, and
never held only in memory. A window created before a process restart and
found due after it is auto-approved exactly as if the process had never
stopped.

SILENCE IS SCOPED, NARROWLY: the "no response ⇒ approved" policy encoded
here governs roadmap/MVP governance only — a caller must never reuse this
module, or its "silence ⇒ proceed" posture, to gate a destructive,
financial, or otherwise sensitive action elsewhere in the system.

NOTIFICATION DELIVERY NEVER GATES THE DEADLINE: ``ApprovalCoordinator``
accepts an optional notifier callable. If it raises, the window is still
created and its deadline still stands — the failure is recorded
(``notification_error``) for audit, never allowed to corrupt or block the
approval mechanism itself.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
NotificationSink = Callable[["ApprovalWindow", str], None]

DEFAULT_WINDOW_SECONDS = 20 * 60.0


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class ApprovalStatus(str, Enum):
    """``RoadmapProposalStatus`` deliberately stays a single value in Slice
    12 — these four states live here instead, layered on top."""

    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFY_REQUESTED = "modify_requested"
    AUTO_APPROVED = "auto_approved"


_TERMINAL_APPROVAL_STATUSES = (
    ApprovalStatus.APPROVED,
    ApprovalStatus.REJECTED,
    ApprovalStatus.MODIFY_REQUESTED,
    ApprovalStatus.AUTO_APPROVED,
)


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Small, explicit policy — configurable delay and activation, per
    ROADMAP.md's Slice 13 requirement. Disabling ``enabled`` never removes
    the deadline mechanics; it only means ``resolve_due`` never
    auto-approves a window opened while it was disabled (see
    ``ApprovalWindow.auto_approval_enabled``, frozen at creation time)."""

    window_seconds: float = DEFAULT_WINDOW_SECONDS
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.window_seconds, (int, float)) or self.window_seconds <= 0:
            raise ValueError("ApprovalPolicy.window_seconds must be a positive number")


@dataclass(frozen=True, slots=True)
class ApprovalWindow:
    """One durable approval window for a single ``RoadmapProposal`` —
    never deleted, even once resolved (audit trail, like ``WaitRecord``)."""

    approval_id: str
    project_id: str
    mvp_id: str
    planning_session_id: str
    roadmap_proposal_id: str
    created_at: datetime
    deadline_at: datetime
    auto_approval_enabled: bool
    status: ApprovalStatus = ApprovalStatus.AWAITING_APPROVAL
    notified_at: datetime | None = None
    notification_error: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "approval_id", "project_id", "mvp_id", "planning_session_id", "roadmap_proposal_id",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"ApprovalWindow.{name}")
        if not isinstance(self.status, ApprovalStatus):
            raise TypeError(f"ApprovalWindow.status must be an ApprovalStatus, got {type(self.status)!r}")
        _require_aware(self.created_at, field_name="ApprovalWindow.created_at")
        _require_aware(self.deadline_at, field_name="ApprovalWindow.deadline_at")
        if self.notified_at is not None:
            _require_aware(self.notified_at, field_name="ApprovalWindow.notified_at")
        if self.decided_at is not None:
            _require_aware(self.decided_at, field_name="ApprovalWindow.decided_at")
        for name in ("notification_error", "decision_reason"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ApprovalWindow.{name}")


class ApprovalStoreError(Exception):
    """Base for ApprovalStore domain errors."""


class UnknownApprovalWindowError(ApprovalStoreError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"unknown approval window: {approval_id!r}")
        self.approval_id = approval_id


class DuplicateApprovalWindowError(ApprovalStoreError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval window already exists: {approval_id!r}")
        self.approval_id = approval_id


class InvalidApprovalTransitionError(ApprovalStoreError):
    def __init__(self, approval_id: str, current: ApprovalStatus) -> None:
        super().__init__(
            f"approval window {approval_id!r} is already {current.value!r}, cannot decide again"
        )
        self.approval_id = approval_id
        self.current = current


class ApprovalNotYetDueError(ApprovalStoreError):
    def __init__(self, approval_id: str, deadline_at: datetime, now: datetime) -> None:
        super().__init__(
            f"approval window {approval_id!r} is not due yet (deadline={deadline_at.isoformat()}, "
            f"now={now.isoformat()}) — auto-approval never fabricated early"
        )
        self.approval_id = approval_id
        self.deadline_at = deadline_at
        self.now = now


class CorruptApprovalWindowError(ApprovalStoreError):
    def __init__(self, approval_id: str, detail: str) -> None:
        super().__init__(f"corrupt approval window {approval_id!r}: {detail}")
        self.approval_id = approval_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS approval_windows (
    approval_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    planning_session_id TEXT NOT NULL,
    roadmap_proposal_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    auto_approval_enabled INTEGER NOT NULL,
    status TEXT NOT NULL,
    notified_at TEXT,
    notification_error TEXT,
    decided_at TEXT,
    decision_reason TEXT
)
"""


def _decode_row(row: sqlite3.Row) -> ApprovalWindow:
    approval_id = row["approval_id"]
    try:
        return ApprovalWindow(
            approval_id=approval_id, project_id=row["project_id"], mvp_id=row["mvp_id"],
            planning_session_id=row["planning_session_id"], roadmap_proposal_id=row["roadmap_proposal_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            deadline_at=datetime.fromisoformat(row["deadline_at"]),
            auto_approval_enabled=bool(row["auto_approval_enabled"]), status=ApprovalStatus(row["status"]),
            notified_at=datetime.fromisoformat(row["notified_at"]) if row["notified_at"] else None,
            notification_error=row["notification_error"],
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            decision_reason=row["decision_reason"],
        )
    except (ValueError, TypeError) as exc:
        raise CorruptApprovalWindowError(approval_id, str(exc)) from exc


class ApprovalStore:
    """Synchronous, sqlite3-backed store for durable ApprovalWindows."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        *,
        approval_id: str,
        project_id: str,
        mvp_id: str,
        planning_session_id: str,
        roadmap_proposal_id: str,
        deadline_at: datetime,
        auto_approval_enabled: bool,
        created_at: datetime | None = None,
    ) -> ApprovalWindow:
        window = ApprovalWindow(
            approval_id=approval_id, project_id=project_id, mvp_id=mvp_id,
            planning_session_id=planning_session_id, roadmap_proposal_id=roadmap_proposal_id,
            created_at=created_at or self._now(), deadline_at=deadline_at,
            auto_approval_enabled=auto_approval_enabled,
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO approval_windows (approval_id, project_id, mvp_id, "
                    "planning_session_id, roadmap_proposal_id, created_at, deadline_at, "
                    "auto_approval_enabled, status, notified_at, notification_error, "
                    "decided_at, decision_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        window.approval_id, window.project_id, window.mvp_id,
                        window.planning_session_id, window.roadmap_proposal_id,
                        window.created_at.isoformat(), window.deadline_at.isoformat(),
                        int(window.auto_approval_enabled), window.status.value,
                        None, None, None, None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateApprovalWindowError(approval_id) from exc
        return window

    def get(self, approval_id: str) -> ApprovalWindow:
        row = self._conn.execute(
            "SELECT * FROM approval_windows WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise UnknownApprovalWindowError(approval_id)
        return _decode_row(row)

    def get_for_proposal(self, roadmap_proposal_id: str) -> ApprovalWindow | None:
        row = self._conn.execute(
            "SELECT * FROM approval_windows WHERE roadmap_proposal_id = ?", (roadmap_proposal_id,)
        ).fetchone()
        return _decode_row(row) if row is not None else None

    def list_for_mvp(self, mvp_id: str) -> list[ApprovalWindow]:
        """Every approval window ever created for an MVP, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM approval_windows WHERE mvp_id = ? ORDER BY created_at ASC", (mvp_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def list_pending(self) -> list[ApprovalWindow]:
        rows = self._conn.execute(
            "SELECT * FROM approval_windows WHERE status = ? ORDER BY deadline_at ASC",
            (ApprovalStatus.AWAITING_APPROVAL.value,),
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def list_due(self, now: datetime) -> list[ApprovalWindow]:
        """Pending windows whose deadline has passed, auto-approval-eligible
        or not (callers decide what to do with the ``auto_approval_enabled``
        flag) — the read primitive instead of continuous polling."""
        _require_aware(now, field_name="now")
        return [w for w in self.list_pending() if w.deadline_at <= now]

    def next_due_at(self) -> datetime | None:
        pending = self.list_pending()
        return min((w.deadline_at for w in pending), default=None)

    def mark_notified(
        self, approval_id: str, *, notified_at: datetime | None = None, error: str | None = None
    ) -> ApprovalWindow:
        current = self.get(approval_id)
        updated = replace(
            current, notified_at=notified_at or self._now(), notification_error=error,
        )
        with self._conn:
            self._conn.execute(
                "UPDATE approval_windows SET notified_at = ?, notification_error = ? WHERE approval_id = ?",
                (updated.notified_at.isoformat(), updated.notification_error, approval_id),
            )
        return updated

    def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reason: str,
        decided_at: datetime | None = None,
        require_due: bool = False,
    ) -> ApprovalWindow:
        if status not in _TERMINAL_APPROVAL_STATUSES:
            raise ValueError(f"decide() requires a terminal ApprovalStatus, got {status!r}")
        current = self.get(approval_id)
        if current.status is not ApprovalStatus.AWAITING_APPROVAL:
            raise InvalidApprovalTransitionError(approval_id, current.status)
        resolved_at = decided_at or self._now()
        if require_due and resolved_at < current.deadline_at:
            raise ApprovalNotYetDueError(approval_id, current.deadline_at, resolved_at)
        with self._conn:
            self._conn.execute(
                "UPDATE approval_windows SET status = ?, decided_at = ?, decision_reason = ? "
                "WHERE approval_id = ?",
                (status.value, resolved_at.isoformat(), reason, approval_id),
            )
        return self.get(approval_id)

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="ApprovalStore clock()")
        return value


class ApprovalCoordinator:
    """Opens/notifies/resolves approval windows for ``RoadmapProposal``s.

    Pure orchestration on top of ``ApprovalStore``: never mutates
    ``ROADMAP.md``, never creates a real MVP, never starts a new planning
    session on ``MODIFY_REQUESTED`` — a later slice acts on the decisions
    recorded here.
    """

    def __init__(
        self,
        approval_store: ApprovalStore,
        *,
        policy: ApprovalPolicy | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        notifier: NotificationSink | None = None,
    ) -> None:
        self._approval_store = approval_store
        self._policy = policy or ApprovalPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._notifier = notifier

    def open_window(
        self,
        *,
        project_id: str,
        mvp_id: str,
        planning_session_id: str,
        roadmap_proposal_id: str,
        notification_body: str = "",
    ) -> ApprovalWindow:
        """Idempotent: a second call for the same ``roadmap_proposal_id``
        returns the existing window untouched — never a fresh deadline,
        never a repeated notification."""
        existing = self._approval_store.get_for_proposal(roadmap_proposal_id)
        if existing is not None:
            return existing

        now = self._clock()
        window = self._approval_store.create(
            approval_id=self._id_factory(), project_id=project_id, mvp_id=mvp_id,
            planning_session_id=planning_session_id, roadmap_proposal_id=roadmap_proposal_id,
            deadline_at=now + timedelta(seconds=self._policy.window_seconds),
            auto_approval_enabled=self._policy.enabled, created_at=now,
        )
        return self._notify(window, notification_body)

    def _notify(self, window: ApprovalWindow, notification_body: str) -> ApprovalWindow:
        if self._notifier is None:
            return window
        try:
            self._notifier(window, notification_body)
        except Exception as exc:  # noqa: BLE001 - delivery failure must never break the deadline
            return self._approval_store.mark_notified(window.approval_id, error=str(exc))
        return self._approval_store.mark_notified(window.approval_id)

    def approve(self, approval_id: str, *, reason: str = "approved") -> ApprovalWindow:
        return self._approval_store.decide(
            approval_id, status=ApprovalStatus.APPROVED, reason=reason, decided_at=self._clock(),
        )

    def reject(self, approval_id: str, *, reason: str) -> ApprovalWindow:
        return self._approval_store.decide(
            approval_id, status=ApprovalStatus.REJECTED, reason=reason, decided_at=self._clock(),
        )

    def request_modification(self, approval_id: str, *, reason: str) -> ApprovalWindow:
        return self._approval_store.decide(
            approval_id, status=ApprovalStatus.MODIFY_REQUESTED, reason=reason, decided_at=self._clock(),
        )

    def list_due(self, *, now: datetime | None = None) -> list[ApprovalWindow]:
        """Due windows whose policy had auto-approval enabled at creation
        time — the set ``resolve_due`` would act on, without acting on it."""
        moment = now or self._clock()
        return [w for w in self._approval_store.list_due(moment) if w.auto_approval_enabled]

    def next_due_at(self) -> datetime | None:
        return self._approval_store.next_due_at()

    def resolve_due(self, *, now: datetime | None = None) -> list[ApprovalWindow]:
        """Auto-approves every currently-due, auto-approval-enabled window.

        Purely event/time-based, exactly like ``WaitCoordinator`` — no
        background thread, no sleep; a caller invokes this on its own
        cadence (e.g. once per orchestration tick) and gets back whatever
        was actually resolved just now.
        """
        moment = now or self._clock()
        resolved: list[ApprovalWindow] = []
        for window in sorted(self.list_due(now=moment), key=lambda w: (w.deadline_at, w.approval_id)):
            resolved.append(
                self._approval_store.decide(
                    window.approval_id, status=ApprovalStatus.AUTO_APPROVED,
                    reason="optimistic approval window elapsed with no response",
                    decided_at=moment, require_due=True,
                )
            )
        return resolved
