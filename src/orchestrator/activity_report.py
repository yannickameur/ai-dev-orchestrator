"""ActivityReport — a durable, factual snapshot of one MVP's release (Slice 10).

This module never invents content. Every field is aggregated from the
existing stores (``ProjectStateStore``, ``ExecutionStore``,
``ValidationStore``, ``ReviewStore``, ``HandoffStore``) by
:class:`~orchestrator.release_manager.ReleaseManager`, at the moment a
release gate PASSES — never from stdout/log scraping, and never written
by an LLM. ``render_markdown`` is a pure, deterministic function over an
already-built ``ActivityReport``.

The report is a snapshot, not a live mirror: it captures the state of
each source store at generation time and persists that verbatim (as one
row per report), so it stays stable even if the underlying stores keep
accumulating data afterwards for other work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Clock = Callable[[], datetime]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


@dataclass(frozen=True, slots=True)
class WorkItemSummary:
    work_item_id: str
    title: str
    status: str
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    execution_id: str
    work_item_id: str
    worker_id: str
    provider: str
    backend: str
    model: str
    role: str
    started_at: datetime
    status: str
    reasoning_effort: str | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    git_sha_before: str | None = None
    git_sha_after: str | None = None
    provider_session_id: str | None = None
    ralph_loop_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    validation_run_id: str
    work_item_id: str
    validation_id: str
    kind: str
    required: bool
    status: str
    duration_ms: int
    exit_code: int | None = None
    git_sha: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    review_id: str
    work_item_id: str
    author_worker_id: str
    status: str
    cycle_number: int
    reviewer_worker_id: str | None = None
    reviewer_provider: str | None = None
    reviewer_model: str | None = None
    findings_summary: tuple[str, ...] = ()
    git_sha_reviewed: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffSummary:
    handoff_id: str
    work_item_id: str
    next_action: str | None = None


@dataclass(frozen=True, slots=True)
class Incident:
    work_item_id: str
    kind: str
    summary: str


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    work_item_count: int
    execution_count: int
    validation_count: int
    review_count: int
    rework_count: int
    failure_count: int
    interruption_count: int
    workers_used: tuple[str, ...] = ()
    providers_used: tuple[str, ...] = ()
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ReleaseCheckSummary:
    """A minimal, local mirror of orchestrator.release.ReleaseCheck.

    Kept independent (rather than importing release.ReleaseCheck) so this
    module has no dependency direction on release.py — release_manager.py
    is the only place that needs to know about both.
    """

    check_id: str
    passed: bool
    summary: str


@dataclass(frozen=True, slots=True)
class ActivityReport:
    """A durable, factual snapshot of one MVP release — never LLM-authored."""

    report_id: str
    project_id: str
    mvp_id: str
    release_id: str
    generated_at: datetime
    mvp_objective: str
    mvp_final_status: str
    summary: ActivitySummary
    mvp_acceptance_criteria: tuple[str, ...] = ()
    git_sha: str | None = None
    work_items: tuple[WorkItemSummary, ...] = ()
    executions: tuple[ExecutionSummary, ...] = ()
    validations: tuple[ValidationSummary, ...] = ()
    reviews: tuple[ReviewSummary, ...] = ()
    handoffs: tuple[HandoffSummary, ...] = ()
    incidents: tuple[Incident, ...] = ()
    gate_checks: tuple[ReleaseCheckSummary, ...] = ()

    def __post_init__(self) -> None:
        for name in ("report_id", "project_id", "mvp_id", "release_id", "mvp_objective", "mvp_final_status"):
            _require_non_empty_str(getattr(self, name), field_name=f"ActivityReport.{name}")
        _require_aware(self.generated_at, field_name="ActivityReport.generated_at")
        if not isinstance(self.summary, ActivitySummary):
            raise TypeError(f"ActivityReport.summary must be an ActivitySummary, got {type(self.summary)!r}")


class ActivityReportStoreError(Exception):
    """Base for ActivityReportStore domain errors."""


class UnknownActivityReportError(ActivityReportStoreError):
    def __init__(self, report_id: str) -> None:
        super().__init__(f"unknown activity report: {report_id!r}")
        self.report_id = report_id


class CorruptActivityReportError(ActivityReportStoreError):
    def __init__(self, report_id: str, detail: str) -> None:
        super().__init__(f"corrupt activity report {report_id!r}: {detail}")
        self.report_id = report_id


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS activity_reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize(report: ActivityReport) -> str:
    payload = {
        "report_id": report.report_id, "project_id": report.project_id, "mvp_id": report.mvp_id,
        "release_id": report.release_id, "generated_at": report.generated_at.isoformat(),
        "mvp_objective": report.mvp_objective, "mvp_final_status": report.mvp_final_status,
        "mvp_acceptance_criteria": list(report.mvp_acceptance_criteria), "git_sha": report.git_sha,
        "work_items": [
            {
                "work_item_id": w.work_item_id, "title": w.title, "status": w.status,
                "dependencies": list(w.dependencies), "acceptance_criteria": list(w.acceptance_criteria),
            }
            for w in report.work_items
        ],
        "executions": [
            {
                "execution_id": e.execution_id, "work_item_id": e.work_item_id, "worker_id": e.worker_id,
                "provider": e.provider, "backend": e.backend, "model": e.model,
                "reasoning_effort": e.reasoning_effort, "role": e.role,
                "started_at": e.started_at.isoformat(), "finished_at": _dt(e.finished_at),
                "status": e.status, "exit_code": e.exit_code, "git_sha_before": e.git_sha_before,
                "git_sha_after": e.git_sha_after, "provider_session_id": e.provider_session_id,
                "ralph_loop_id": e.ralph_loop_id,
            }
            for e in report.executions
        ],
        "validations": [
            {
                "validation_run_id": v.validation_run_id, "work_item_id": v.work_item_id,
                "validation_id": v.validation_id, "kind": v.kind, "required": v.required,
                "status": v.status, "duration_ms": v.duration_ms, "exit_code": v.exit_code,
                "git_sha": v.git_sha,
            }
            for v in report.validations
        ],
        "reviews": [
            {
                "review_id": r.review_id, "work_item_id": r.work_item_id,
                "author_worker_id": r.author_worker_id, "status": r.status, "cycle_number": r.cycle_number,
                "reviewer_worker_id": r.reviewer_worker_id, "reviewer_provider": r.reviewer_provider,
                "reviewer_model": r.reviewer_model, "findings_summary": list(r.findings_summary),
                "git_sha_reviewed": r.git_sha_reviewed,
            }
            for r in report.reviews
        ],
        "handoffs": [
            {"handoff_id": h.handoff_id, "work_item_id": h.work_item_id, "next_action": h.next_action}
            for h in report.handoffs
        ],
        "incidents": [
            {"work_item_id": i.work_item_id, "kind": i.kind, "summary": i.summary}
            for i in report.incidents
        ],
        "gate_checks": [
            {"check_id": c.check_id, "passed": c.passed, "summary": c.summary} for c in report.gate_checks
        ],
        "summary": {
            "work_item_count": report.summary.work_item_count,
            "execution_count": report.summary.execution_count,
            "validation_count": report.summary.validation_count,
            "review_count": report.summary.review_count,
            "rework_count": report.summary.rework_count,
            "failure_count": report.summary.failure_count,
            "interruption_count": report.summary.interruption_count,
            "workers_used": list(report.summary.workers_used),
            "providers_used": list(report.summary.providers_used),
            "duration_seconds": report.summary.duration_seconds,
        },
    }
    return json.dumps(payload)


def _deserialize(report_id: str, raw: str) -> ActivityReport:
    try:
        data = json.loads(raw)
        return ActivityReport(
            report_id=data["report_id"], project_id=data["project_id"], mvp_id=data["mvp_id"],
            release_id=data["release_id"], generated_at=datetime.fromisoformat(data["generated_at"]),
            mvp_objective=data["mvp_objective"], mvp_final_status=data["mvp_final_status"],
            mvp_acceptance_criteria=tuple(data["mvp_acceptance_criteria"]), git_sha=data["git_sha"],
            work_items=tuple(
                WorkItemSummary(
                    work_item_id=w["work_item_id"], title=w["title"], status=w["status"],
                    dependencies=tuple(w["dependencies"]),
                    acceptance_criteria=tuple(w["acceptance_criteria"]),
                )
                for w in data["work_items"]
            ),
            executions=tuple(
                ExecutionSummary(
                    execution_id=e["execution_id"], work_item_id=e["work_item_id"],
                    worker_id=e["worker_id"], provider=e["provider"], backend=e["backend"],
                    model=e["model"], reasoning_effort=e["reasoning_effort"], role=e["role"],
                    started_at=datetime.fromisoformat(e["started_at"]),
                    finished_at=datetime.fromisoformat(e["finished_at"]) if e["finished_at"] else None,
                    status=e["status"], exit_code=e["exit_code"], git_sha_before=e["git_sha_before"],
                    git_sha_after=e["git_sha_after"], provider_session_id=e["provider_session_id"],
                    ralph_loop_id=e["ralph_loop_id"],
                )
                for e in data["executions"]
            ),
            validations=tuple(
                ValidationSummary(
                    validation_run_id=v["validation_run_id"], work_item_id=v["work_item_id"],
                    validation_id=v["validation_id"], kind=v["kind"], required=v["required"],
                    status=v["status"], duration_ms=v["duration_ms"], exit_code=v["exit_code"],
                    git_sha=v["git_sha"],
                )
                for v in data["validations"]
            ),
            reviews=tuple(
                ReviewSummary(
                    review_id=r["review_id"], work_item_id=r["work_item_id"],
                    author_worker_id=r["author_worker_id"], status=r["status"],
                    cycle_number=r["cycle_number"], reviewer_worker_id=r["reviewer_worker_id"],
                    reviewer_provider=r["reviewer_provider"], reviewer_model=r["reviewer_model"],
                    findings_summary=tuple(r["findings_summary"]),
                    git_sha_reviewed=r["git_sha_reviewed"],
                )
                for r in data["reviews"]
            ),
            handoffs=tuple(
                HandoffSummary(handoff_id=h["handoff_id"], work_item_id=h["work_item_id"], next_action=h["next_action"])
                for h in data["handoffs"]
            ),
            incidents=tuple(
                Incident(work_item_id=i["work_item_id"], kind=i["kind"], summary=i["summary"])
                for i in data["incidents"]
            ),
            gate_checks=tuple(
                ReleaseCheckSummary(check_id=c["check_id"], passed=c["passed"], summary=c["summary"])
                for c in data["gate_checks"]
            ),
            summary=ActivitySummary(
                work_item_count=data["summary"]["work_item_count"],
                execution_count=data["summary"]["execution_count"],
                validation_count=data["summary"]["validation_count"],
                review_count=data["summary"]["review_count"],
                rework_count=data["summary"]["rework_count"],
                failure_count=data["summary"]["failure_count"],
                interruption_count=data["summary"]["interruption_count"],
                workers_used=tuple(data["summary"]["workers_used"]),
                providers_used=tuple(data["summary"]["providers_used"]),
                duration_seconds=data["summary"]["duration_seconds"],
            ),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise CorruptActivityReportError(report_id, str(exc)) from exc


class ActivityReportStore:
    """Synchronous, sqlite3-backed store for durable ActivityReport snapshots."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)

    def close(self) -> None:
        self._conn.close()

    def record(self, report: ActivityReport) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO activity_reports "
                "(report_id, project_id, mvp_id, release_id, generated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report.report_id, report.project_id, report.mvp_id, report.release_id,
                    report.generated_at.isoformat(), _serialize(report),
                ),
            )

    def get(self, report_id: str) -> ActivityReport:
        row = self._conn.execute(
            "SELECT * FROM activity_reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise UnknownActivityReportError(report_id)
        return _deserialize(report_id, row["payload"])

    def latest_for_mvp(self, mvp_id: str) -> ActivityReport | None:
        row = self._conn.execute(
            "SELECT * FROM activity_reports WHERE mvp_id = ? ORDER BY generated_at DESC LIMIT 1",
            (mvp_id,),
        ).fetchone()
        if row is None:
            return None
        return _deserialize(row["report_id"], row["payload"])


def render_markdown(report: ActivityReport) -> str:
    """Deterministic, pure Markdown rendering — no LLM involved, ever."""
    lines = [
        f"# MVP {report.mvp_id} — Release Report",
        "",
        "## Summary",
        "",
        f"- Release ID: {report.release_id}",
        f"- Generated at: {report.generated_at.isoformat()}",
        f"- Git SHA: {report.git_sha or 'n/a'}",
        f"- Objective: {report.mvp_objective}",
        f"- Final status: {report.mvp_final_status}",
        f"- Work items: {report.summary.work_item_count}",
        f"- Executions: {report.summary.execution_count}",
        f"- Validations: {report.summary.validation_count}",
        f"- Reviews: {report.summary.review_count}",
        f"- Reworks: {report.summary.rework_count}",
        f"- Failures: {report.summary.failure_count}",
        f"- Interruptions: {report.summary.interruption_count}",
        f"- Workers used: {', '.join(report.summary.workers_used) or 'none'}",
        f"- Providers used: {', '.join(report.summary.providers_used) or 'none'}",
        "- Duration (s): "
        + (f"{report.summary.duration_seconds:.1f}" if report.summary.duration_seconds is not None else "n/a"),
        "",
        "## WorkItems",
        "",
    ]
    for w in report.work_items:
        lines.append(f"- `{w.work_item_id}` — {w.title} — status={w.status}")

    lines += ["", "## Executions", ""]
    for e in report.executions:
        lines.append(
            f"- `{e.execution_id}` ({e.work_item_id}): {e.worker_id} / {e.provider} / {e.model} "
            f"role={e.role} status={e.status} exit_code={e.exit_code}"
        )

    lines += ["", "## Quality Gates", ""]
    for v in report.validations:
        lines.append(
            f"- `{v.validation_run_id}` ({v.work_item_id}): {v.validation_id} [{v.kind}] "
            f"required={v.required} status={v.status}"
        )

    lines += ["", "## Reviews", ""]
    for r in report.reviews:
        lines.append(
            f"- `{r.review_id}` ({r.work_item_id}) cycle #{r.cycle_number}: "
            f"author={r.author_worker_id} reviewer={r.reviewer_worker_id or 'n/a'} status={r.status}"
        )
        for finding in r.findings_summary:
            lines.append(f"  - finding: {finding}")

    lines += ["", "## Handoffs", ""]
    for h in report.handoffs:
        lines.append(f"- `{h.handoff_id}` ({h.work_item_id}): next_action={h.next_action or 'n/a'}")

    lines += ["", "## Incidents", ""]
    if report.incidents:
        for i in report.incidents:
            lines.append(f"- [{i.kind}] {i.work_item_id}: {i.summary}")
    else:
        lines.append("(none)")

    lines += ["", "## Release Gate", ""]
    for c in report.gate_checks:
        lines.append(f"- [{'PASS' if c.passed else 'FAIL'}] {c.check_id}: {c.summary}")

    return "\n".join(lines) + "\n"
