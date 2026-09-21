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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.validation import (
    CorruptValidationResultError,
    QualityGateRunner,
    ReadOnlyValidationViolationError,
    UnknownValidationRunError,
    ValidationCommand,
    ValidationEnvironmentEvidence,
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


class TestMandatoryManifestHardening:
    """Slice 21.5, point A: an empty (or all-optional) mandatory manifest
    must never be indistinguishable from "all mandatory checks passed" —
    but only when a caller opts into that stricter behavior. Reproduced
    first (see the two "confirms" tests) before being fixed, per the
    session's explicit instruction not to trust the audit report blindly.
    """

    def test_confirms_default_behavior_is_trivial_pass_on_empty_manifest(self, tmp_path: Path) -> None:
        # This is the exact behavior the Codex/Claude audits flagged.
        # Reproduced here first: zero configured commands -> passed=True.
        # It must remain legal by default (e.g. an all-optional gate) —
        # this test pins that pre-existing, still-intentional behavior.
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is True

    def test_mandatory_qa_verification_with_empty_manifest_never_passes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [])
        runner = _runner(store)

        gate = asyncio.run(
            runner.run_gate(
                project_id="proj-1", cwd=tmp_path, require_nonempty_mandatory_manifest=True,
            )
        )
        assert gate.passed is False

    def test_mandatory_qa_verification_with_only_optional_commands_never_passes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(required=False, argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(
            runner.run_gate(
                project_id="proj-1", cwd=tmp_path, require_nonempty_mandatory_manifest=True,
            )
        )
        assert gate.passed is False

    def test_mandatory_qa_verification_with_a_real_required_command_can_pass(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(required=True, argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(
            runner.run_gate(
                project_id="proj-1", cwd=tmp_path, require_nonempty_mandatory_manifest=True,
            )
        )
        assert gate.passed is True


class TestPolicySnapshotHardening:
    """Slice 21.5, point B: a historical gate result must stay bound to the
    manifest actually applied at run time — never recomputed from the
    project's possibly-since-changed *current* configuration.
    """

    def test_confirms_replay_used_to_be_sensitive_to_config_drift(self, tmp_path: Path) -> None:
        # Reproduction of the exact bug: without a recorded manifest,
        # get_gate_result recomputes "passed" from *current*
        # get_project_commands — so adding a new required command later,
        # which never even existed at run time, silently flips an old
        # PASS to FAIL on replay. This test bypasses record_manifest
        # (as pre-Slice-21.5 code always did) to pin the drift as real.
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(validation_id="a", required=True)])
        result = ValidationResult(
            validation_run_id="run-drift", validation_id="a", kind=ValidationKind.UNIT_TEST,
            required=True, argv=(PY, "-c", "pass"), status=ValidationStatus.PASSED,
            started_at=UTC_NOW, finished_at=UTC_NOW,
        )
        store.record_result("run-drift", "proj-1", result)  # no record_manifest call
        assert store.get_gate_result("run-drift").passed is True

        # Policy changes after the fact: a new required check is added
        # that never ran for this historical run.
        store.set_project_commands(
            "proj-1",
            [_cmd(validation_id="a", required=True), _cmd(validation_id="b", required=True)],
        )
        assert store.get_gate_result("run-drift").passed is False  # drifted silently

    def test_manifest_snapshot_keeps_historical_result_stable_after_new_required_check(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(validation_id="a", required=True)])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is True

        # A new required check is added to the project after this run.
        store.set_project_commands(
            "proj-1",
            [_cmd(validation_id="a", required=True), _cmd(validation_id="b", required=True)],
        )

        replayed = store.get_gate_result(gate.validation_run_id)
        assert replayed.passed is True  # bound to the manifest actually applied, unaffected

    def test_manifest_snapshot_survives_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "validation.sqlite3"
        store = ValidationStore(db_path, clock=lambda: UTC_NOW)
        store.set_project_commands("proj-1", [_cmd(validation_id="a", required=True)])
        runner = _runner(store)
        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        store.close()

        # New process/instance, and policy has since changed too.
        reopened = ValidationStore(db_path, clock=lambda: UTC_NOW)
        reopened.set_project_commands(
            "proj-1",
            [_cmd(validation_id="a", required=True), _cmd(validation_id="b", required=True)],
        )
        replayed = reopened.get_gate_result(gate.validation_run_id)
        assert replayed.passed is True  # the original run's own manifest, not today's config

    def test_manifest_snapshot_keeps_historical_result_stable_after_required_flag_toggled(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1", [_cmd(validation_id="a", required=True, argv=(PY, "-c", "import sys; sys.exit(1)"))]
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is False  # "a" failed and was required at run time

        # Someone downgrades "a" to optional after the fact — the old FAIL
        # must not silently become a PASS on replay either.
        store.set_project_commands(
            "proj-1", [_cmd(validation_id="a", required=False, argv=(PY, "-c", "import sys; sys.exit(1)"))]
        )
        replayed = store.get_gate_result(gate.validation_run_id)
        assert replayed.passed is False

    def test_get_gate_result_still_falls_back_without_a_recorded_manifest(self, tmp_path: Path) -> None:
        # Backward compatibility: results written directly via
        # record_result (bypassing run_gate/record_manifest, as some
        # tests and any pre-Slice-21.5 data do) still resolve via the
        # live project configuration — this is the existing
        # TestGateComputation.test_missing_required_result_is_never_interpreted_as_passed
        # behavior, re-asserted here as an explicit manifest-fallback test.
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(validation_id="a", required=True)])
        result = ValidationResult(
            validation_run_id="run-nomanifest", validation_id="a", kind=ValidationKind.UNIT_TEST,
            required=True, argv=(PY, "-c", "pass"), status=ValidationStatus.PASSED,
            started_at=UTC_NOW, finished_at=UTC_NOW,
        )
        store.record_result("run-nomanifest", "proj-1", result)
        assert store.get_manifest_for_run("run-nomanifest") is None
        assert store.get_gate_result("run-nomanifest").passed is True

    def test_run_gate_records_a_manifest_for_every_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(validation_id="a", required=True)])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        manifest = store.get_manifest_for_run(gate.validation_run_id)
        assert manifest is not None
        assert [c.validation_id for c in manifest] == ["a"]


