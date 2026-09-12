"""Independent review — ReviewRecord/ReviewStore/ReviewPolicy (Slice 9).

This module holds the durable record of an independent review verdict. It
never selects a reviewer and never runs anything itself — that is
:class:`~orchestrator.mvp_manager.MVPManager`'s job, composing the existing
:class:`~orchestrator.worker_selector.WorkerSelector` (author-independence
is already fully enforced there, via ``WorkerSelectionPolicy`` and
``author_worker_id`` — this module does not duplicate that logic) and
:class:`~orchestrator.ralph_execution_engine.RalphExecutionEngine` (a
review is just another Ralph execution, never a direct
Claude/Codex/Ralph-subprocess call from here).

Design invariants:

- The verdict is never derived from free-text stdout. It comes from the
  business events a review execution was configured with
  (conventionally ``review.approved``/``review.rejected``) via
  ``RalphExecutionEngine``'s own fail-closed verdict logic: no reliable
  terminal business event is never treated as approval.
- Findings are parsed from the ``review.rejected`` payload under an
  explicit, documented contract (see ``parse_findings``): a JSON array of
  finding objects for structured output, or the raw string wrapped as one
  finding — matching the plain-string pattern already validated by the
  Phase 0.5 spike (``ralph emit "review.rejected" "<reason>"``).
- No secrets are ever stored here.
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


class ReviewStatus(str, Enum):
    """A review's terminal verdict. Never derived from free text."""

    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One reviewer-reported issue. ``severity`` is deliberately a free-form
    label (e.g. "blocker"/"major"/"minor"/"info"), not a rigid enum: it is
    advisory, human/LLM-facing text, and never drives this slice's control
    flow (only ``ReviewStatus`` does).
    """

    finding_id: str
    summary: str
    severity: str = "unknown"
    detail: str | None = None
    file_path: str | None = None
    line: int | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.finding_id, field_name="ReviewFinding.finding_id")
        _require_non_empty_str(self.summary, field_name="ReviewFinding.summary")
        _require_non_empty_str(self.severity, field_name="ReviewFinding.severity")
        for name in ("detail", "file_path", "category"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ReviewFinding.{name}")
        if self.line is not None and (not isinstance(self.line, int) or isinstance(self.line, bool)):
            raise TypeError(f"ReviewFinding.line must be an int or None, got {type(self.line)!r}")


def parse_findings(payload: str | None, finding_id_factory: Callable[[], str]) -> tuple[ReviewFinding, ...]:
    """Parses a ``review.rejected`` payload into structured findings.

    Contract: a JSON array of objects (fields: summary required;
    severity/detail/file_path/line/category optional) yields one finding
    per object. Anything else — plain text, the validated spike pattern —
    yields exactly one finding wrapping the raw payload as its summary.
    """
    if not payload:
        return ()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        findings = []
        for item in data:
            summary = item.get("summary") or payload
            findings.append(
                ReviewFinding(
                    finding_id=str(item.get("finding_id") or finding_id_factory()),
                    summary=str(summary),
                    severity=str(item.get("severity") or "unknown"),
                    detail=item.get("detail"),
                    file_path=item.get("file_path"),
                    line=item.get("line"),
                    category=item.get("category"),
                )
            )
        return tuple(findings)

    return (ReviewFinding(finding_id=finding_id_factory(), summary=payload),)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """The durable, structured outcome of one independent review attempt."""

    review_id: str
    project_id: str
    mvp_id: str
    work_item_id: str
    author_execution_id: str
    author_worker_id: str
    started_at: datetime
    finished_at: datetime
    status: ReviewStatus
    reviewer_execution_id: str | None = None
    reviewer_worker_id: str | None = None
    reviewer_provider: str | None = None
    reviewer_model: str | None = None
    findings: tuple[ReviewFinding, ...] = ()
    git_sha_reviewed: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "review_id", "project_id", "mvp_id", "work_item_id",
            "author_execution_id", "author_worker_id",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"ReviewRecord.{name}")
        _require_aware(self.started_at, field_name="ReviewRecord.started_at")
        _require_aware(self.finished_at, field_name="ReviewRecord.finished_at")
        if not isinstance(self.status, ReviewStatus):
            raise TypeError(f"ReviewRecord.status must be a ReviewStatus, got {type(self.status)!r}")
        for name in ("reviewer_execution_id", "reviewer_worker_id", "reviewer_provider", "reviewer_model", "git_sha_reviewed"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"ReviewRecord.{name}")
        object.__setattr__(self, "findings", tuple(self.findings))
        # The absolute invariant this whole slice exists to enforce.
        if self.reviewer_worker_id is not None and self.reviewer_worker_id == self.author_worker_id:
            raise ValueError(
                f"ReviewRecord invariant violated: reviewer_worker_id == author_worker_id "
                f"({self.author_worker_id!r}) — a worker can never review its own work"
            )


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """Bounds the review/rework loop. Cross-provider preference/requirement
    is deliberately NOT duplicated here: it already lives on the injected
    WorkerSelector's own ``WorkerSelectionPolicy`` (Slice 4) and this
    module/MVPManager never re-implements it.
    """

    max_review_cycles: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.max_review_cycles, int) or isinstance(self.max_review_cycles, bool) or self.max_review_cycles < 1:
            raise ValueError(
                f"ReviewPolicy.max_review_cycles must be a positive int, got {self.max_review_cycles!r}"
            )


class ReviewStoreError(Exception):
    """Base for ReviewStore domain errors."""


class UnknownReviewError(ReviewStoreError):
    def __init__(self, review_id: str) -> None:
        super().__init__(f"unknown review: {review_id!r}")
        self.review_id = review_id


class DuplicateReviewError(ReviewStoreError):
    def __init__(self, review_id: str) -> None:
        super().__init__(f"review already exists: {review_id!r}")
        self.review_id = review_id


class CorruptReviewRecordError(ReviewStoreError):
    def __init__(self, review_id: str, detail: str) -> None:
        super().__init__(f"corrupt review record {review_id!r}: {detail}")
        self.review_id = review_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    author_execution_id TEXT NOT NULL,
    author_worker_id TEXT NOT NULL,
    reviewer_execution_id TEXT,
    reviewer_worker_id TEXT,
    reviewer_provider TEXT,
    reviewer_model TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    findings TEXT NOT NULL,
    git_sha_reviewed TEXT
)
"""

