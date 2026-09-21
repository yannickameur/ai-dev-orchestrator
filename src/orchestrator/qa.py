"""QA governance domain — provider-independent contracts, persistence, and
deterministic verdict computation (Slice 22).

This module answers exactly one question: "given what a QA engine
observed, is this WorkItem's current head SHA actually PASS, FAIL, or
INCONCLUSIVE, as a matter of governance?" It never runs a real QA engine
(no ``InternalQAEngine``/``TestSpriteQAEngine``/etc. here — those are
Slice 23+), never wires into ``MVPManager``'s development/review/merge
cycle (Slice 24), and never touches ``compute_merge_eligibility`` or
``ReleaseManager`` (also Slice 24). This slice builds the domain only:
contracts, persistence, and the governance decision function a future
engine and a future merge/release integration will both depend on without
ever needing to change.

CENTRAL PRINCIPLE — ENGINE RESULT != GOVERNED VERDICT:

``QAResult`` is what a ``QAEngine`` observed — a normalized report, which
may itself carry a provider's own claimed status
(``QAResult.engine_reported_status``). That claim is **never** authoritative.
``QAVerdict`` is this module's own deterministic decision, computed by
``evaluate_qa_verdict`` purely from already-persisted facts (SHA match,
non-empty mandatory evidence, no unresolved regression, no unauthorized
protected-test mutation, no missing required engine, run status) — never
an LLM call, never trusting an engine's self-reported "PASS". See
``evaluate_qa_verdict`` and ``tests/test_qa.py::TestEngineResultNeverAuthoritative``.

Design invariants (mirroring ``validation.py``/``git_governance.py``):

- FAIL CLOSED: a technically-``FAILED``/``INTERRUPTED``/unfinished run, an
  empty mandatory evidence manifest, a SHA mismatch, an unresolved
  regression, or an unauthorized protected-test change all produce
  ``FAIL`` or ``INCONCLUSIVE`` — never ``PASS`` by default or by omission.
- POLICY/MANIFEST SNAPSHOT: exactly the same pattern as Slice 21.5's
  ``ValidationStore.record_manifest``/``get_manifest_for_run`` — the
  policy and evidence manifest actually applied to a ``QARun`` are
  snapshotted at creation time and never recomputed from
  possibly-since-changed live configuration.
- PROVIDER INDEPENDENCE: no vendor name, token, workspace id, or other
  provider-specific field appears anywhere in this module. ``QAEngine`` is
  a bare ``Protocol``; ``QAEngineCapabilities`` describes what an engine
  can do, never who it is.
- RESTART-SAFE: ``QARunStore`` alone (never re-querying a provider) can
  answer, after a cold restart, what was requested, on what SHA, with
  what policy/manifest, by which engine, whether it finished, and what
  verdict was produced.
- Git vs SQLite boundary: this module (SQLite, orchestrator runtime/audit)
  never reads or writes ``.qa/*.yaml`` (Git-durable product knowledge) —
  that is ``orchestrator.qa_knowledge``'s exclusive concern.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from orchestrator.validation import QualityGateRunner

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _tuple_of_str(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return items


# --- canonical enums ---------------------------------------------------------


class FailureClassification(str, Enum):
    """Canonical, closed set — no implicit extra classification anywhere in
    this codebase. ``UNKNOWN`` is a first-class outcome, never silently
    forced into another class just to look decisive."""

    REGRESSION = "regression"
    EXPECTED_CHANGE = "expected_change"
    TEST_DEFECT = "test_defect"
    FLAKY_TEST = "flaky_test"
    ENVIRONMENT_FAILURE = "environment_failure"
    UNKNOWN = "unknown"


class QAPhase(str, Enum):
    """Distinct from ``QARunStatus``/``QAVerdictStatus`` — this is *which
    kind* of QA run this is, not whether it finished or what it decided.

    ``TEST_AUTHORING``: may add/modify tests, fixtures, or knowledge files
    per policy; never production code. May change HEAD. LEGACY_COMPATIBILITY:
    this phase was only ever driven by the now-removed ``GOVERNED_FULL``
    pipeline's isolated QA Test Authoring step (see ROADMAP.md's dated
    removal entry); WorkItem Flow's QA call only ever uses
    ``FINAL_VERIFICATION``. Kept as a generic, still-meaningful `QAEngine`
    contract value, not because any current code path reaches it.
    ``FINAL_VERIFICATION``: must be read-only — see
    ``run_final_verification_gate``/``evaluate_qa_verdict``'s
    read-only-violation/read-only-unprovable handling. This is the only
    phase WorkItem Flow ever uses.
    """

    TEST_AUTHORING = "test_authoring"
    FINAL_VERIFICATION = "final_verification"


class QARunStatus(str, Enum):
    """Execution status of a ``QARun`` — distinct from ``QAVerdictStatus``.
    A technically ``FAILED``/``INTERRUPTED`` run can lead to an
    ``INCONCLUSIVE`` verdict, but never an automatic ``PASS``."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