class TestReadOnlyVerificationHardening:
    """Slice 21.5, point C: a gate run declared read-only must fail closed
    if the repository's HEAD changes while its commands run — a future
    mandatory Final QA Verification must never be fooled by a subprocess
    that silently commits.
    """

    @staticmethod
    def _init_repo(tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)

    def test_confirms_a_mutating_command_is_silently_allowed_by_default(self, tmp_path: Path) -> None:
        # Reproduction: without verify_repository_unchanged, a "validation"
        # command that actually commits goes completely unnoticed.
        self._init_repo(tmp_path)
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [_cmd(argv=("git", "commit", "--allow-empty", "-q", "-m", "sneaky"), required=True)],
        )
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is True  # the mutation went completely undetected

    def test_read_only_run_raises_when_head_changes(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        store = _store(tmp_path)
        store.set_project_commands(
            "proj-1",
            [_cmd(argv=("git", "commit", "--allow-empty", "-q", "-m", "sneaky"), required=True)],
        )
        runner = _runner(store)

        with pytest.raises(ReadOnlyValidationViolationError):
            asyncio.run(
                runner.run_gate(project_id="proj-1", cwd=tmp_path, verify_repository_unchanged=True)
            )

    def test_read_only_run_passes_through_when_head_is_stable(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"), required=True)])
        runner = _runner(store)

        gate = asyncio.run(
            runner.run_gate(project_id="proj-1", cwd=tmp_path, verify_repository_unchanged=True)
        )
        assert gate.passed is True


class TestEnvironmentEvidence:
    """Slice 25: every ``ValidationResult`` also records what actually ran
    it — found necessary by a real defect (see
    ``ValidationEnvironmentEvidence``'s own docstring: same head SHA, same
    command, FAIL then PASS because the ambient Python environment
    silently changed between two QA attempts)."""

    def test_real_python_command_is_recognized_as_python(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        environment = gate.results[0].environment
        assert environment is not None
        assert environment.resolved_executable == PY
        assert environment.python_executable == PY
        assert environment.python_version is not None
        assert environment.sys_prefix is not None
        assert environment.packages_fingerprint is not None
        assert environment.probe_error is None

    def test_non_python_command_has_no_python_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=("true",))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        environment = gate.results[0].environment
        assert environment is not None
        assert environment.resolved_executable is not None
        assert environment.python_executable is None
        assert environment.packages_fingerprint is None

    def test_missing_binary_still_records_environment_with_probe_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=("this-binary-does-not-exist-xyz",))])
        runner = _runner(store)

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        environment = gate.results[0].environment
        assert environment is not None
        assert environment.resolved_executable is None
        assert environment.probe_error is not None

    def test_identical_environment_probe_yields_identical_fingerprint(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        runner = _runner(store)

        gate1 = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        gate2 = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))

        assert gate1.results[0].environment.fingerprint == gate2.results[0].environment.fingerprint

    def test_custom_environment_probe_is_used_and_persisted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])
        fake_evidence = ValidationEnvironmentEvidence(
            resolved_executable="/fake/pytest", path_value="/fake/bin", python_executable="/fake/python",
            python_version="3.99", sys_prefix="/fake/venv", packages_fingerprint="deadbeef",
        )
        runner = QualityGateRunner(
            store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1",
            environment_probe=lambda argv, cwd: fake_evidence,
        )

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.results[0].environment == fake_evidence

        fingerprints = store.get_environment_fingerprints(gate.validation_run_id)
        assert fingerprints == {"unit-tests": fake_evidence.fingerprint}

    def test_environment_probe_raising_never_crashes_the_gate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_project_commands("proj-1", [_cmd(argv=(PY, "-c", "pass"))])

        def _boom(argv, cwd):
            raise RuntimeError("probe exploded")

        runner = QualityGateRunner(
            store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1", environment_probe=_boom,
        )

        gate = asyncio.run(runner.run_gate(project_id="proj-1", cwd=tmp_path))
        assert gate.passed is True  # the command itself still ran fine
        assert gate.results[0].environment is not None
        assert "probe exploded" in gate.results[0].environment.probe_error

    def test_old_rows_without_environment_decode_as_none(self, tmp_path: Path) -> None:
        # Simulates a pre-Slice-25 database row (inserted before
        # environment_json existed): must decode honestly as unknown,
        # never a fabricated fingerprint.
        db_path = tmp_path / "validation.sqlite3"
        store = ValidationStore(db_path, clock=lambda: UTC_NOW)
        with store._conn:
            store._conn.execute(
                "INSERT INTO validation_results (validation_run_id, project_id, mvp_id, work_item_id, "
                "validation_id, kind, required, argv, status, started_at, finished_at, exit_code, stdout, "
                "stderr, git_sha) VALUES (?, ?, NULL, NULL, ?, ?, 1, ?, ?, ?, ?, 0, '', '', NULL)",
                (
                    "legacy-run", "proj-1", "unit-tests", ValidationKind.UNIT_TEST.value, json.dumps(["pytest"]),
                    ValidationStatus.PASSED.value, UTC_NOW.isoformat(), UTC_NOW.isoformat(),
                ),
            )

        gate = store.get_gate_result("legacy-run")
        assert gate.results[0].environment is None
        assert store.get_environment_fingerprints("legacy-run") == {"unit-tests": None}

    def test_migration_is_idempotent_on_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "validation.sqlite3"
        ValidationStore(db_path, clock=lambda: UTC_NOW).close()
        # Reopening an already-migrated database must not raise (e.g. a
        # naive unconditional "ALTER TABLE ADD COLUMN" would fail the
        # second time).
        reopened = ValidationStore(db_path, clock=lambda: UTC_NOW)
        reopened.set_project_commands("proj-1", [_cmd()])
        assert reopened.get_project_commands("proj-1") == (_cmd(),)
