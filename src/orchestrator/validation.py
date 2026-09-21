"""Project validation commands + quality gates — Slice 8.

This module answers: "does this project's configured technical bar pass,
as actually observed by running real commands?" A worker claiming "the
tests pass" is never sufficient proof — the orchestrator runs the
project's own configured validation commands itself and persists exactly
what happened (command, cwd, exit code, duration, bounded stdout/stderr).

This is not code review: it never judges code quality, only whether
explicitly configured technical commands (unit tests, lint, typecheck,
build, smoke, ...) pass. Independent AI review is Slice 9's job.

Design invariants:

- Commands come exclusively from a project's persisted configuration
  (``ValidationStore.set_project_commands``), never invented by an LLM at
  run time and never parsed out of free text. ``argv`` is always a
  structured tuple, executed with ``shell=False`` — no shell string is
  ever built or interpreted.
- FAILED (command ran, exit code != 0) is never confused with ERROR
  (could not run the command at all, e.g. missing binary) or TIMEOUT
  (exceeded its bound) — three distinct, structured statuses.
- Fail-closed: a ``required`` validation must have a recorded ``PASSED``
  result for the gate to pass. Anything else — FAILED, ERROR, TIMEOUT, or
  simply *no result at all* for a configured required validation — fails
  the gate. "Not executed" is never interpreted as success.
- An optional (``required=False``) validation can fail without failing the
  overall gate; its result is still persisted.
- Git access is read-only (``git rev-parse HEAD``, once per gate run, as
  an audit fact) — no checkout/branch/commit/merge/reset/clean, ever.
- A ``validation_run_id`` identifies one quality-gate run (which may
  execute several configured commands); it is distinct from
  ``execution_id``/``work_item_id``/``mvp_id``. The same WorkItem can be
  gated multiple times, each with its own run id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Sequence

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

_MAX_CAPTURED_OUTPUT_CHARS = 4000


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _bounded(text: str) -> str:
    if len(text) <= _MAX_CAPTURED_OUTPUT_CHARS:
        return text
    return text[:_MAX_CAPTURED_OUTPUT_CHARS] + "...<truncated>"


class ValidationKind(str, Enum):
    """A small, deliberately non-exhaustive taxonomy of technical checks."""

    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"
    SMOKE = "smoke"
    CUSTOM = "custom"


class ValidationStatus(str, Enum):
    """Never conflate these: FAILED != ERROR != TIMEOUT."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ValidationCommand:
    """A configured, structured validation command — never a shell string."""

    validation_id: str
    kind: ValidationKind
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    required: bool = True

    def __post_init__(self) -> None:
        _require_non_empty_str(self.validation_id, field_name="ValidationCommand.validation_id")
        if not isinstance(self.kind, ValidationKind):
            raise TypeError(f"ValidationCommand.kind must be a ValidationKind, got {type(self.kind)!r}")
        argv = tuple(self.argv)
        if not argv:
            raise ValueError("ValidationCommand.argv must be a non-empty sequence")
        for arg in argv:
            if not isinstance(arg, str) or not arg:
                raise ValueError(f"ValidationCommand.argv must only contain non-empty strings, got {arg!r}")
        object.__setattr__(self, "argv", argv)
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError(
                f"ValidationCommand.timeout_seconds must be a positive number, got {self.timeout_seconds!r}"
            )
        if not isinstance(self.required, bool):
            raise TypeError(f"ValidationCommand.required must be a bool, got {type(self.required)!r}")


