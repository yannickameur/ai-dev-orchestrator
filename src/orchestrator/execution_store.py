"""ExecutionStore — audit persistence for a single execution's identity/state.

This module answers, after the fact, exactly the questions the future
``RalphExecutionEngine`` will need answered (see ROADMAP.md, Phase 1, step
8): which task, which worker (as a full snapshot, not a live reference),
which provider/backend/model/reasoning_effort, which role, when it started
and finished, what its status and exit code were, which provider/Ralph
session it correlates to, and which git SHAs bound it — plus, critically,
whether an old execution was still ``RUNNING`` when something crashed.

This slice does not run anything: no Claude/Codex/Ralph subprocess, no git
subprocess, no worker selection, no quota logic. It only persists and
reads back execution identity/state, synchronously, via ``sqlite3``
(stdlib) — atomic per-call, durable across process restarts, no ORM.

Design invariants:

- ``execution_id`` is its own technical identity, distinct from
  ``task_id`` and ``worker_id`` — a task can have several executions, and
  a worker can appear in several executions.
- A record captures a *snapshot* of the worker identity used for that
  execution (provider, backend, model, reasoning_effort). It never
  references a live, possibly-since-mutated ``Worker`` object — the audit
  trail must stay correct even if the worker registry changes later.
- Identity fields (``execution_id``, ``task_id``, ``worker_id``,
  ``provider``, ``backend``, ``model``, ``reasoning_effort``, ``role``,
  ``started_at``) are set once at ``create()`` and never changed by any
  transition method — the typed transition API simply has no parameter
  for them.
- EXIT CODE != BUSINESS VERDICT: ``exit_code`` is audit data only. Nothing
  in this module derives ``SUCCEEDED``/``FAILED`` from it automatically —
  the spike showed a real case where the expected business event fired
  but Ralph still reported ``reason=max_iterations``/a non-zero exit code.
  A caller (the future engine/governance layer) decides the final status
  from business events and passes it explicitly via ``mark_succeeded`` or
  ``mark_failed``.
- ``WAITING_RESET`` does not belong here — it is a future
  orchestration/policy state, not an execution audit state.
- Never blind-retry: this module never re-launches anything. It only
  provides ``list_running()`` to find executions still ``RUNNING`` after a
  restart, and explicit ``mark_interrupted``/``mark_recovery_required``
  methods to move them out of ``RUNNING`` — a human/engine decision, not
  an automatic one.
- Transitions are minimal and validated: ``RUNNING`` may move to exactly
  one of ``SUCCEEDED``/``FAILED``/``INTERRUPTED``/``RECOVERY_REQUIRED``;
  every other status is terminal and can never be transitioned out of,
  preventing a terminal execution from being silently reopened.
- ``permission_mode`` (P12, ROADMAP.md §13) is an identity field like
  ``worker_id``/``role`` — set once at ``create()``, exactly reflecting
  the requesting ``RalphExecutionEngine``'s own configured
  ``ExecutionPermissionMode`` (or ``None`` if that engine was not
  configured with one), never changed by a transition. Stored as a plain
  nullable string, not an enum — this module has no dependency on
  ``orchestrator.execution_policy``. A database created before this field
  existed is migrated in place, idempotently, by adding the column if
  missing (``_ensure_permission_mode_column``); its pre-existing rows
  decode with ``permission_mode=None`` — never fabricated as
  ``"standard"``/``"unrestricted"`` after the fact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

Clock = Callable[[], datetime]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware datetime, got {moment!r}"
        )


class ExecutionStatus(str, Enum):
    """Minimal, explicit execution audit states.

    ``RUNNING`` is the only non-terminal state in this slice. Every other
    member is terminal. ``WAITING_RESET`` is deliberately absent — it is a
    future orchestration/policy state, not an execution audit fact.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_REQUIRED = "recovery_required"


_ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.INTERRUPTED,
            ExecutionStatus.RECOVERY_REQUIRED,
        }
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.INTERRUPTED: frozenset(),
    ExecutionStatus.RECOVERY_REQUIRED: frozenset(),
}