_QA_RUN_TRANSITIONS: dict[QARunStatus, frozenset[QARunStatus]] = {
    QARunStatus.CREATED: frozenset({QARunStatus.RUNNING, QARunStatus.FAILED, QARunStatus.INTERRUPTED}),
    QARunStatus.RUNNING: frozenset({QARunStatus.COMPLETED, QARunStatus.FAILED, QARunStatus.INTERRUPTED}),
    QARunStatus.COMPLETED: frozenset(),
    QARunStatus.FAILED: frozenset(),
    QARunStatus.INTERRUPTED: frozenset(),
}


class QAVerdictStatus(str, Enum):
    """The governed decision — computed exclusively by ``evaluate_qa_verdict``,
    never by an engine's own self-report."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


#: A machine-checkable token embedded in ``QAVerdict.reason`` whenever
#: ``evaluate_qa_verdict`` returns INCONCLUSIVE because of
#: ``environment_drift_reason`` below — see that function's docstring.
VALIDATION_ENVIRONMENT_CHANGED = "VALIDATION_ENVIRONMENT_CHANGED"


# --- QARequest / QAResult ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class QARequest:
    """What is being asked of a ``QAEngine`` — provider-independent, never
    a fourre-tout. No vendor field (token/project id/workspace) here.

    ``phase`` (Slice 24, additive, default ``None`` for full backward
    compatibility with every Slice 22/23 caller that never set it): lets a
    generic engine distinguish ``TEST_AUTHORING`` from
    ``FINAL_VERIFICATION`` through the single ``QAEngine.run()`` Protocol
    method — without it, a provider-independent caller (``MVPManager``)
    would have no way to ask *any* engine, internal or external, for a
    read-only run through the Protocol alone.
    """

    project_id: str
    mvp_id: str
    work_item_id: str
    workspace: str
    base_sha: str
    head_sha: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    requested_test_levels: tuple[str, ...] = ()
    technology_stack: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    review_findings: tuple[str, ...] = ()
    required_invariants: tuple[str, ...] = ()
    required_test_ids: tuple[str, ...] = ()
    phase: "QAPhase | None" = None

    def __post_init__(self) -> None:
        for name in ("project_id", "mvp_id", "work_item_id", "workspace", "base_sha", "head_sha", "objective"):
            _require_non_empty_str(getattr(self, name), field_name=f"QARequest.{name}")
        for name in (
            "acceptance_criteria", "requested_test_levels", "technology_stack", "changed_files",
            "review_findings", "required_invariants", "required_test_ids",
        ):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"QARequest.{name}"))
        if self.phase is not None and not isinstance(self.phase, QAPhase):
            raise TypeError(f"QARequest.phase must be a QAPhase or None, got {type(self.phase)!r}")


@dataclass(frozen=True, slots=True)
class QAResult:
    """What a ``QAEngine`` observed — normalized, but **never** authoritative
    for the governed verdict (see module docstring). May carry a
    provider's raw claimed status in ``engine_reported_status`` purely as
    informational data; ``evaluate_qa_verdict`` never reads that field.
    """

    engine_id: str
    observed_head_sha: str
    started_at: datetime
    finished_at: datetime
    engine_run_id: str | None = None
    change_scope: str = ""
    risks: tuple[str, ...] = ()
    tests_selected: tuple[str, ...] = ()
    tests_added: tuple[str, ...] = ()
    tests_executed: tuple[str, ...] = ()
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    failure_classifications: tuple[FailureClassification, ...] = ()
    regressions: tuple[str, ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    requires_coding_agent: bool = False
    artifacts: tuple[str, ...] = ()
    failed_invariant_ids: tuple[str, ...] = ()
    engine_reported_status: str | None = None
    #: Slice 24, additive, default ``False`` (backward compatible with
    #: every Slice 22/23 caller): a normalized, provider-independent
    #: signal that a declared-read-only (``FINAL_VERIFICATION``) run
    #: actually mutated the workspace, or that the engine could not even
    #: prove it stayed read-only — direct inputs for
    #: ``evaluate_qa_verdict``'s ``read_only_violation``/
    #: ``read_only_unprovable`` kwargs. Part of the normalized result
    #: contract so any engine (internal or external) can report them
    #: without ``MVPManager`` needing engine-specific knowledge.
    read_only_violation: bool = False
    read_only_unprovable: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.engine_id, field_name="QAResult.engine_id")
        _require_non_empty_str(self.observed_head_sha, field_name="QAResult.observed_head_sha")
        _require_aware(self.started_at, field_name="QAResult.started_at")
        _require_aware(self.finished_at, field_name="QAResult.finished_at")
        if self.engine_run_id is not None:
            _require_non_empty_str(self.engine_run_id, field_name="QAResult.engine_run_id")
        for name in ("passed_count", "failed_count", "skipped_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"QAResult.{name} must be a non-negative int, got {value!r}")
        if not isinstance(self.requires_coding_agent, bool):
            raise TypeError("QAResult.requires_coding_agent must be a bool")
        for name in ("read_only_violation", "read_only_unprovable"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"QAResult.{name} must be a bool")
        for name in (
            "risks", "tests_selected", "tests_added", "tests_executed", "regressions",
            "coverage_gaps", "recommended_actions", "artifacts", "failed_invariant_ids",
        ):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"QAResult.{name}"))
        classifications = tuple(self.failure_classifications)
        for item in classifications:
            if not isinstance(item, FailureClassification):
                raise TypeError(f"QAResult.failure_classifications must contain FailureClassification, got {item!r}")
        object.__setattr__(self, "failure_classifications", classifications)


# --- policy / evidence manifest ----------------------------------------------


@dataclass(frozen=True, slots=True)
class QAEvidenceManifest:
    """What MUST be verified for a ``QARun`` — snapshotted with the run
    (see ``QARunStore.create``), never recomputed from live policy on
    replay. An empty manifest combined with
    ``QAPolicy.require_nonempty_evidence`` can never produce ``PASS``
    (the Slice 21.5 invariant, generalized to QA — see
    ``evaluate_qa_verdict``)."""

    required_test_ids: tuple[str, ...] = ()
    required_invariant_ids: tuple[str, ...] = ()
    required_engines: tuple[str, ...] = ()
    required_test_levels: tuple[str, ...] = ()
    deterministic_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "required_test_ids", "required_invariant_ids", "required_engines",
            "required_test_levels", "deterministic_commands",
        ):
            object.__setattr__(
                self, name, _tuple_of_str(getattr(self, name), field_name=f"QAEvidenceManifest.{name}")
            )

    @property
    def is_empty(self) -> bool:
        return not (
            self.required_test_ids or self.required_invariant_ids
            or self.required_engines or self.required_test_levels
        )


@dataclass(frozen=True, slots=True)
class QAPolicy:
    """Provider-independent, deliberately small — not 40 flags.
    ``required_engines`` holds abstract ``engine_id`` strings this project
    itself assigns (e.g. ``"internal"``), never a hardcoded vendor name."""

    qa_required: bool = True
    require_nonempty_evidence: bool = True
    require_exact_sha: bool = True
    final_verification_read_only: bool = True
    allowed_test_levels: tuple[str, ...] = ()
    required_invariant_ids: tuple[str, ...] = ()
    max_qa_cycles: int = 3
    required_engines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("qa_required", "require_nonempty_evidence", "require_exact_sha", "final_verification_read_only"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"QAPolicy.{name} must be a bool")
        if not isinstance(self.max_qa_cycles, int) or isinstance(self.max_qa_cycles, bool) or self.max_qa_cycles < 1:
            raise ValueError(f"QAPolicy.max_qa_cycles must be a positive int, got {self.max_qa_cycles!r}")
        for name in ("allowed_test_levels", "required_invariant_ids", "required_engines"):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"QAPolicy.{name}"))


# --- verdict -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QAVerdict:
    """The orchestrator's own deterministic governance decision — never an
    engine's self-report. Produced exclusively by ``evaluate_qa_verdict``."""

    status: QAVerdictStatus
    reason: str
    evaluated_at: datetime
    run_id: str | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, QAVerdictStatus):
            raise TypeError(f"QAVerdict.status must be a QAVerdictStatus, got {type(self.status)!r}")
        _require_non_empty_str(self.reason, field_name="QAVerdict.reason")
        _require_aware(self.evaluated_at, field_name="QAVerdict.evaluated_at")