@dataclass(frozen=True, slots=True)
class ValidationEnvironmentEvidence:
    """What actually ran a ``ValidationCommand`` — observed once per
    execution, alongside the git SHA it ran against.

    Found necessary via a real AIDO Code self-dogfood run (WI-02, see
    ``docs/reports/``): a project's configured QA command (``pytest -q``,
    resolved as a bare name via inherited ``PATH``) FAILed with
    ``ModuleNotFoundError`` on some head SHA, then PASSed on that exact
    same SHA and command after an unrelated process installed the
    missing dependency into the ambient environment between the two QA
    attempts. Before this, ``ValidationResult`` recorded the SHA and the
    command but nothing about *which* interpreter/environment actually
    ran it — so this exact "same SHA, different environment" drift was
    unrepresentable, and a resulting PASS looked identical to a genuinely
    deterministic one. See ``qa.environment_drift_reason``/
    ``evaluate_qa_verdict``'s ``environment_drift_detail``.

    Deliberately a small, explicit allow-list — never a raw ``os.environ``
    dump: only facts that can plausibly explain a command resolving
    differently between two runs of the identical ``argv``/``cwd``/SHA.
    No secret, token, or credential is ever captured here.

    This does not prove the execution environment was hermetic/sandboxed
    — only that it was *observed*, so a later drift is *detectable*. See
    ``docs/QA_STRATEGY.md``'s "DETERMINISTIC QA EVIDENCE" definition.
    """

    resolved_executable: str | None
    path_value: str
    virtual_env: str | None = None
    pythonpath: str | None = None
    python_executable: str | None = None
    python_version: str | None = None
    sys_prefix: str | None = None
    packages_fingerprint: str | None = None
    probe_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_value", _bounded(self.path_value))

    @property
    def fingerprint(self) -> str:
        """A stable digest of every field above. Two evidence values with
        the same fingerprint are, as far as this module can observe, the
        identical validation environment — used only to detect drift
        between two attempts, never to prove hermetic isolation."""
        payload = json.dumps(
            {
                "resolved_executable": self.resolved_executable,
                "path_value": self.path_value,
                "virtual_env": self.virtual_env,
                "pythonpath": self.pythonpath,
                "python_executable": self.python_executable,
                "python_version": self.python_version,
                "sys_prefix": self.sys_prefix,
                "packages_fingerprint": self.packages_fingerprint,
                "probe_error": self.probe_error,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The observed outcome of running one ValidationCommand once."""

    validation_run_id: str
    validation_id: str
    kind: ValidationKind
    required: bool
    argv: tuple[str, ...]
    status: ValidationStatus
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    git_sha: str | None = None
    #: ``None`` for any result recorded before this field existed — decoded
    #: honestly as unknown, never fabricated after the fact (see
    #: ``_decode_result_row``).
    environment: ValidationEnvironmentEvidence | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.validation_run_id, field_name="ValidationResult.validation_run_id")
        _require_non_empty_str(self.validation_id, field_name="ValidationResult.validation_id")
        if not isinstance(self.kind, ValidationKind):
            raise TypeError(f"ValidationResult.kind must be a ValidationKind, got {type(self.kind)!r}")
        if not isinstance(self.status, ValidationStatus):
            raise TypeError(f"ValidationResult.status must be a ValidationStatus, got {type(self.status)!r}")
        _require_aware(self.started_at, field_name="ValidationResult.started_at")
        _require_aware(self.finished_at, field_name="ValidationResult.finished_at")
        object.__setattr__(self, "argv", tuple(self.argv))
        if self.exit_code is not None and (not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)):
            raise TypeError(f"ValidationResult.exit_code must be an int or None, got {type(self.exit_code)!r}")
        if self.git_sha is not None:
            _require_non_empty_str(self.git_sha, field_name="ValidationResult.git_sha")
        if self.environment is not None and not isinstance(self.environment, ValidationEnvironmentEvidence):
            raise TypeError(
                f"ValidationResult.environment must be a ValidationEnvironmentEvidence or None, "
                f"got {type(self.environment)!r}"
            )

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    """The aggregated outcome of one quality-gate run."""

    validation_run_id: str
    project_id: str
    passed: bool
    results: tuple[ValidationResult, ...]
    mvp_id: str | None = None
    work_item_id: str | None = None
    git_sha: str | None = None


class ValidationError(Exception):
    """Base for validation/quality-gate domain errors."""


class UnknownValidationRunError(ValidationError):
    def __init__(self, validation_run_id: str) -> None:
        super().__init__(f"unknown validation run: {validation_run_id!r}")
        self.validation_run_id = validation_run_id


class CorruptValidationResultError(ValidationError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"corrupt validation result: {detail}")


class ValidationTimeoutError(ValidationError):
    """Internal: a validation command exceeded its bounded timeout."""


class ReadOnlyValidationViolationError(ValidationError):
    """Raised when a gate run declared ``verify_repository_unchanged=True``
    but the repository's HEAD changed while the configured commands ran —
    e.g. a "final verification" step is expected to only observe/execute/
    report, never to commit or otherwise mutate the workspace. Fail-closed:
    this is raised instead of returning a result that could be mistaken
    for a legitimate PASS/FAIL."""

    def __init__(self, validation_run_id: str, sha_before: str | None, sha_after: str | None) -> None:
        super().__init__(
            f"validation run {validation_run_id!r} was declared read-only but HEAD changed "
            f"({sha_before!r} -> {sha_after!r})"
        )
        self.validation_run_id = validation_run_id
        self.sha_before = sha_before
        self.sha_after = sha_after


def _compute_passed(
    commands: Sequence[ValidationCommand],
    results: Sequence[ValidationResult],
    *,
    require_nonempty_mandatory_manifest: bool = False,
) -> bool:
    """Fail-closed manifest check, generalized in Slice 21.5.

    ``commands`` must be the manifest actually applied at run time (see
    ``ValidationStore.record_manifest``/``get_manifest_for_run``) — never a
    possibly-since-changed *current* project configuration, or a historical
    PASS/FAIL could silently flip meaning without the underlying evidence
    changing at all.

    ``require_nonempty_mandatory_manifest``, opt-in and False by default
    (preserves every pre-Slice-21.5 caller's behavior unchanged — a gate
    with zero required commands legitimately passes trivially today, e.g.
    an all-optional lint-only gate): when True, a manifest with no
    ``required`` command at all can never PASS. This is the primitive a
    future mandatory QA Final Verification (Slice 22/23) must set to True —
    "no mandatory checks were even configured" must never be
    indistinguishable from "all mandatory checks passed".
    """
    required_commands = [c for c in commands if c.required]
    if require_nonempty_mandatory_manifest and not required_commands:
        return False
    by_id = {r.validation_id: r for r in results}
    for command in required_commands:
        result = by_id.get(command.validation_id)
        # No result at all for a required validation is never "passed".
        if result is None or result.status is not ValidationStatus.PASSED:
            return False
    return True


def _encode_environment(environment: ValidationEnvironmentEvidence | None) -> str | None:
    if environment is None:
        return None
    return json.dumps(
        {
            "resolved_executable": environment.resolved_executable,
            "path_value": environment.path_value,
            "virtual_env": environment.virtual_env,
            "pythonpath": environment.pythonpath,
            "python_executable": environment.python_executable,
            "python_version": environment.python_version,
            "sys_prefix": environment.sys_prefix,
            "packages_fingerprint": environment.packages_fingerprint,
            "probe_error": environment.probe_error,
        }
    )


def _decode_environment(raw: str | None) -> ValidationEnvironmentEvidence | None:
    """``None`` for anything not recorded (missing column value, or a row
    written before this field existed) — decoded honestly as unknown,
    never fabricated after the fact."""
    if raw is None:
        return None
    data = json.loads(raw)
    return ValidationEnvironmentEvidence(**data)


def _decode_result_row(row: sqlite3.Row) -> ValidationResult:
    try:
        environment_raw = row["environment_json"] if "environment_json" in row.keys() else None
        return ValidationResult(
            validation_run_id=row["validation_run_id"],
            validation_id=row["validation_id"],
            kind=ValidationKind(row["kind"]),
            required=bool(row["required"]),
            argv=tuple(json.loads(row["argv"])),
            status=ValidationStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            exit_code=row["exit_code"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            git_sha=row["git_sha"],
            environment=_decode_environment(environment_raw),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CorruptValidationResultError(str(exc)) from exc


def _git_head_sha(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS validation_commands (
    project_id TEXT NOT NULL,
    validation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    argv TEXT NOT NULL,
    timeout_seconds REAL NOT NULL,
    required INTEGER NOT NULL,
    PRIMARY KEY (project_id, validation_id)
);
CREATE TABLE IF NOT EXISTS validation_results (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    validation_run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    mvp_id TEXT,
    work_item_id TEXT,
    validation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    required INTEGER NOT NULL,
    argv TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    exit_code INTEGER,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    git_sha TEXT,
    environment_json TEXT
);
CREATE TABLE IF NOT EXISTS validation_run_manifests (
    validation_run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class ValidationStore:
    """Synchronous, sqlite3-backed store for validation config + results."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # check_same_thread=False: a QAEngine backed by this store (e.g.
        # InternalQAEngine) is invoked by MVPManager via asyncio.to_thread
        # (Slice 24, mvp_manager.py's _run_qa_cycle) so a synchronous
        # engine's own internal asyncio.run() never collides with the
        # caller's already-running event loop — meaning this connection is
        # created on the main thread but legitimately queried from that
        # worker thread. Access here is always sequential (never actually
        # concurrent: MVPManager runs at most one QA/gate step at a time
        # for a given WorkItem), so no additional locking is needed.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_CREATE_TABLES_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Idempotent, additive-only: a pre-existing ``validation_results``
        table (created before ``environment_json`` existed) gets the
        column added once; a fresh table already has it from
        ``_CREATE_TABLES_SQL``. Never drops/renames a column, never
        touches existing rows — old rows simply decode
        ``environment=None`` (see ``_decode_environment``)."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(validation_results)").fetchall()}
        if "environment_json" not in columns:
            with self._conn:
                self._conn.execute("ALTER TABLE validation_results ADD COLUMN environment_json TEXT")

    def close(self) -> None:
        self._conn.close()

    def set_project_commands(self, project_id: str, commands: Sequence[ValidationCommand]) -> None:
        """Replaces the full configured validation set for a project."""
        _require_non_empty_str(project_id, field_name="project_id")
        with self._conn:
            self._conn.execute("DELETE FROM validation_commands WHERE project_id = ?", (project_id,))
            for sequence, command in enumerate(commands):
                self._conn.execute(
                    "INSERT INTO validation_commands "
                    "(project_id, validation_id, sequence, kind, argv, timeout_seconds, required) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id, command.validation_id, sequence, command.kind.value,
                        json.dumps(command.argv), command.timeout_seconds, int(command.required),
                    ),
                )

    def get_project_commands(self, project_id: str) -> tuple[ValidationCommand, ...]:
        rows = self._conn.execute(
            "SELECT * FROM validation_commands WHERE project_id = ? ORDER BY sequence ASC",
            (project_id,),
        ).fetchall()
        return tuple(
            ValidationCommand(
                validation_id=row["validation_id"],
                kind=ValidationKind(row["kind"]),
                argv=tuple(json.loads(row["argv"])),
                timeout_seconds=row["timeout_seconds"],
                required=bool(row["required"]),
            )
            for row in rows
        )

    def record_result(
        self,
        validation_run_id: str,
        project_id: str,
        result: ValidationResult,
        *,
        mvp_id: str | None = None,
        work_item_id: str | None = None,
        git_sha: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO validation_results "
                "(validation_run_id, project_id, mvp_id, work_item_id, validation_id, kind, "
                "required, argv, status, started_at, finished_at, exit_code, stdout, stderr, git_sha, "
                "environment_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    validation_run_id, project_id, mvp_id, work_item_id, result.validation_id,
                    result.kind.value, int(result.required), json.dumps(result.argv),
                    result.status.value, result.started_at.isoformat(), result.finished_at.isoformat(),
                    result.exit_code, result.stdout, result.stderr, git_sha,
                    _encode_environment(result.environment),
                ),
            )

    def get_environment_fingerprints(self, validation_run_id: str) -> dict[str, str | None]:
        """``validation_id -> ValidationEnvironmentEvidence.fingerprint``
        for every result recorded under this run, ``None`` for any result
        with no recorded environment evidence (a pre-Slice-25 row, or a
        probe that itself failed) — never fabricated. The primitive
        ``qa.environment_drift_reason`` needs to compare two attempts at
        the same head SHA."""
        rows = self._conn.execute(
            "SELECT validation_id, environment_json FROM validation_results WHERE validation_run_id = ?",
            (validation_run_id,),
        ).fetchall()
        fingerprints: dict[str, str | None] = {}
        for row in rows:
            environment = _decode_environment(row["environment_json"])
            fingerprints[row["validation_id"]] = environment.fingerprint if environment is not None else None
        return fingerprints

    def record_manifest(
        self, validation_run_id: str, project_id: str, commands: Sequence[ValidationCommand]
    ) -> None:
        """Snapshots the exact configured commands used for one gate run.

        A later replay (``get_gate_result``) must reflect the policy
        actually applied at run time — never the project's possibly-
        since-changed *current* ``validation_commands`` configuration
        (Slice 21.5 hardening: a required check added/removed/toggled
        after the fact must never retroactively change what an old run's
        ``passed`` means). Insert-only, like the rest of this store: a
        repeated call for the same ``validation_run_id`` is a no-op — the
        first recorded manifest wins, it is never overwritten.
        """
        manifest_json = json.dumps(
            [
                {
                    "validation_id": c.validation_id, "kind": c.kind.value, "argv": list(c.argv),
                    "timeout_seconds": c.timeout_seconds, "required": c.required,
                }
                for c in commands
            ]
        )
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO validation_run_manifests "
                "(validation_run_id, project_id, manifest_json, recorded_at) VALUES (?, ?, ?, ?)",
                (validation_run_id, project_id, manifest_json, self._clock().isoformat()),
            )

    def get_manifest_for_run(self, validation_run_id: str) -> tuple[ValidationCommand, ...] | None:
        """The manifest snapshot recorded for this run, or ``None`` if none
        was ever recorded (e.g. results written directly via
        ``record_result`` without going through ``QualityGateRunner.run_gate``,
        as some pre-Slice-21.5 tests do) — callers fall back to
        ``get_project_commands`` only in that case."""
        row = self._conn.execute(
            "SELECT manifest_json FROM validation_run_manifests WHERE validation_run_id = ?",
            (validation_run_id,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["manifest_json"])
        return tuple(
            ValidationCommand(
                validation_id=d["validation_id"], kind=ValidationKind(d["kind"]), argv=tuple(d["argv"]),
                timeout_seconds=d["timeout_seconds"], required=d["required"],
            )
            for d in data
        )

    def get_gate_result(
        self, validation_run_id: str, *, require_nonempty_mandatory_manifest: bool = False
    ) -> QualityGateResult:
        rows = self._conn.execute(
            "SELECT * FROM validation_results WHERE validation_run_id = ? ORDER BY row_id ASC",
            (validation_run_id,),
        ).fetchall()
        if not rows:
            raise UnknownValidationRunError(validation_run_id)

        results = tuple(_decode_result_row(row) for row in rows)

        first = rows[0]
        project_id = first["project_id"]
        commands = self.get_manifest_for_run(validation_run_id)
        if commands is None:
            commands = self.get_project_commands(project_id)
        return QualityGateResult(
            validation_run_id=validation_run_id,
            project_id=project_id,
            passed=_compute_passed(
                commands, results, require_nonempty_mandatory_manifest=require_nonempty_mandatory_manifest
            ),
            results=results,
            mvp_id=first["mvp_id"],
            work_item_id=first["work_item_id"],
            git_sha=first["git_sha"],
        )

    def latest_gate_result_for_work_item(self, work_item_id: str) -> QualityGateResult | None:
        row = self._conn.execute(
            "SELECT validation_run_id FROM validation_results WHERE work_item_id = ? "
            "ORDER BY row_id DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_gate_result(row["validation_run_id"])

    def list_results_for_work_item(self, work_item_id: str) -> list[ValidationResult]:
        """All validation results ever recorded for a WorkItem, oldest first.

        Spans every quality-gate run (including rework re-runs) — the read
        primitive an activity report (Slice 10) needs for full validation
        history, never log scraping.
        """
        rows = self._conn.execute(
            "SELECT * FROM validation_results WHERE work_item_id = ? ORDER BY row_id ASC",
            (work_item_id,),
        ).fetchall()
        return [_decode_result_row(row) for row in rows]


SubprocessRunner = Callable[[Sequence[str], Path, float], Awaitable[tuple[int, bytes, bytes]]]


async def _default_subprocess_runner(
    argv: Sequence[str], cwd: Path, timeout: float
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ValidationTimeoutError(f"validation command timed out after {timeout}s: {list(argv)!r}")
    return process.returncode, stdout, stderr


EnvironmentProbe = Callable[[Sequence[str], Path], ValidationEnvironmentEvidence]

_PYTHON_NAME_RE = re.compile(r"^python[0-9.]*$")

_PYTHON_PROBE_SOURCE = (
    "import json, sys\n"
    "packages = []\n"
    "try:\n"
    "    from importlib import metadata\n"
    "    for d in metadata.distributions():\n"
    "        name = d.name\n"
    "        if not name:\n"
    "            continue\n"
    "        entry = f'{name}=={d.version}'\n"
    "        try:\n"
    "            direct_url = d.read_text('direct_url.json') or ''\n"
    "        except Exception:\n"
    "            direct_url = ''\n"
    "        if '\"editable\": true' in direct_url.replace(' ', ''):\n"
    "            entry += '+editable'\n"
    "        packages.append(entry)\n"
    "    packages = sorted(set(packages))\n"
    "except Exception:\n"
    "    packages = []\n"
    "print(json.dumps({'version': sys.version, 'prefix': sys.prefix, "
    "'executable': sys.executable, 'packages': packages}))\n"
)


def _looks_like_python(path: str) -> bool:
    return bool(_PYTHON_NAME_RE.match(Path(path).name))


def _follow_shebang(executable: str) -> str | None:
    """Generic, not Python-specific: a console-script wrapper (``pytest``,
    ``ruff``, ...) is almost always a small text file whose first line
    names its real interpreter (``#!/path/to/python3``, possibly via
    ``#!/usr/bin/env python3`` indirection). Returns the resolved
    interpreter path, or ``None`` when ``executable`` has no shebang (a
    real ELF binary — ``node``, ``cargo``, ``go``, ...) or that
    interpreter cannot be located."""
    try:
        with open(executable, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    parts = line[2:].strip().split()
    if not parts:
        return None
    program = parts[0]
    if Path(program).name == "env" and len(parts) > 1:
        program = parts[1]
    if Path(program).is_absolute():
        return program if Path(program).is_file() else None
    return shutil.which(program)


def _resolve_python_executable(resolved: str) -> str | None:
    if _looks_like_python(resolved):
        return resolved
    followed = _follow_shebang(resolved)
    if followed is not None and _looks_like_python(followed):
        return followed
    return None


def _probe_python_environment(python_executable: str) -> tuple[dict | None, str | None]:
    try:
        probe = subprocess.run(
            [python_executable, "-c", _PYTHON_PROBE_SOURCE], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"python environment probe could not run: {exc}"
    if probe.returncode != 0:
        return None, f"python environment probe exited {probe.returncode}: {_bounded(probe.stderr)}"
    try:
        return json.loads(probe.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"python environment probe returned unparseable output: {exc}"


def _default_environment_probe(argv: Sequence[str], cwd: Path) -> ValidationEnvironmentEvidence:
    """Real, best-effort resolution of what will actually run ``argv[0]``
    — never invents a fact it could not observe (see
    ``ValidationEnvironmentEvidence``'s own docstring for why this
    exists). Reads only an explicit allow-list of environment variables
    (``PATH``/``VIRTUAL_ENV``/``PYTHONPATH``) — never a raw ``os.environ``
    dump, so no secret/token/credential is ever captured."""
    path_value = os.environ.get("PATH", "")
    virtual_env = os.environ.get("VIRTUAL_ENV")
    pythonpath = os.environ.get("PYTHONPATH")

    argv0 = argv[0] if argv else ""
    resolved = shutil.which(argv0) if argv0 else None
    if resolved is None and argv0 and Path(argv0).is_file():
        # A bare name is resolved by shutil.which via PATH; argv0 may
        # instead already be an absolute/relative path (e.g.
        # ``sys.executable``), which shutil.which does not handle.
        resolved = str(Path(argv0))

    if resolved is None:
        return ValidationEnvironmentEvidence(
            resolved_executable=None, path_value=path_value, virtual_env=virtual_env, pythonpath=pythonpath,
            probe_error=f"could not resolve {argv0!r} via PATH",
        )

    python_executable = _resolve_python_executable(resolved)
    if python_executable is None:
        return ValidationEnvironmentEvidence(
            resolved_executable=resolved, path_value=path_value, virtual_env=virtual_env, pythonpath=pythonpath,
        )

    data, probe_error = _probe_python_environment(python_executable)
    if data is None:
        return ValidationEnvironmentEvidence(
            resolved_executable=resolved, path_value=path_value, virtual_env=virtual_env, pythonpath=pythonpath,
            python_executable=python_executable, probe_error=probe_error,
        )
    packages_fingerprint = hashlib.sha256(
        json.dumps(data.get("packages", []), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ValidationEnvironmentEvidence(
        resolved_executable=resolved, path_value=path_value, virtual_env=virtual_env, pythonpath=pythonpath,
        python_executable=data.get("executable") or python_executable,
        python_version=data.get("version"), sys_prefix=data.get("prefix"),
        packages_fingerprint=packages_fingerprint,
    )


class QualityGateRunner:
    """Executes a project's configured ValidationCommands and persists results.

    Never invents a command: everything it runs comes from
    ``ValidationStore.get_project_commands``. Never mutates git. Never
    launches Claude/Codex/Ralph — only whatever ``argv`` the project
    configured (typically ``pytest``, ``ruff``, ``mypy``, ``npm``, ...).
    """

    def __init__(
        self,
        store: ValidationStore,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        subprocess_runner: SubprocessRunner | None = None,
        environment_probe: EnvironmentProbe | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._run_subprocess = subprocess_runner or _default_subprocess_runner
        self._environment_probe = environment_probe or _default_environment_probe

    async def run_gate(
        self,
        *,
        project_id: str,
        cwd: str | Path,
        mvp_id: str | None = None,
        work_item_id: str | None = None,
        validation_run_id: str | None = None,
        require_nonempty_mandatory_manifest: bool = False,
        verify_repository_unchanged: bool = False,
    ) -> QualityGateResult:
        """Runs this project's configured commands and persists the result.

        ``require_nonempty_mandatory_manifest`` (Slice 21.5, default False,
        backward-compatible): when True, a manifest with zero ``required``
        commands can never PASS — see ``_compute_passed``.

        ``verify_repository_unchanged`` (Slice 21.5, default False): when
        True, this is a declared *read-only* run — if HEAD differs before
        vs. after the configured commands ran, raises
        ``ReadOnlyValidationViolationError`` instead of returning a result
        (fail-closed; a "read-only" verification that silently commits is
        never treated as a legitimate PASS or FAIL). This is a generic
        primitive for a future QA Final Verification (Slice 22/23) — this
        slice does not build that engine, only the primitive it needs.
        """
        cwd = Path(cwd)
        run_id = validation_run_id or self._id_factory()
        commands = self._store.get_project_commands(project_id)
        self._store.record_manifest(run_id, project_id, commands)
        git_sha = _git_head_sha(cwd)

        results: list[ValidationResult] = []
        for command in commands:
            result = await self._run_one(run_id, command, cwd)
            self._store.record_result(
                run_id, project_id, result, mvp_id=mvp_id, work_item_id=work_item_id, git_sha=git_sha,
            )
            results.append(result)

        if verify_repository_unchanged:
            git_sha_after = _git_head_sha(cwd)
            if git_sha_after != git_sha:
                raise ReadOnlyValidationViolationError(run_id, git_sha, git_sha_after)

        return QualityGateResult(
            validation_run_id=run_id,
            project_id=project_id,
            passed=_compute_passed(
                commands, results, require_nonempty_mandatory_manifest=require_nonempty_mandatory_manifest
            ),
            results=tuple(results),
            mvp_id=mvp_id,
            work_item_id=work_item_id,
            git_sha=git_sha,
        )

    def _capture_environment(self, argv: Sequence[str], cwd: Path) -> ValidationEnvironmentEvidence:
        try:
            return self._environment_probe(argv, cwd)
        except Exception as exc:  # noqa: BLE001 - forensic evidence must never crash the gate itself
            return ValidationEnvironmentEvidence(
                resolved_executable=None, path_value=os.environ.get("PATH", ""),
                virtual_env=os.environ.get("VIRTUAL_ENV"), pythonpath=os.environ.get("PYTHONPATH"),
                probe_error=f"environment probe raised: {exc}",
            )

    async def _run_one(
        self, validation_run_id: str, command: ValidationCommand, cwd: Path
    ) -> ValidationResult:
        environment = self._capture_environment(command.argv, cwd)
        started_at = self._clock()
        try:
            exit_code, stdout, stderr = await self._run_subprocess(
                command.argv, cwd, command.timeout_seconds
            )
        except ValidationTimeoutError:
            return ValidationResult(
                validation_run_id=validation_run_id, validation_id=command.validation_id,
                kind=command.kind, required=command.required, argv=command.argv,
                status=ValidationStatus.TIMEOUT, started_at=started_at, finished_at=self._clock(),
                environment=environment,
            )
        except FileNotFoundError as exc:
            return ValidationResult(
                validation_run_id=validation_run_id, validation_id=command.validation_id,
                kind=command.kind, required=command.required, argv=command.argv,
                status=ValidationStatus.ERROR, started_at=started_at, finished_at=self._clock(),
                stderr=_bounded(str(exc)), environment=environment,
            )

        finished_at = self._clock()
        status = ValidationStatus.PASSED if exit_code == 0 else ValidationStatus.FAILED
        return ValidationResult(
            validation_run_id=validation_run_id, validation_id=command.validation_id,
            kind=command.kind, required=command.required, argv=command.argv,
            status=status, started_at=started_at, finished_at=finished_at, exit_code=exit_code,
            stdout=_bounded(stdout.decode("utf-8", errors="replace")),
            stderr=_bounded(stderr.decode("utf-8", errors="replace")),
            environment=environment,
        )
