"""Tests for ValidationStore / QualityGateRunner (Phase 1 / Slice 8).

Uses small, fast, controlled local commands (``python -c ...``, ``true``,
``false``, a nonexistent binary) instead of the real project test suite —
running the real suite recursively from inside its own unit tests would be
both slow and a correctness trap. No Claude/Codex/Ralph/network anywhere
in this file.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.validation import (
    CorruptValidationResultError,
    QualityGateRunner,
    UnknownValidationRunError,
    ValidationCommand,
    ValidationKind,
    ValidationResult,
    ValidationStatus,
    ValidationStore,
    ValidationTimeoutError,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
PY = sys.executable


def _store(tmp_path: Path, *, clock=None) -> ValidationStore:
    return ValidationStore(tmp_path / "validation.sqlite3", clock=clock or (lambda: UTC_NOW))


def _cmd(**overrides) -> ValidationCommand:
    fields = dict(
        validation_id="unit-tests", kind=ValidationKind.UNIT_TEST,
        argv=(PY, "-c", "pass"), timeout_seconds=5.0, required=True,
    )
    fields.update(overrides)
    return ValidationCommand(**fields)


def _runner(store: ValidationStore, *, id_factory=None) -> QualityGateRunner:
    counter = {"n": 0}

    def default_id_factory() -> str:
        counter["n"] += 1
        return f"run-{counter['n']}"

    return QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=id_factory or default_id_factory)


class TestConfiguration:
    def test_set_and_get_project_commands(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        cmd = _cmd()
        store.set_project_commands("proj-1", [cmd])

        fetched = store.get_project_commands("proj-1")
        assert fetched == (cmd,)

    def test_argv_is_preserved_exactly(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        cmd = _cmd(argv=("ruff", "check", "."))
        store.set_project_commands("proj-1", [cmd])

        fetched = store.get_project_commands("proj-1")
        assert fetched[0].argv == ("ruff", "check", ".")

    def test_configuration_survives_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "validation.sqlite3"
        store = ValidationStore(db_path, clock=lambda: UTC_NOW)
        store.set_project_commands("proj-1", [_cmd()])
        store.close()

        reopened = ValidationStore(db_path, clock=lambda: UTC_NOW)
        assert reopened.get_project_commands("proj-1") == (_cmd(),)

    def test_set_project_commands_replaces_previous_config(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(validation_id="a")])
        store.set_project_commands("proj-1", [_cmd(validation_id="b")])

        ids = [c.validation_id for c in store.get_project_commands("proj-1")]
        assert ids == ["b"]

    def test_empty_argv_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ValidationCommand(validation_id="x", kind=ValidationKind.LINT, argv=())

    def test_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _cmd(timeout_seconds=0)


class TestCommandExecution:
    def test_successful_command_yields_passed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate.passed is True
        assert gate.results[0].status is ValidationStatus.PASSED
        assert gate.results[0].exit_code == 0

    def test_non_zero_exit_yields_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "import sys; sys.exit(1)"))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate.results[0].status is ValidationStatus.FAILED
        assert gate.results[0].exit_code == 1
        assert gate.passed is False

    def test_missing_binary_yields_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1", [_cmd(argv=("this-binary-does-not-exist-xyz",))]
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate.results[0].status is ValidationStatus.ERROR
        assert gate.results[0].exit_code is None
        assert gate.passed is False

    def test_timeout_yields_timeout_status(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [_cmd(argv=(PY, "-c", "import time; time.sleep(5)"), timeout_seconds=0.2)],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate.results[0].status is ValidationStatus.TIMEOUT
        assert gate.passed is False

    def test_stdout_stderr_are_captured_and_bounded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [_cmd(argv=(PY, "-c", "print('x' * 10000)"))],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert "x" in gate.results[0].stdout
        assert len(gate.results[0].stdout) <= 4100  # bounded, not the full 10000 chars

    def test_cwd_is_respected(self, tmp_path: Path) -> None:
        subdir = tmp_path / "workdir"
        subdir.mkdir()
        (subdir / "marker.txt").write_text("here")
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [_cmd(argv=(PY, "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('marker.txt').exists() else 1)"))],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=subdir))

        assert gate.results[0].status is ValidationStatus.PASSED


class TestQualityGateRule:
    def test_required_passed_gate_passes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"), required=True)])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is True

    def test_required_failed_gate_fails(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1", [_cmd(argv=(PY, "-c", "import sys; sys.exit(1)"), required=True)]
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is False

    def test_optional_failed_does_not_fail_gate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [
                _cmd(validation_id="required-ok", argv=(PY, "-c", "pass"), required=True),
                _cmd(
                    validation_id="optional-broken",
                    argv=(PY, "-c", "import sys; sys.exit(1)"),
                    required=False,
                ),
            ],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate.passed is True
        statuses = {r.validation_id: r.status for r in gate.results}
        assert statuses["optional-broken"] is ValidationStatus.FAILED

    def test_multiple_required_validations_all_must_pass(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [
                _cmd(validation_id="a", argv=(PY, "-c", "pass"), required=True),
                _cmd(validation_id="b", argv=(PY, "-c", "import sys; sys.exit(1)"), required=True),
            ],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is False

    def test_missing_required_result_is_never_interpreted_as_passed(self, tmp_path: Path) -> None:
        # Simulate a required validation that is configured but for which
        # no result was ever recorded for this run (e.g. runner crashed
        # before reaching it) — get_gate_result must still fail-close.
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [
                _cmd(validation_id="ran", argv=(PY, "-c", "pass"), required=True),
                _cmd(validation_id="never-ran", argv=(PY, "-c", "pass"), required=True),
            ],
        )
        only_result = ValidationResult(
            validation_run_id="run-x", validation_id="ran", kind=ValidationKind.UNIT_TEST,
            required=True, argv=(PY, "-c", "pass"), status=ValidationStatus.PASSED,
            started_at=UTC_NOW, finished_at=UTC_NOW,
        )
        store.record_result("run-x", "proj-1", only_result)

        gate = store.get_gate_result("run-x")
        assert gate.passed is False


class TestMultipleRunsAndPersistence:
    def test_multiple_runs_of_same_work_item_are_independent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        first = asyncio.run(
            runner.run_gate(project_id="proj-1", cwd=tmp_path, work_item_id="wi-1")
        )
        second = asyncio.run(
            runner.run_gate(project_id="proj-1", cwd=tmp_path, work_item_id="wi-1")
        )

        assert first.validation_run_id != second.validation_run_id
        assert store.get_gate_result(first.validation_run_id).passed is True
        assert store.get_gate_result(second.validation_run_id).passed is True

    def test_latest_gate_result_for_work_item(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path, work_item_id="wi-1"))
        second = asyncio.run(
            runner.run_gate(project_id="proj-1", cwd=tmp_path, work_item_id="wi-1")
        )

        latest = store.latest_gate_result_for_work_item("wi-1")
        assert latest.validation_run_id == second.validation_run_id

    def test_latest_gate_result_absent_is_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.latest_gate_result_for_work_item("does-not-exist") is None

    def test_results_survive_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "validation.sqlite3"
        store = ValidationStore(db_path, clock=lambda: UTC_NOW)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1")
        asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path, work_item_id="wi-1"))
        store.close()

        reopened = ValidationStore(db_path, clock=lambda: UTC_NOW)
        gate = reopened.get_gate_result("run-1")
        assert gate.passed is True
        assert gate.work_item_id == "wi-1"

    def test_git_sha_captured_when_available(self, tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)

        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.git_sha is not None
        assert len(gate.git_sha) == 40

    def test_git_sha_none_for_non_git_workspace(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.git_sha is None

    def test_unknown_validation_run_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownValidationRunError):
            store.get_gate_result("does-not-exist")


class TestCorruptData:
    def test_corrupt_status_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)
        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        store._conn.execute(  # noqa: SLF001 - deliberately corrupting data for the test
            "UPDATE validation_results SET status = 'not_a_status' WHERE validation_run_id = ?",
            (gate.validation_run_id,),
        )
        store._conn.commit()

        with pytest.raises(CorruptValidationResultError):
            store.get_gate_result(gate.validation_run_id)


class TestNoForbiddenBehavior:
    def test_no_shell_true_anywhere(self) -> None:
        from orchestrator import validation as module

        source = inspect.getsource(module)
        assert "shell=True" not in source

    def test_module_never_calls_claude_codex_ralph_directly(self) -> None:
        from orchestrator import validation as module

        source = inspect.getsource(module)
        for forbidden in ("ClaudeCodeAdapter", "CodexAdapter", "RalphExecutionEngine", '"ralph"', "'ralph'"):
            assert forbidden not in source

    def test_no_git_mutation_commands(self) -> None:
        from orchestrator import validation as module

        source = inspect.getsource(module)
        for forbidden in ('"checkout"', '"commit"', '"merge"', '"reset"', '"clean"', "'add'"):
            assert forbidden not in source
