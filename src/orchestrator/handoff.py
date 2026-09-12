"""HandoffRecord — durable, structured handoff between workers/sessions.

This module answers exactly one question: "what does the next worker (or
session, or human) need to know to continue this WorkItem, without relying
on the previous worker's conversational memory?" It never launches
anything and never decides what happens next — it is a durable fact store,
read/written by :class:`~orchestrator.mvp_manager.MVPManager`.

Design invariants:

- A handoff must be reconstructible from persistent facts (an
  `ExecutionRecord`, business events, a git SHA) — never trust-only a free
  LLM summary. In Slice 7, not every fact is collected yet (no test runner,
  no review): fields that are not genuinely known stay ``None``/empty
  rather than being fabricated. Later slices (tests, review) enrich the
  same contract without needing to change it.
- Multiple handoffs may exist for the same WorkItem (e.g. one per attempt);
  ``latest_for_work_item`` is how a caller finds the most recent one after
  a restart.
- No secrets: nothing here stores API keys/tokens — only project-domain
  facts (objective, decisions, file paths, test names, a git SHA).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

Clock = Callable[[], datetime]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _as_tuple_of_str(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return result


_OPTIONAL_STR_FIELDS = (
    "execution_id", "worker_id", "completed_work", "decisions", "test_results",
    "open_issues", "risks", "next_action", "git_sha_after",
)


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """A structured, durable handoff for one WorkItem attempt."""

    handoff_id: str
    project_id: str
    mvp_id: str
    work_item_id: str
    created_at: datetime
    objective: str
    execution_id: str | None = None
    worker_id: str | None = None
    completed_work: str | None = None
    decisions: str | None = None
    files_touched: tuple[str, ...] = ()
    tests_run: tuple[str, ...] = ()
    test_results: str | None = None
    open_issues: str | None = None
    risks: str | None = None
    next_action: str | None = None
    git_sha_after: str | None = None

    def __post_init__(self) -> None:
        for name in ("handoff_id", "project_id", "mvp_id", "work_item_id", "objective"):
            _require_non_empty_str(getattr(self, name), field_name=f"HandoffRecord.{name}")
        _require_aware(self.created_at, field_name="HandoffRecord.created_at")
        object.__setattr__(
            self, "files_touched",
            _as_tuple_of_str(self.files_touched, field_name="HandoffRecord.files_touched"),
        )
        object.__setattr__(
            self, "tests_run",
            _as_tuple_of_str(self.tests_run, field_name="HandoffRecord.tests_run"),
        )
        for name in _OPTIONAL_STR_FIELDS:
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"HandoffRecord.{name}")


class HandoffStoreError(Exception):
    """Base for HandoffStore domain errors."""


class UnknownHandoffError(HandoffStoreError):
    def __init__(self, handoff_id: str) -> None:
        super().__init__(f"unknown handoff: {handoff_id!r}")
        self.handoff_id = handoff_id


class DuplicateHandoffError(HandoffStoreError):
    def __init__(self, handoff_id: str) -> None:
        super().__init__(f"handoff already exists: {handoff_id!r}")
        self.handoff_id = handoff_id


class CorruptHandoffRecordError(HandoffStoreError):
    def __init__(self, handoff_id: str, detail: str) -> None:
        super().__init__(f"corrupt handoff record {handoff_id!r}: {detail}")
        self.handoff_id = handoff_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    objective TEXT NOT NULL,
    execution_id TEXT,
    worker_id TEXT,
    completed_work TEXT,
    decisions TEXT,
    files_touched TEXT NOT NULL,
    tests_run TEXT NOT NULL,
    test_results TEXT,
    open_issues TEXT,
    risks TEXT,
    next_action TEXT,
    git_sha_after TEXT
)
"""

_COLUMNS = (
    "handoff_id", "project_id", "mvp_id", "work_item_id", "created_at", "objective",
    "execution_id", "worker_id", "completed_work", "decisions", "files_touched",
    "tests_run", "test_results", "open_issues", "risks", "next_action", "git_sha_after",
)

_INSERT_SQL = (
    f"INSERT INTO handoffs ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


def _encode(record: HandoffRecord) -> tuple:
    return (
        record.handoff_id, record.project_id, record.mvp_id, record.work_item_id,
        record.created_at.isoformat(), record.objective, record.execution_id,
        record.worker_id, record.completed_work, record.decisions,
        json.dumps(record.files_touched), json.dumps(record.tests_run),
        record.test_results, record.open_issues, record.risks, record.next_action,
        record.git_sha_after,
    )


def _decode_row(row: sqlite3.Row) -> HandoffRecord:
    handoff_id = row["handoff_id"]
    try:
        return HandoffRecord(
            handoff_id=handoff_id,
            project_id=row["project_id"],
            mvp_id=row["mvp_id"],
            work_item_id=row["work_item_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            objective=row["objective"],
            execution_id=row["execution_id"],
            worker_id=row["worker_id"],
            completed_work=row["completed_work"],
            decisions=row["decisions"],
            files_touched=tuple(json.loads(row["files_touched"])),
            tests_run=tuple(json.loads(row["tests_run"])),
            test_results=row["test_results"],
            open_issues=row["open_issues"],
            risks=row["risks"],
            next_action=row["next_action"],
            git_sha_after=row["git_sha_after"],
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CorruptHandoffRecordError(handoff_id, str(exc)) from exc


class HandoffStore:
    """Synchronous, sqlite3-backed store for durable HandoffRecords."""

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
        handoff_id: str,
        project_id: str,
        mvp_id: str,
        work_item_id: str,
        objective: str,
        execution_id: str | None = None,
        worker_id: str | None = None,
        completed_work: str | None = None,
        decisions: str | None = None,
        files_touched: Iterable[str] = (),
        tests_run: Iterable[str] = (),
        test_results: str | None = None,
        open_issues: str | None = None,
        risks: str | None = None,
        next_action: str | None = None,
        git_sha_after: str | None = None,
        created_at: datetime | None = None,
    ) -> HandoffRecord:
        record = HandoffRecord(
            handoff_id=handoff_id, project_id=project_id, mvp_id=mvp_id,
            work_item_id=work_item_id, created_at=created_at or self._now(),
            objective=objective, execution_id=execution_id, worker_id=worker_id,
            completed_work=completed_work, decisions=decisions,
            files_touched=tuple(files_touched), tests_run=tuple(tests_run),
            test_results=test_results, open_issues=open_issues, risks=risks,
            next_action=next_action, git_sha_after=git_sha_after,
        )
        try:
            with self._conn:
                self._conn.execute(_INSERT_SQL, _encode(record))
        except sqlite3.IntegrityError as exc:
            raise DuplicateHandoffError(handoff_id) from exc
        return record

    def get(self, handoff_id: str) -> HandoffRecord:
        row = self._conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise UnknownHandoffError(handoff_id)
        return _decode_row(row)

    def list_for_work_item(self, work_item_id: str) -> list[HandoffRecord]:
        """All handoffs for a WorkItem, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM handoffs WHERE work_item_id = ? ORDER BY created_at ASC",
            (work_item_id,),
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def latest_for_work_item(self, work_item_id: str) -> HandoffRecord | None:
        handoffs = self.list_for_work_item(work_item_id)
        return handoffs[-1] if handoffs else None

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="HandoffStore clock()")
        return value