_COLUMNS = (
    "review_id", "project_id", "mvp_id", "work_item_id", "author_execution_id",
    "author_worker_id", "reviewer_execution_id", "reviewer_worker_id", "reviewer_provider",
    "reviewer_model", "started_at", "finished_at", "status", "findings", "git_sha_reviewed",
)

_INSERT_SQL = f"INSERT INTO reviews ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})"


def _encode_finding(finding: ReviewFinding) -> dict:
    return {
        "finding_id": finding.finding_id, "summary": finding.summary, "severity": finding.severity,
        "detail": finding.detail, "file_path": finding.file_path, "line": finding.line,
        "category": finding.category,
    }


def _encode(review: ReviewRecord) -> tuple:
    return (
        review.review_id, review.project_id, review.mvp_id, review.work_item_id,
        review.author_execution_id, review.author_worker_id, review.reviewer_execution_id,
        review.reviewer_worker_id, review.reviewer_provider, review.reviewer_model,
        review.started_at.isoformat(), review.finished_at.isoformat(), review.status.value,
        json.dumps([_encode_finding(f) for f in review.findings]), review.git_sha_reviewed,
    )


def _decode_row(row: sqlite3.Row) -> ReviewRecord:
    review_id = row["review_id"]
    try:
        findings = tuple(
            ReviewFinding(
                finding_id=item["finding_id"], summary=item["summary"], severity=item["severity"],
                detail=item.get("detail"), file_path=item.get("file_path"), line=item.get("line"),
                category=item.get("category"),
            )
            for item in json.loads(row["findings"])
        )
        return ReviewRecord(
            review_id=review_id, project_id=row["project_id"], mvp_id=row["mvp_id"],
            work_item_id=row["work_item_id"], author_execution_id=row["author_execution_id"],
            author_worker_id=row["author_worker_id"], reviewer_execution_id=row["reviewer_execution_id"],
            reviewer_worker_id=row["reviewer_worker_id"], reviewer_provider=row["reviewer_provider"],
            reviewer_model=row["reviewer_model"], started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]), status=ReviewStatus(row["status"]),
            findings=findings, git_sha_reviewed=row["git_sha_reviewed"],
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise CorruptReviewRecordError(review_id, str(exc)) from exc


class ReviewStore:
    """Synchronous, sqlite3-backed store for durable ReviewRecords."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def record(self, review: ReviewRecord) -> None:
        try:
            with self._conn:
                self._conn.execute(_INSERT_SQL, _encode(review))
        except sqlite3.IntegrityError as exc:
            raise DuplicateReviewError(review.review_id) from exc

    def get(self, review_id: str) -> ReviewRecord:
        row = self._conn.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        if row is None:
            raise UnknownReviewError(review_id)
        return _decode_row(row)

    def list_for_work_item(self, work_item_id: str) -> list[ReviewRecord]:
        """All reviews for a WorkItem, oldest first — its review-cycle history."""
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE work_item_id = ? ORDER BY started_at ASC", (work_item_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def latest_for_work_item(self, work_item_id: str) -> ReviewRecord | None:
        reviews = self.list_for_work_item(work_item_id)
        return reviews[-1] if reviews else None

    def count_for_work_item(self, work_item_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        return int(row["n"])
