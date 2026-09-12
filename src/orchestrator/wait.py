"""Durable wait/resume bookkeeping — WaitRecord/WaitStore/WaitCoordinator (Slice 11).

This module answers exactly one question: "given that WorkerSelector could
not find an eligible worker right now, is that diagnosable as a quota
issue with a known reset time — and if so, when should the orchestrator
try again?" It never selects a worker, never probes a provider, never
executes anything, and never re-implements WorkerSelector/QuotaManager
logic — it only interprets the read-only
:class:`~orchestrator.worker_selector.ProviderSelectionDiagnostic` facts
those already expose on a selection failure, and persists the resulting
decision.

WAITING_RESET IS AN ORCHESTRATION DECISION, NOT PROVIDER TRUTH:
``ProviderAvailability`` (Slices 0/1/2) remains the raw provider fact. A
``WaitRecord`` represents this module's own decision — "this WorkItem
cannot proceed right now because no eligible worker exists, and a
plausible next moment to retry is known" — layered on top of that fact,
never confused with it.

NO BLIND RETRY, EVER:
resuming a wait always means a *new* selection attempt (and, if it
succeeds, a brand new ``execution_id`` — see
:mod:`~orchestrator.mvp_manager`). This module never remembers or reuses
an old execution; it only remembers *that something was waiting, why, and
since when*.

NO POLLING: this module is purely event/time-based. It has no background
thread, no loop, no sleep — ``list_due(now)``/``next_due_at()`` let a
caller (itself invoked whenever it wants — e.g. once per
``MVPManager.run_next_work_item()`` call) ask "is anything due?" without
this module ever driving that cadence itself.

FAIL-CLOSED ON THE DEADLINE ITSELF: if no candidate provider's
``QuotaWindow.reset_at`` is known, no wait is ever fabricated with an
invented deadline — ``WaitCoordinator.record_wait`` returns ``None`` in
that case, and the caller falls back to its own existing failure handling
(propagate, or a different terminal state) rather than lying about when
retrying might help.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class WaitPhase(str, Enum):
    """What the WorkItem was trying to do when it had to start waiting.

    Deliberately small: enough to know whether resuming means re-entering
    READY, NEEDS_REWORK, or REVIEWING — never a general-purpose workflow
    state machine.
    """

    DEVELOPMENT = "development"
    REWORK = "rework"
    REVIEW = "review"


class WaitReason(str, Enum):
    """Why the wait exists. Only one reason in this slice, by design —
    room to add more later (e.g. a generic provider outage) without
    touching WorkItemStatus."""

    QUOTA_RESET = "quota_reset"


class WaitStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class WaitRecord:
    """One durable, auditable wait — never deleted, even once resolved."""

    wait_id: str
    project_id: str
    mvp_id: str
    work_item_id: str
    phase: WaitPhase
    reason: WaitReason
    created_at: datetime
    eligible_at: datetime
    status: WaitStatus = WaitStatus.PENDING
    providers: tuple[str, ...] = ()
    source_execution_id: str | None = None
    source_handoff_id: str | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None

    def __post_init__(self) -> None:
        for name in ("wait_id", "project_id", "mvp_id", "work_item_id"):
            _require_non_empty_str(getattr(self, name), field_name=f"WaitRecord.{name}")
        if not isinstance(self.phase, WaitPhase):
            raise TypeError(f"WaitRecord.phase must be a WaitPhase, got {type(self.phase)!r}")
        if not isinstance(self.reason, WaitReason):
            raise TypeError(f"WaitRecord.reason must be a WaitReason, got {type(self.reason)!r}")
        if not isinstance(self.status, WaitStatus):
            raise TypeError(f"WaitRecord.status must be a WaitStatus, got {type(self.status)!r}")
        _require_aware(self.created_at, field_name="WaitRecord.created_at")
        _require_aware(self.eligible_at, field_name="WaitRecord.eligible_at")
        if self.resolved_at is not None:
            _require_aware(self.resolved_at, field_name="WaitRecord.resolved_at")
        object.__setattr__(self, "providers", tuple(self.providers))
        for name in ("source_execution_id", "source_handoff_id", "resolution"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"WaitRecord.{name}")


class WaitStoreError(Exception):
    """Base for WaitStore domain errors."""


class UnknownWaitError(WaitStoreError):
    def __init__(self, wait_id: str) -> None:
        super().__init__(f"unknown wait: {wait_id!r}")
        self.wait_id = wait_id


class DuplicateWaitError(WaitStoreError):
    def __init__(self, wait_id: str) -> None:
        super().__init__(f"wait already exists: {wait_id!r}")
        self.wait_id = wait_id


class InvalidWaitTransitionError(WaitStoreError):
    def __init__(self, wait_id: str, current: WaitStatus) -> None:
        super().__init__(f"wait {wait_id!r} is already {current.value!r}, cannot resolve again")
        self.wait_id = wait_id
        self.current = current


class CorruptWaitRecordError(WaitStoreError):
    def __init__(self, wait_id: str, detail: str) -> None:
        super().__init__(f"corrupt wait record {wait_id!r}: {detail}")
        self.wait_id = wait_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS waits (
    wait_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    eligible_at TEXT NOT NULL,
    status TEXT NOT NULL,
    providers TEXT NOT NULL,
    source_execution_id TEXT,
    source_handoff_id TEXT,
    resolved_at TEXT,
    resolution TEXT
)
"""


