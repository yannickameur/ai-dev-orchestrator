"""Release Gate — ReleaseCheck/ReleaseGateResult/ReleaseRecord/ReleaseStore (Slice 10).

This module holds the durable record of one release-gate evaluation
attempt. It never decides anything itself and never runs a worker — that
is :class:`~orchestrator.release_manager.ReleaseManager`'s job, which
composes the existing stores (``ProjectStateStore``, ``ExecutionStore``,
``ValidationStore``, ``ReviewStore``) to build the checks this module only
represents and persists.

Design invariants:

- FAIL-CLOSED: a release is never PASSED by default. Every check must
  positively confirm its condition; an absence of proof (no gate result,
  no review, a dangling RUNNING execution) always fails its check.
- ERROR is distinct from FAILED: FAILED means every check was evaluated
  and at least one genuinely did not hold; ERROR means the evaluation
  itself could not be trusted (e.g. corrupt persisted data). Neither is
  ever treated as PASSED.
- A release attempt is never overwritten: multiple attempts for the same
  MVP are all kept (each gets its own ``release_id``), so a failed attempt
  is never silently lost.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
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
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class ReleaseGateStatus(str, Enum):
    """Never PASSED unless every check genuinely confirmed its condition."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    """One named, structured piece of evidence for or against a release."""

    check_id: str
    passed: bool
    summary: str
    related_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.check_id, field_name="ReleaseCheck.check_id")
        _require_non_empty_str(self.summary, field_name="ReleaseCheck.summary")
        object.__setattr__(self, "related_ids", tuple(self.related_ids))


@dataclass(frozen=True, slots=True)
class ReleaseGateResult:
    """The aggregated outcome of evaluating a release gate once."""

    status: ReleaseGateStatus
    checks: tuple[ReleaseCheck, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReleaseGateStatus):
            raise TypeError(f"ReleaseGateResult.status must be a ReleaseGateStatus, got {type(self.status)!r}")
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def passed(self) -> bool:
        return self.status is ReleaseGateStatus.PASSED


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    """One durable release-gate attempt for an MVP."""

    release_id: str
    project_id: str
    mvp_id: str
    started_at: datetime
    finished_at: datetime
    status: ReleaseGateStatus
    checks: tuple[ReleaseCheck, ...] = ()
    git_sha: str | None = None
    report_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("release_id", "project_id", "mvp_id"):
            _require_non_empty_str(getattr(self, name), field_name=f"ReleaseRecord.{name}")
        _require_aware(self.started_at, field_name="ReleaseRecord.started_at")
        _require_aware(self.finished_at, field_name="ReleaseRecord.finished_at")
        if not isinstance(self.status, ReleaseGateStatus):
            raise TypeError(f"ReleaseRecord.status must be a ReleaseGateStatus, got {type(self.status)!r}")
        object.__setattr__(self, "checks", tuple(self.checks))
        for name in ("git_sha", "report_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ReleaseRecord.{name}")


class ReleaseStoreError(Exception):
    """Base for ReleaseStore domain errors."""


class UnknownReleaseError(ReleaseStoreError):
    def __init__(self, release_id: str) -> None:
        super().__init__(f"unknown release: {release_id!r}")
        self.release_id = release_id


class DuplicateReleaseError(ReleaseStoreError):
    def __init__(self, release_id: str) -> None:
        super().__init__(f"release already exists: {release_id!r}")
        self.release_id = release_id


class CorruptReleaseRecordError(ReleaseStoreError):
    def __init__(self, release_id: str, detail: str) -> None:
        super().__init__(f"corrupt release record {release_id!r}: {detail}")
        self.release_id = release_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    checks TEXT NOT NULL,
    git_sha TEXT,
    report_id TEXT
)
"""


def _encode_check(check: ReleaseCheck) -> dict:
    return {
        "check_id": check.check_id, "passed": check.passed, "summary": check.summary,
        "related_ids": list(check.related_ids),
    }


def _decode_row(row: sqlite3.Row) -> ReleaseRecord:
    release_id = row["release_id"]
    try:
        checks = tuple(
            ReleaseCheck(
                check_id=item["check_id"], passed=bool(item["passed"]), summary=item["summary"],
                related_ids=tuple(item.get("related_ids", ())),
            )
            for item in json.loads(row["checks"])
        )
        return ReleaseRecord(
            release_id=release_id, project_id=row["project_id"], mvp_id=row["mvp_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            status=ReleaseGateStatus(row["status"]), checks=checks,
            git_sha=row["git_sha"], report_id=row["report_id"],
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise CorruptReleaseRecordError(release_id, str(exc)) from exc


class ReleaseStore:
    """Synchronous, sqlite3-backed store for durable ReleaseRecords."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def record(self, release: ReleaseRecord) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO releases (release_id, project_id, mvp_id, started_at, finished_at, "
                    "status, checks, git_sha, report_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        release.release_id, release.project_id, release.mvp_id,
                        release.started_at.isoformat(), release.finished_at.isoformat(),
                        release.status.value, json.dumps([_encode_check(c) for c in release.checks]),
                        release.git_sha, release.report_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateReleaseError(release.release_id) from exc

    def get(self, release_id: str) -> ReleaseRecord:
        row = self._conn.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        if row is None:
            raise UnknownReleaseError(release_id)
        return _decode_row(row)

    def list_for_mvp(self, mvp_id: str) -> list[ReleaseRecord]:
        """All release attempts for an MVP, oldest first — none ever overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM releases WHERE mvp_id = ? ORDER BY started_at ASC", (mvp_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def latest_for_mvp(self, mvp_id: str) -> ReleaseRecord | None:
        releases = self.list_for_mvp(mvp_id)
        return releases[-1] if releases else None