# --- QARun -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QARun:
    """The durable record of one QA execution attempt — policy and
    evidence manifest are snapshotted at creation, exactly as
    ``ValidationStore.record_manifest`` snapshots a quality-gate manifest
    (Slice 21.5). ``verdict`` is set at most once (see
    ``QARunStore.record_verdict``)."""

    run_id: str
    project_id: str
    mvp_id: str
    work_item_id: str
    engine_id: str
    phase: QAPhase
    expected_base_sha: str
    expected_head_sha: str
    policy: QAPolicy
    manifest: QAEvidenceManifest
    status: QARunStatus
    created_at: datetime
    updated_at: datetime
    engine_run_id: str | None = None
    verdict: QAVerdict | None = None

    def __post_init__(self) -> None:
        for name in (
            "run_id", "project_id", "mvp_id", "work_item_id", "engine_id",
            "expected_base_sha", "expected_head_sha",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"QARun.{name}")
        if not isinstance(self.phase, QAPhase):
            raise TypeError(f"QARun.phase must be a QAPhase, got {type(self.phase)!r}")
        if not isinstance(self.status, QARunStatus):
            raise TypeError(f"QARun.status must be a QARunStatus, got {type(self.status)!r}")
        if not isinstance(self.policy, QAPolicy):
            raise TypeError(f"QARun.policy must be a QAPolicy, got {type(self.policy)!r}")
        if not isinstance(self.manifest, QAEvidenceManifest):
            raise TypeError(f"QARun.manifest must be a QAEvidenceManifest, got {type(self.manifest)!r}")
        _require_aware(self.created_at, field_name="QARun.created_at")
        _require_aware(self.updated_at, field_name="QARun.updated_at")
        if self.engine_run_id is not None:
            _require_non_empty_str(self.engine_run_id, field_name="QARun.engine_run_id")
        if self.verdict is not None and not isinstance(self.verdict, QAVerdict):
            raise TypeError(f"QARun.verdict must be a QAVerdict or None, got {type(self.verdict)!r}")