def _decode_row(row: sqlite3.Row) -> WaitRecord:
    wait_id = row["wait_id"]
    try:
        return WaitRecord(
            wait_id=wait_id, project_id=row["project_id"], mvp_id=row["mvp_id"],
            work_item_id=row["work_item_id"], phase=WaitPhase(row["phase"]),
            reason=WaitReason(row["reason"]), created_at=datetime.fromisoformat(row["created_at"]),
            eligible_at=datetime.fromisoformat(row["eligible_at"]), status=WaitStatus(row["status"]),
            providers=tuple(p for p in row["providers"].split(",") if p),
            source_execution_id=row["source_execution_id"], source_handoff_id=row["source_handoff_id"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            resolution=row["resolution"],
        )
    except (ValueError, TypeError) as exc:
        raise CorruptWaitRecordError(wait_id, str(exc)) from exc


class WaitStore:
    """Synchronous, sqlite3-backed store for durable WaitRecords."""

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
        wait_id: str,
        project_id: str,
        mvp_id: str,
        work_item_id: str,
        phase: WaitPhase,
        reason: WaitReason,
        eligible_at: datetime,
        providers: Sequence[str] = (),
        source_execution_id: str | None = None,
        source_handoff_id: str | None = None,
        created_at: datetime | None = None,
    ) -> WaitRecord:
        record = WaitRecord(
            wait_id=wait_id, project_id=project_id, mvp_id=mvp_id, work_item_id=work_item_id,
            phase=phase, reason=reason, created_at=created_at or self._now(), eligible_at=eligible_at,
            providers=tuple(providers), source_execution_id=source_execution_id,
            source_handoff_id=source_handoff_id,
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO waits (wait_id, project_id, mvp_id, work_item_id, phase, reason, "
                    "created_at, eligible_at, status, providers, source_execution_id, "
                    "source_handoff_id, resolved_at, resolution) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.wait_id, record.project_id, record.mvp_id, record.work_item_id,
                        record.phase.value, record.reason.value, record.created_at.isoformat(),
                        record.eligible_at.isoformat(), record.status.value,
                        ",".join(record.providers), record.source_execution_id,
                        record.source_handoff_id, None, None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateWaitError(wait_id) from exc
        return record

    def get(self, wait_id: str) -> WaitRecord:
        row = self._conn.execute("SELECT * FROM waits WHERE wait_id = ?", (wait_id,)).fetchone()
        if row is None:
            raise UnknownWaitError(wait_id)
        return _decode_row(row)

    def list_for_mvp(self, mvp_id: str) -> list[WaitRecord]:
        """Every wait ever created for an MVP (pending or resolved), oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM waits WHERE mvp_id = ? ORDER BY created_at ASC", (mvp_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def list_pending(self) -> list[WaitRecord]:
        rows = self._conn.execute(
            "SELECT * FROM waits WHERE status = ? ORDER BY eligible_at ASC", (WaitStatus.PENDING.value,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def list_due(self, now: datetime) -> list[WaitRecord]:
        """Pending waits whose eligible_at has passed — the read primitive
        an orchestrator asks instead of polling providers continuously."""
        _require_aware(now, field_name="now")
        return [w for w in self.list_pending() if w.eligible_at <= now]

    def next_due_at(self) -> datetime | None:
        pending = self.list_pending()
        return min((w.eligible_at for w in pending), default=None)

    def resolve(self, wait_id: str, *, resolved_at: datetime | None = None, resolution: str) -> WaitRecord:
        current = self.get(wait_id)
        if current.status is not WaitStatus.PENDING:
            raise InvalidWaitTransitionError(wait_id, current.status)
        updated_resolved_at = resolved_at or self._now()
        with self._conn:
            self._conn.execute(
                "UPDATE waits SET status = ?, resolved_at = ?, resolution = ? WHERE wait_id = ?",
                (WaitStatus.RESOLVED.value, updated_resolved_at.isoformat(), resolution, wait_id),
            )
        return self.get(wait_id)

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="WaitStore clock()")
        return value


class WaitCoordinator:
    """Turns a WorkerSelector selection-failure diagnostic into a durable wait.

    Pure bookkeeping: never selects a worker, never probes a provider,
    never executes anything. The one place Slice 11's "is this
    quota-diagnosable, and if so what's the next plausible retry moment"
    policy lives, so callers (``MVPManager``) don't have to.
    """

    def __init__(
        self, wait_store: WaitStore, *, clock: Clock | None = None, id_factory: IdFactory | None = None
    ) -> None:
        self._wait_store = wait_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def next_quota_reset(self, diagnostics: Sequence) -> datetime | None:
        """The earliest known reset among candidates reported quota-exhausted.

        Returns ``None`` if no candidate's reason is ``"quota_exhausted"``
        with at least one known ``reset_at`` — callers must never fabricate
        a deadline in that case.
        """
        candidates = [
            reset_at
            for diagnostic in diagnostics
            if getattr(diagnostic, "reason", None) == "quota_exhausted"
            for reset_at in getattr(diagnostic, "reset_at", ())
        ]
        return min(candidates) if candidates else None

    def record_wait(
        self,
        *,
        project_id: str,
        mvp_id: str,
        work_item_id: str,
        phase: WaitPhase,
        diagnostics: Sequence,
        source_execution_id: str | None = None,
        source_handoff_id: str | None = None,
    ) -> WaitRecord | None:
        """Persists a new wait if diagnosable as quota, else returns None.

        A ``None`` return means the caller must fall back to its own
        existing failure handling — never presented as a fake wait.
        """
        eligible_at = self.next_quota_reset(diagnostics)
        if eligible_at is None:
            return None
        providers = tuple(
            sorted(
                {
                    d.provider for d in diagnostics
                    if getattr(d, "reason", None) == "quota_exhausted" and getattr(d, "reset_at", ())
                }
            )
        )
        return self._wait_store.create(
            wait_id=self._id_factory(), project_id=project_id, mvp_id=mvp_id, work_item_id=work_item_id,
            phase=phase, reason=WaitReason.QUOTA_RESET, eligible_at=eligible_at, providers=providers,
            source_execution_id=source_execution_id, source_handoff_id=source_handoff_id,
            created_at=self._clock(),
        )

    def find_due(self, mvp_id: str) -> WaitRecord | None:
        """The lexically-first due wait for this MVP, if any — never polls,
        just asks the store what is already known to be due right now."""
        due = [w for w in self._wait_store.list_due(self._clock()) if w.mvp_id == mvp_id]
        return sorted(due, key=lambda w: w.work_item_id)[0] if due else None

    def resolve(self, wait_id: str, *, resolution: str) -> WaitRecord:
        return self._wait_store.resolve(wait_id, resolved_at=self._clock(), resolution=resolution)