_OPTIONAL_STR_FIELDS = (
    "reasoning_effort", "provider_session_id", "ralph_loop_id",
    "git_sha_before", "git_sha_after", "permission_mode",
)


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """An immutable snapshot of one execution's identity and current audit state."""

    execution_id: str
    task_id: str
    worker_id: str
    provider: str
    backend: str
    model: str
    role: str
    started_at: datetime
    status: ExecutionStatus
    reasoning_effort: str | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    provider_session_id: str | None = None
    ralph_loop_id: str | None = None
    git_sha_before: str | None = None
    git_sha_after: str | None = None
    permission_mode: str | None = None

    def __post_init__(self) -> None:
        for name in ("execution_id", "task_id", "worker_id", "provider", "backend", "model", "role"):
            _require_non_empty_str(getattr(self, name), field_name=f"ExecutionRecord.{name}")
        _require_aware(self.started_at, field_name="ExecutionRecord.started_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, field_name="ExecutionRecord.finished_at")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError(f"ExecutionRecord.status must be an ExecutionStatus, got {type(self.status)!r}")
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise TypeError(f"ExecutionRecord.exit_code must be an int or None, got {type(self.exit_code)!r}")
        for name in _OPTIONAL_STR_FIELDS:
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ExecutionRecord.{name}")


class ExecutionStoreError(Exception):
    """Base for ExecutionStore domain errors."""


class UnknownExecutionError(ExecutionStoreError):
    """Raised when an execution_id is not found in the store."""

    def __init__(self, execution_id: str) -> None:
        super().__init__(f"unknown execution: {execution_id!r}")
        self.execution_id = execution_id


class DuplicateExecutionError(ExecutionStoreError):
    """Raised when create() is called with an already-existing execution_id."""

    def __init__(self, execution_id: str) -> None:
        super().__init__(f"execution already exists: {execution_id!r}")
        self.execution_id = execution_id


class InvalidTransitionError(ExecutionStoreError):
    """Raised when a status transition is not allowed from the current status."""

    def __init__(self, execution_id: str, current: ExecutionStatus, attempted: ExecutionStatus) -> None:
        super().__init__(
            f"execution {execution_id!r} cannot move from {current.value!r} to "
            f"{attempted.value!r} — {current.value!r} is terminal or the transition is not allowed"
        )
        self.execution_id = execution_id
        self.current = current
        self.attempted = attempted


class CorruptExecutionRecordError(ExecutionStoreError):
    """Raised when a stored row cannot be decoded into a valid ExecutionRecord."""

    def __init__(self, execution_id: str, detail: str) -> None:
        super().__init__(f"corrupt execution record {execution_id!r}: {detail}")
        self.execution_id = execution_id


_COLUMNS = (
    "execution_id", "task_id", "worker_id", "provider", "backend", "model", "role",
    "reasoning_effort", "started_at", "finished_at", "status", "exit_code",
    "provider_session_id", "ralph_loop_id", "git_sha_before", "git_sha_after",
    "permission_mode",
)

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    reasoning_effort TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    provider_session_id TEXT,
    ralph_loop_id TEXT,
    git_sha_before TEXT,
    git_sha_after TEXT,
    permission_mode TEXT
)
"""

_INSERT_SQL = (
    f"INSERT INTO executions ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)

_SELECT_BY_ID_SQL = "SELECT * FROM executions WHERE execution_id = ?"
_SELECT_RUNNING_SQL = "SELECT * FROM executions WHERE status = ? ORDER BY started_at ASC"
_UPDATE_TRANSITION_SQL = (
    "UPDATE executions SET status = ?, exit_code = ?, finished_at = ?, git_sha_after = ?, "
    "provider_session_id = ?, ralph_loop_id = ? WHERE execution_id = ?"
)


def _encode_insert(record: ExecutionRecord) -> tuple:
    return (
        record.execution_id, record.task_id, record.worker_id, record.provider,
        record.backend, record.model, record.role, record.reasoning_effort,
        record.started_at.isoformat(),
        None if record.finished_at is None else record.finished_at.isoformat(),
        record.status.value, record.exit_code, record.provider_session_id,
        record.ralph_loop_id, record.git_sha_before, record.git_sha_after,
        record.permission_mode,
    )


def _decode_row(row: sqlite3.Row) -> ExecutionRecord:
    execution_id = row["execution_id"]
    row_keys = row.keys()
    try:
        return ExecutionRecord(
            execution_id=execution_id,
            task_id=row["task_id"],
            worker_id=row["worker_id"],
            provider=row["provider"],
            backend=row["backend"],
            model=row["model"],
            role=row["role"],
            reasoning_effort=row["reasoning_effort"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=None if row["finished_at"] is None else datetime.fromisoformat(row["finished_at"]),
            status=ExecutionStatus(row["status"]),
            exit_code=row["exit_code"],
            provider_session_id=row["provider_session_id"],
            ralph_loop_id=row["ralph_loop_id"],
            git_sha_before=row["git_sha_before"],
            git_sha_after=row["git_sha_after"],
            # Absent on a row read via a pre-migration column set (should
            # not happen after __init__'s migration, but decoded honestly
            # as unknown/None rather than raising, matching a historical
            # row that never recorded a permission mode).
            permission_mode=row["permission_mode"] if "permission_mode" in row_keys else None,
        )
    except (ValueError, TypeError) as exc:
        raise CorruptExecutionRecordError(execution_id, str(exc)) from exc


class ExecutionStore:
    """Synchronous, sqlite3-backed audit store for ExecutionRecord."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)
            self._ensure_permission_mode_column()

    def _ensure_permission_mode_column(self) -> None:
        """Idempotent migration for a v0.1.1-era database created before
        ``permission_mode`` existed — ``CREATE TABLE IF NOT EXISTS`` above
        never adds a column to an already-existing table, so this adds it
        explicitly the first time it is missing. A no-op on every
        subsequent open."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(executions)")}
        if "permission_mode" not in existing:
            self._conn.execute("ALTER TABLE executions ADD COLUMN permission_mode TEXT")

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        *,
        execution_id: str,
        task_id: str,
        worker_id: str,
        provider: str,
        backend: str,
        model: str,
        role: str,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        ralph_loop_id: str | None = None,
        git_sha_before: str | None = None,
        started_at: datetime | None = None,
        permission_mode: str | None = None,
    ) -> ExecutionRecord:
        """Create a new execution, recorded as RUNNING from the start.

        ``permission_mode`` is a plain, optional string snapshot of the
        caller's ``ExecutionPermissionMode.value`` (P12) — this module
        never imports ``orchestrator.execution_policy`` itself, it only
        stores/returns exactly what it is given, honestly.
        """
        record = ExecutionRecord(
            execution_id=execution_id,
            task_id=task_id,
            worker_id=worker_id,
            provider=provider,
            backend=backend,
            model=model,
            role=role,
            reasoning_effort=reasoning_effort,
            started_at=started_at if started_at is not None else self._now(),
            status=ExecutionStatus.RUNNING,
            provider_session_id=provider_session_id,
            ralph_loop_id=ralph_loop_id,
            git_sha_before=git_sha_before,
            permission_mode=permission_mode,
        )
        try:
            with self._conn:
                self._conn.execute(_INSERT_SQL, _encode_insert(record))
        except sqlite3.IntegrityError as exc:
            raise DuplicateExecutionError(execution_id) from exc
        return record

    def get(self, execution_id: str) -> ExecutionRecord:
        row = self._conn.execute(_SELECT_BY_ID_SQL, (execution_id,)).fetchone()
        if row is None:
            raise UnknownExecutionError(execution_id)
        return _decode_row(row)

    def list_running(self) -> list[ExecutionRecord]:
        """Executions still RUNNING — the only non-terminal, incomplete state.

        A caller (never this module) decides what to do with them: this is
        the read primitive recovery/reconciliation needs, not recovery
        itself. No retry, automatic or otherwise, happens here.
        """
        rows = self._conn.execute(_SELECT_RUNNING_SQL, (ExecutionStatus.RUNNING.value,)).fetchall()
        return [_decode_row(row) for row in rows]

    def list_for_task(self, task_id: str) -> list[ExecutionRecord]:
        """All executions recorded for a task (WorkItem), oldest first.

        The read primitive an activity report (Slice 10) needs to see a
        WorkItem's full execution history — never log scraping.
        """
        rows = self._conn.execute(
            "SELECT * FROM executions WHERE task_id = ? ORDER BY started_at ASC", (task_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def mark_succeeded(
        self,
        execution_id: str,
        *,
        exit_code: int | None = None,
        git_sha_after: str | None = None,
        provider_session_id: str | None = None,
        ralph_loop_id: str | None = None,
        finished_at: datetime | None = None,
    ) -> ExecutionRecord:
        return self._transition(
            execution_id, ExecutionStatus.SUCCEEDED,
            exit_code=exit_code, git_sha_after=git_sha_after,
            provider_session_id=provider_session_id, ralph_loop_id=ralph_loop_id,
            finished_at=finished_at,
        )

    def mark_failed(
        self,
        execution_id: str,
        *,
        exit_code: int | None = None,
        git_sha_after: str | None = None,
        provider_session_id: str | None = None,
        ralph_loop_id: str | None = None,
        finished_at: datetime | None = None,
    ) -> ExecutionRecord:
        return self._transition(
            execution_id, ExecutionStatus.FAILED,
            exit_code=exit_code, git_sha_after=git_sha_after,
            provider_session_id=provider_session_id, ralph_loop_id=ralph_loop_id,
            finished_at=finished_at,
        )

    def mark_interrupted(
        self,
        execution_id: str,
        *,
        ralph_loop_id: str | None = None,
        finished_at: datetime | None = None,
    ) -> ExecutionRecord:
        return self._transition(
            execution_id, ExecutionStatus.INTERRUPTED,
            ralph_loop_id=ralph_loop_id, finished_at=finished_at,
        )

    def mark_recovery_required(
        self, execution_id: str, *, finished_at: datetime | None = None
    ) -> ExecutionRecord:
        return self._transition(execution_id, ExecutionStatus.RECOVERY_REQUIRED, finished_at=finished_at)

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="ExecutionStore clock()")
        return value

    def _transition(
        self,
        execution_id: str,
        new_status: ExecutionStatus,
        *,
        exit_code: int | None = None,
        git_sha_after: str | None = None,
        provider_session_id: str | None = None,
        ralph_loop_id: str | None = None,
        finished_at: datetime | None = None,
    ) -> ExecutionRecord:
        current = self.get(execution_id)
        allowed = _ALLOWED_TRANSITIONS.get(current.status, frozenset())
        if new_status not in allowed:
            raise InvalidTransitionError(execution_id, current.status, new_status)

        updated = replace(
            current,
            status=new_status,
            exit_code=exit_code if exit_code is not None else current.exit_code,
            git_sha_after=git_sha_after if git_sha_after is not None else current.git_sha_after,
            provider_session_id=(
                provider_session_id if provider_session_id is not None else current.provider_session_id
            ),
            ralph_loop_id=ralph_loop_id if ralph_loop_id is not None else current.ralph_loop_id,
            finished_at=finished_at if finished_at is not None else self._now(),
        )
        with self._conn:
            self._conn.execute(
                _UPDATE_TRANSITION_SQL,
                (
                    updated.status.value,
                    updated.exit_code,
                    updated.finished_at.isoformat(),
                    updated.git_sha_after,
                    updated.provider_session_id,
                    updated.ralph_loop_id,
                    execution_id,
                ),
            )
        return updated