# --- domain errors -----------------------------------------------------------


class QAError(Exception):
    """Base for QA-governance domain errors."""


class UnknownQARunError(QAError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"unknown QA run: {run_id!r}")
        self.run_id = run_id


class DuplicateQARunError(QAError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"QA run already exists: {run_id!r}")
        self.run_id = run_id


class InvalidQARunTransitionError(QAError):
    def __init__(self, run_id: str, current: QARunStatus, attempted: QARunStatus) -> None:
        super().__init__(f"QA run {run_id!r} cannot move from {current.value!r} to {attempted.value!r}")
        self.run_id = run_id
        self.current = current
        self.attempted = attempted


class VerdictAlreadyRecordedError(QAError):
    """A ``QARun``'s verdict is recorded at most once — historical facts are
    immutable, mirroring the insert-only philosophy used across this
    project's audit stores."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"QA run {run_id!r} already has a recorded verdict")
        self.run_id = run_id


class ResultAlreadyRecordedError(QAError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"QA run {run_id!r} already has a recorded result")
        self.run_id = run_id


# --- QARunStore: sqlite3, restart-safe ---------------------------------------


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS qa_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    expected_base_sha TEXT NOT NULL,
    expected_head_sha TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    engine_run_id TEXT,
    result_json TEXT,
    verdict_status TEXT,
    verdict_reason TEXT,
    verdict_evaluated_at TEXT
);
CREATE TABLE IF NOT EXISTS qa_run_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _encode_policy(policy: QAPolicy) -> str:
    return json.dumps(
        {
            "qa_required": policy.qa_required,
            "require_nonempty_evidence": policy.require_nonempty_evidence,
            "require_exact_sha": policy.require_exact_sha,
            "final_verification_read_only": policy.final_verification_read_only,
            "allowed_test_levels": list(policy.allowed_test_levels),
            "required_invariant_ids": list(policy.required_invariant_ids),
            "max_qa_cycles": policy.max_qa_cycles,
            "required_engines": list(policy.required_engines),
        }
    )


def _decode_policy(raw: str) -> QAPolicy:
    data = json.loads(raw)
    return QAPolicy(
        qa_required=data["qa_required"], require_nonempty_evidence=data["require_nonempty_evidence"],
        require_exact_sha=data["require_exact_sha"], final_verification_read_only=data["final_verification_read_only"],
        allowed_test_levels=tuple(data["allowed_test_levels"]), required_invariant_ids=tuple(data["required_invariant_ids"]),
        max_qa_cycles=data["max_qa_cycles"], required_engines=tuple(data["required_engines"]),
    )


def _encode_manifest(manifest: QAEvidenceManifest) -> str:
    return json.dumps(
        {
            "required_test_ids": list(manifest.required_test_ids),
            "required_invariant_ids": list(manifest.required_invariant_ids),
            "required_engines": list(manifest.required_engines),
            "required_test_levels": list(manifest.required_test_levels),
            "deterministic_commands": list(manifest.deterministic_commands),
        }
    )


def _decode_manifest(raw: str) -> QAEvidenceManifest:
    data = json.loads(raw)
    return QAEvidenceManifest(
        required_test_ids=tuple(data["required_test_ids"]), required_invariant_ids=tuple(data["required_invariant_ids"]),
        required_engines=tuple(data["required_engines"]), required_test_levels=tuple(data["required_test_levels"]),
        deterministic_commands=tuple(data["deterministic_commands"]),
    )


def _encode_result(result: QAResult) -> str:
    return json.dumps(
        {
            "engine_id": result.engine_id, "observed_head_sha": result.observed_head_sha,
            "started_at": result.started_at.isoformat(), "finished_at": result.finished_at.isoformat(),
            "engine_run_id": result.engine_run_id, "change_scope": result.change_scope,
            "risks": list(result.risks), "tests_selected": list(result.tests_selected),
            "tests_added": list(result.tests_added), "tests_executed": list(result.tests_executed),
            "passed_count": result.passed_count, "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
            "failure_classifications": [c.value for c in result.failure_classifications],
            "regressions": list(result.regressions), "coverage_gaps": list(result.coverage_gaps),
            "recommended_actions": list(result.recommended_actions),
            "requires_coding_agent": result.requires_coding_agent, "artifacts": list(result.artifacts),
            "failed_invariant_ids": list(result.failed_invariant_ids),
            "engine_reported_status": result.engine_reported_status,
        }
    )


def _decode_result(raw: str) -> QAResult:
    data = json.loads(raw)
    return QAResult(
        engine_id=data["engine_id"], observed_head_sha=data["observed_head_sha"],
        started_at=datetime.fromisoformat(data["started_at"]), finished_at=datetime.fromisoformat(data["finished_at"]),
        engine_run_id=data["engine_run_id"], change_scope=data["change_scope"],
        risks=tuple(data["risks"]), tests_selected=tuple(data["tests_selected"]),
        tests_added=tuple(data["tests_added"]), tests_executed=tuple(data["tests_executed"]),
        passed_count=data["passed_count"], failed_count=data["failed_count"], skipped_count=data["skipped_count"],
        failure_classifications=tuple(FailureClassification(c) for c in data["failure_classifications"]),
        regressions=tuple(data["regressions"]), coverage_gaps=tuple(data["coverage_gaps"]),
        recommended_actions=tuple(data["recommended_actions"]), requires_coding_agent=data["requires_coding_agent"],
        artifacts=tuple(data["artifacts"]), failed_invariant_ids=tuple(data["failed_invariant_ids"]),
        engine_reported_status=data["engine_reported_status"],
    )


def _decode_row(row: sqlite3.Row) -> QARun:
    verdict = None
    if row["verdict_status"] is not None:
        verdict = QAVerdict(
            status=QAVerdictStatus(row["verdict_status"]), reason=row["verdict_reason"],
            evaluated_at=datetime.fromisoformat(row["verdict_evaluated_at"]),
            run_id=row["run_id"], head_sha=row["expected_head_sha"],
        )
    return QARun(
        run_id=row["run_id"], project_id=row["project_id"], mvp_id=row["mvp_id"], work_item_id=row["work_item_id"],
        engine_id=row["engine_id"], phase=QAPhase(row["phase"]), expected_base_sha=row["expected_base_sha"],
        expected_head_sha=row["expected_head_sha"], policy=_decode_policy(row["policy_json"]),
        manifest=_decode_manifest(row["manifest_json"]), status=QARunStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        engine_run_id=row["engine_run_id"], verdict=verdict,
    )


class QARunStore:
    """Synchronous, sqlite3-backed store: one mutable current-state row per
    QA run, plus an insert-only audit event log — restart-safe by
    construction (see module docstring)."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_CREATE_TABLES_SQL)

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="QARunStore clock()")
        return value

    def create(self, run: QARun) -> QARun:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO qa_runs (run_id, project_id, mvp_id, work_item_id, engine_id, phase, "
                    "expected_base_sha, expected_head_sha, policy_json, manifest_json, status, "
                    "created_at, updated_at, engine_run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id, run.project_id, run.mvp_id, run.work_item_id, run.engine_id, run.phase.value,
                        run.expected_base_sha, run.expected_head_sha, _encode_policy(run.policy),
                        _encode_manifest(run.manifest), run.status.value,
                        run.created_at.isoformat(), run.updated_at.isoformat(), run.engine_run_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateQARunError(run.run_id) from exc
        self._log_event(run.run_id, "run_created", f"engine_id={run.engine_id!r} phase={run.phase.value!r}")
        return run

    def get(self, run_id: str) -> QARun:
        row = self._conn.execute("SELECT * FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownQARunError(run_id)
        return _decode_row(row)

    def try_get(self, run_id: str) -> QARun | None:
        row = self._conn.execute("SELECT * FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else _decode_row(row)

    def list_for_work_item(self, work_item_id: str) -> list[QARun]:
        rows = self._conn.execute(
            "SELECT * FROM qa_runs WHERE work_item_id = ? ORDER BY created_at ASC", (work_item_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def latest_for_work_item(self, work_item_id: str) -> QARun | None:
        row = self._conn.execute(
            "SELECT * FROM qa_runs WHERE work_item_id = ? ORDER BY created_at DESC LIMIT 1", (work_item_id,)
        ).fetchone()
        return None if row is None else _decode_row(row)

    def update_status(self, run_id: str, new_status: QARunStatus) -> QARun:
        current = self.get(run_id)
        if new_status != current.status:
            allowed = _QA_RUN_TRANSITIONS.get(current.status, frozenset())
            if new_status not in allowed:
                raise InvalidQARunTransitionError(run_id, current.status, new_status)
            with self._conn:
                self._conn.execute(
                    "UPDATE qa_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (new_status.value, self._now().isoformat(), run_id),
                )
            self._log_event(run_id, "status_changed", f"{current.status.value} -> {new_status.value}")
        return self.get(run_id)

    def record_result(self, run_id: str, result: QAResult) -> QARun:
        """Insert-only for a given run: raises ``ResultAlreadyRecordedError``
        on a second call for the same run — a run's own result is a
        historical fact, never silently replaced."""
        row = self._conn.execute("SELECT result_json FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownQARunError(run_id)
        if row["result_json"] is not None:
            raise ResultAlreadyRecordedError(run_id)
        with self._conn:
            self._conn.execute(
                "UPDATE qa_runs SET result_json = ?, updated_at = ? WHERE run_id = ?",
                (_encode_result(result), self._now().isoformat(), run_id),
            )
        self._log_event(run_id, "result_recorded", f"engine_id={result.engine_id!r} observed_head_sha={result.observed_head_sha!r}")
        return self.get(run_id)

    def get_result(self, run_id: str) -> QAResult | None:
        row = self._conn.execute("SELECT result_json FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownQARunError(run_id)
        if row["result_json"] is None:
            return None
        return _decode_result(row["result_json"])

    def record_verdict(self, run_id: str, verdict: QAVerdict) -> QARun:
        """Insert-only: a run's verdict is recorded at most once (see
        ``VerdictAlreadyRecordedError``) — a historical governance decision
        is never retroactively changed by this store."""
        row = self._conn.execute("SELECT verdict_status FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownQARunError(run_id)
        if row["verdict_status"] is not None:
            raise VerdictAlreadyRecordedError(run_id)
        with self._conn:
            self._conn.execute(
                "UPDATE qa_runs SET verdict_status = ?, verdict_reason = ?, verdict_evaluated_at = ?, "
                "updated_at = ? WHERE run_id = ?",
                (
                    verdict.status.value, verdict.reason, verdict.evaluated_at.isoformat(),
                    self._now().isoformat(), run_id,
                ),
            )
        self._log_event(run_id, "verdict_recorded", f"status={verdict.status.value!r} reason={verdict.reason!r}")
        return self.get(run_id)

    def list_events(self, run_id: str) -> list[tuple[str, str, datetime]]:
        rows = self._conn.execute(
            "SELECT event_type, detail, created_at FROM qa_run_events WHERE run_id = ? ORDER BY row_id ASC",
            (run_id,),
        ).fetchall()
        return [(r["event_type"], r["detail"], datetime.fromisoformat(r["created_at"])) for r in rows]

    def _log_event(self, run_id: str, event_type: str, detail: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO qa_run_events (run_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (run_id, event_type, detail, self._now().isoformat()),
            )


def new_qa_run(
    *,
    project_id: str,
    mvp_id: str,
    work_item_id: str,
    engine_id: str,
    phase: QAPhase,
    expected_base_sha: str,
    expected_head_sha: str,
    policy: QAPolicy,
    manifest: QAEvidenceManifest,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    engine_run_id: str | None = None,
) -> QARun:
    """Convenience constructor for a fresh, ``CREATED`` ``QARun`` — not a
    store method, since callers may want to hold the object before
    persisting it via ``QARunStore.create``."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    run_id = (id_factory or (lambda: uuid.uuid4().hex))()
    return QARun(
        run_id=run_id, project_id=project_id, mvp_id=mvp_id, work_item_id=work_item_id, engine_id=engine_id,
        phase=phase, expected_base_sha=expected_base_sha, expected_head_sha=expected_head_sha,
        policy=policy, manifest=manifest, status=QARunStatus.CREATED, created_at=now, updated_at=now,
        engine_run_id=engine_run_id,
    )


# --- deterministic governance decision ---------------------------------------


def environment_drift_reason(
    *,
    previous_verdict_status: QAVerdictStatus | None,
    previous_environment_fingerprints: Mapping[str, str | None],
    current_environment_fingerprints: Mapping[str, str | None],
) -> str | None:
    """Detects the exact defect a real AIDO Code self-dogfood run found
    (WI-02, see ``validation.ValidationEnvironmentEvidence``'s docstring):
    a validation command FAILed, then PASSed on the identical head SHA
    and command after something outside Git changed the ambient
    execution environment (a dependency became importable) between the
    two QA attempts. Neither attempt was "wrong" about what it observed —
    but the second attempt's PASS is not deterministic evidence
    equivalent to the first attempt's FAIL, because the environment
    itself was never pinned/verified.

    Returns a reason string embedding ``VALIDATION_ENVIRONMENT_CHANGED``
    when: a prior QA run exists at this exact head SHA, that prior run's
    verdict was not already ``PASS`` (nothing here overwrites an already-
    trusted PASS), and at least one validation command's environment
    fingerprint genuinely differs between the two attempts (a fingerprint
    missing on either side — e.g. a pre-Slice-25 historical run — is never
    treated as a difference; absence of evidence is not evidence of
    drift). Returns ``None`` otherwise: no prior run to compare against,
    the prior run was already ``PASS``, or every comparable fingerprint
    matches (genuinely reproducible).

    Pure — takes already-computed fingerprints (from
    ``ValidationStore.get_environment_fingerprints``), never queries a
    store itself, exactly like every other ``evaluate_qa_verdict`` input.
    """
    if previous_verdict_status is None or previous_verdict_status is QAVerdictStatus.PASS:
        return None
    changed = sorted(
        validation_id
        for validation_id, current_fp in current_environment_fingerprints.items()
        if current_fp is not None
        and previous_environment_fingerprints.get(validation_id) is not None
        and previous_environment_fingerprints[validation_id] != current_fp
    )
    if not changed:
        return None
    return (
        f"{VALIDATION_ENVIRONMENT_CHANGED}: validation command(s) {changed!r} ran under a "
        f"different validation environment than the prior {previous_verdict_status.value!r} "
        "attempt at this exact head SHA — a resulting PASS is not deterministic evidence "
        "equivalent to that prior attempt"
    )


def evaluate_qa_verdict(
    *,
    run: QARun,
    result: QAResult | None,
    now: datetime,
    unauthorized_protected_change: bool = False,
    read_only_violation: bool = False,
    read_only_unprovable: bool = False,
    environment_drift_detail: str | None = None,
) -> QAVerdict:
    """Pure, deterministic — never an LLM call, never reads
    ``result.engine_reported_status``. Mirrors
    ``git_governance.compute_merge_eligibility``'s shape exactly: every
    fact beyond ``run``/``result`` is a caller-supplied, already-computed
    fact (e.g. from ``orchestrator.qa_protection``'s baseline comparison,
    ``run_final_verification_gate`` below, or ``environment_drift_reason``
    above) — this function never queries a store or the filesystem
    itself.

    ``environment_drift_detail`` (Slice 25, additive, default ``None``):
    the caller-computed result of ``environment_drift_reason`` — when not
    ``None``, this run's verdict is ``INCONCLUSIVE`` regardless of what
    ``result`` itself shows, since the caller only ever passes a non-
    ``None`` value when this attempt would otherwise flip a prior non-PASS
    attempt to PASS under a different, unpinned validation environment
    (see that function). "DETERMINISTIC QA EVIDENCE" means: same head SHA
    + same validation command + same *observed* validation environment —
    never a claim of full sandbox hermeticity (see
    ``docs/QA_STRATEGY.md``).

    Order of checks mirrors the minimal PASS conditions from
    ``docs/QA_STRATEGY.md``/``docs/QA_GOVERNANCE.md``: run must be
    ``COMPLETED``; a read-only Final Verification that can't prove it
    stayed read-only is ``INCONCLUSIVE``; a missing ``QAResult`` on a
    completed run is ``INCONCLUSIVE`` (an infrastructure gap, not a
    negative test signal); an unpinned environment change since a prior
    non-PASS attempt at this exact SHA is also ``INCONCLUSIVE``;
    everything after that is a definite ``FAIL`` signal (read-only
    violated, wrong SHA, empty mandatory evidence, unauthorized
    protected-test change, unresolved regression, a required invariant
    failing, or a required engine missing) — only when none of those
    apply is the verdict ``PASS``.
    """
    _require_aware(now, field_name="evaluate_qa_verdict now")

    def _verdict(status: QAVerdictStatus, reason: str) -> QAVerdict:
        return QAVerdict(status=status, reason=reason, evaluated_at=now, run_id=run.run_id, head_sha=run.expected_head_sha)

    if run.status in (QARunStatus.CREATED, QARunStatus.RUNNING):
        return _verdict(QAVerdictStatus.INCONCLUSIVE, f"qa run not finished (status={run.status.value!r})")
    if run.status is QARunStatus.INTERRUPTED:
        return _verdict(QAVerdictStatus.INCONCLUSIVE, "qa run was interrupted before completion")
    if run.status is QARunStatus.FAILED:
        return _verdict(QAVerdictStatus.INCONCLUSIVE, "qa run failed at the infrastructure/execution level")

    # run.status is COMPLETED from here.
    if read_only_unprovable:
        return _verdict(QAVerdictStatus.INCONCLUSIVE, "final verification could not prove the repository stayed read-only")
    if result is None:
        return _verdict(QAVerdictStatus.INCONCLUSIVE, "qa run completed but produced no QAResult")
    if environment_drift_detail is not None:
        return _verdict(QAVerdictStatus.INCONCLUSIVE, environment_drift_detail)
    if read_only_violation:
        return _verdict(QAVerdictStatus.FAIL, "final verification declared read-only but a mutation was observed")
    if run.policy.require_exact_sha and result.observed_head_sha != run.expected_head_sha:
        return _verdict(
            QAVerdictStatus.FAIL,
            f"QAResult.observed_head_sha {result.observed_head_sha!r} != expected {run.expected_head_sha!r}",
        )
    if run.policy.require_nonempty_evidence and run.manifest.is_empty:
        return _verdict(QAVerdictStatus.FAIL, "mandatory QA evidence manifest is empty")
    if unauthorized_protected_change:
        return _verdict(QAVerdictStatus.FAIL, "a protected test changed without a versioned authorization")
    if result.regressions:
        return _verdict(QAVerdictStatus.FAIL, f"unresolved regression(s): {list(result.regressions)!r}")
    failed_required_invariants = sorted(set(run.policy.required_invariant_ids) & set(result.failed_invariant_ids))
    if failed_required_invariants:
        return _verdict(QAVerdictStatus.FAIL, f"mandatory invariant(s) failing: {failed_required_invariants!r}")
    if run.policy.required_engines and run.engine_id not in run.policy.required_engines:
        return _verdict(
            QAVerdictStatus.FAIL,
            f"required engine(s) {list(run.policy.required_engines)!r} missing (run used {run.engine_id!r})",
        )
    return _verdict(QAVerdictStatus.PASS, "all mandatory QA evidence present and passing for the current head SHA")


# --- read-only Final Verification, reusing Slice 21.5's validation.py -------


async def run_final_verification_gate(
    *,
    gate_runner: "QualityGateRunner",
    project_id: str,
    cwd: str | Path,
    mvp_id: str | None = None,
    work_item_id: str | None = None,
) -> tuple[bool, bool]:
    """Runs a project's configured commands as a declared read-only Final
    Verification pass — reuses
    ``QualityGateRunner.run_gate(verify_repository_unchanged=True,
    require_nonempty_mandatory_manifest=True)`` verbatim (Slice 21.5),
    never a second implementation of read-only detection.

    Returns ``(read_only_violation, read_only_unprovable)`` for direct use
    as ``evaluate_qa_verdict`` kwargs. ``read_only_unprovable`` is True
    only when the workspace isn't a git repository at all (no SHA to
    compare against) — the "infrastructure inability to prove read-only"
    case from the spec, distinct from an actually observed mutation.
    """
    from orchestrator.validation import ReadOnlyValidationViolationError

    try:
        result = await gate_runner.run_gate(
            project_id=project_id, cwd=cwd, mvp_id=mvp_id, work_item_id=work_item_id,
            require_nonempty_mandatory_manifest=True, verify_repository_unchanged=True,
        )
    except ReadOnlyValidationViolationError:
        return True, False
    if result.git_sha is None:
        return False, True
    return False, False


# --- QAEngine abstraction (contract only — no implementation here) ---------


class QAEngine(Protocol):
    """Provider-independent contract. No ``InternalQAEngine``/
    ``TestSpriteQAEngine``/etc. implements this in this slice — see
    ``docs/QA_GOVERNANCE.md``. A future implementation's ``run`` may call
    out to an LLM/subprocess/external API; only the shape is fixed here."""

    def run(self, request: QARequest) -> QAResult: ...


@dataclass(frozen=True, slots=True)
class QAEngineCapabilities:
    """Describes what an engine *can do* — never who it is. No vendor name
    hardcoded anywhere in this dataclass or its usage in this module."""

    engine_id: str
    supported_test_levels: tuple[str, ...] = ()
    supported_stacks: tuple[str, ...] = ()
    can_author_tests: bool = False
    can_execute_existing_tests: bool = False
    supports_read_only: bool = False
    supports_resume: bool = False
    supports_external_artifacts: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.engine_id, field_name="QAEngineCapabilities.engine_id")
        for name in ("supported_test_levels", "supported_stacks"):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"QAEngineCapabilities.{name}"))
        for name in (
            "can_author_tests", "can_execute_existing_tests", "supports_read_only",
            "supports_resume", "supports_external_artifacts",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"QAEngineCapabilities.{name} must be a bool")


# --- Test Impact Analysis contract (deterministic analyzer lives in
#     orchestrator.qa_knowledge, which depends on the regression map) -------


@dataclass(frozen=True, slots=True)
class TestImpactRequest:
    base_sha: str
    head_sha: str
    changed_files: tuple[str, ...] = ()
    technology_stack: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.base_sha, field_name="TestImpactRequest.base_sha")
        _require_non_empty_str(self.head_sha, field_name="TestImpactRequest.head_sha")
        for name in ("changed_files", "technology_stack"):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"TestImpactRequest.{name}"))


@dataclass(frozen=True, slots=True)
class TestImpactResult:
    impacted_components: tuple[str, ...] = ()
    impacted_capabilities: tuple[str, ...] = ()
    existing_tests: tuple[str, ...] = ()
    missing_test_areas: tuple[str, ...] = ()
    recommended_test_levels: tuple[str, ...] = ()
    required_invariants: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "impacted_components", "impacted_capabilities", "existing_tests", "missing_test_areas",
            "recommended_test_levels", "required_invariants", "rationale",
        ):
            object.__setattr__(self, name, _tuple_of_str(getattr(self, name), field_name=f"TestImpactResult.{name}"))
