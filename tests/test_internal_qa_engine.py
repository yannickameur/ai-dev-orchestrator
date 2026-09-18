"""Tests for InternalQAEngine (Phase 1 / Slice 23).

Foundational engine/gate-reuse tests use real, temporary git repositories
and the real ``python -m pytest`` binary (no git/pytest mocking) — this
file creates and destroys disposable repos under ``tmp_path`` for every
test. No real Claude/Codex/Ralph invocation, no network, no reset credit
consumed anywhere in this file.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.adaptive_execution import AdaptiveExecutionDecisionStore, AdaptiveExecutionSelector
from orchestrator.complexity_estimation import ComplexityEstimationRequest, ExecutionRecommendation
from orchestrator.git_governance import LocalGitWorkspace
from orchestrator.internal_qa_engine import (
    ENGINE_ID,
    InternalQAEngine,
    InternalQAPlan,
    NoEvidenceAvailableError,
    UnsupportedStackError,
    classify_validation_status,
    run_qa_cycle,
    working_tree_changed_files,
)
from orchestrator.qa import (
    FailureClassification,
    QAEngine,
    QAEvidenceManifest,
    QAPhase,
    QAPolicy,
    QARequest,
    QARun,
    QARunStatus,
    QARunStore,
    QAResult,
    QAVerdictStatus,
    evaluate_qa_verdict,
)
from orchestrator.qa_knowledge import (
    CriticalPath,
    KnownFlakyEntry,
    QAInvariant,
    RegressionMapEntry,
    write_critical_paths,
    write_invariants,
    write_known_flaky,
    write_regression_map,
)
from orchestrator.qa_protection import (
    ExpectedChangeSource,
    TestChangeAuthorization,
    capture_protected_test_baseline,
)
from orchestrator.ralph_execution_engine import ExecutionRequest, RalphEvent, RalphExecutionEngine
from orchestrator.execution_store import ExecutionStore
from orchestrator.validation import (
    QualityGateRunner,
    ValidationCommand,
    ValidationKind,
    ValidationStatus,
    ValidationStore,
)
from orchestrator.worker_selector import (
    ExecutionProfile,
    NoEligibleWorkerError,
    QualityTier,
    Worker,
    WorkerSelectionRequest,
)

UTC_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PY = sys.executable


# --- real-git helpers (mirrors tests/test_git_governance.py) --------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "pyproject.toml").write_text("[project]\nname = \"demo\"\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom app import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n"
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "initial commit")
    return repo


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> str:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run_git(repo, "add", rel_path)
    _run_git(repo, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _head(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


# --- domain fixtures --------------------------------------------------------


def _validation_store(tmp_path: Path, name: str = "validation.sqlite3") -> ValidationStore:
    return ValidationStore(tmp_path / name, clock=lambda: UTC_NOW)


def _qa_run_store(tmp_path: Path, name: str = "qa_runs.sqlite3") -> QARunStore:
    return QARunStore(tmp_path / name, clock=lambda: UTC_NOW)


def _gate_runner(store: ValidationStore) -> QualityGateRunner:
    return QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=_counting_id_factory("run"))


def _engine(validation_store: ValidationStore) -> InternalQAEngine:
    return InternalQAEngine(validation_store=validation_store, gate_runner=_gate_runner(validation_store), clock=lambda: UTC_NOW)


def _counting_id_factory(prefix: str = "id"):
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return id_factory


def _qa_request(repo: Path, *, head_sha: str | None = None, base_sha: str | None = None, **overrides) -> QARequest:
    fields = dict(
        project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", workspace=str(repo),
        base_sha=base_sha or _head(repo), head_sha=head_sha or _head(repo),
        objective="Fix add() to actually add",
    )
    fields.update(overrides)
    return QARequest(**fields)


def _policy(**overrides) -> QAPolicy:
    fields = dict()
    fields.update(overrides)
    return QAPolicy(**fields)


# --- Part A/B/Q: engine + deterministic plan --------------------------------


class TestEngineIsAQAEngine:
    def test_engine_satisfies_qa_engine_protocol(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        store = _validation_store(tmp_path)
        engine: QAEngine = _engine(store)
        result = engine.run(_qa_request(repo, required_test_ids=("tests/test_app.py",)))
        assert isinstance(result, QAResult)
        assert result.engine_id == ENGINE_ID

    def test_python_stack_is_supported(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        assert _engine(_validation_store(tmp_path)).stack_supported(repo) is True

    def test_unsupported_stack_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "no-python"
        repo.mkdir()
        _run_git(repo, "init", "-b", "main")
        _run_git(repo, "config", "user.email", "t@example.com")
        _run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("hello")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "seed")
        with pytest.raises(UnsupportedStackError):
            asyncio.run(_engine(_validation_store(tmp_path)).run_async(_qa_request(repo)))

    def test_unsupported_stack_yields_inconclusive_via_run_qa_cycle(self, tmp_path: Path) -> None:
        repo = tmp_path / "no-python"
        repo.mkdir()
        _run_git(repo, "init", "-b", "main")
        _run_git(repo, "config", "user.email", "t@example.com")
        _run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("hello")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "seed")
        store = _validation_store(tmp_path)
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(store), run_store=run_store, request=_qa_request(repo),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.status is QARunStatus.FAILED
        assert run.verdict.status is QAVerdictStatus.INCONCLUSIVE


class TestDeterministicTestImpact:
    def _kb_repo(self, tmp_path: Path) -> Path:
        repo = _init_repo(tmp_path)
        write_regression_map(
            repo, [RegressionMapEntry(entry_id="app", paths=("src/app.py",), related_tests=("tests/test_app.py",), invariant_ids=("INV-1",))],
        )
        write_critical_paths(
            repo, [CriticalPath(path_id="core", paths=("src/",), required_tests=("tests/test_app.py",), required_invariant_ids=("INV-1",))],
        )
        write_invariants(repo, [QAInvariant(invariant_id="INV-1", description="add works", related_tests=("tests/test_app.py",))])
        return repo

    def test_regression_map_tests_included(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        plan = _engine(_validation_store(tmp_path)).build_plan(_qa_request(repo, changed_files=("src/app.py",)))
        assert "tests/test_app.py" in plan.selected_tests
        assert "app" in plan.impacted_areas

    def test_critical_path_tests_included(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        plan = _engine(_validation_store(tmp_path)).build_plan(_qa_request(repo, changed_files=("src/app.py",)))
        assert "core" in plan.impacted_areas
        assert "INV-1" in plan.required_invariants

    def test_unrelated_change_invents_nothing(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        plan = _engine(_validation_store(tmp_path)).build_plan(_qa_request(repo, changed_files=("docs/unrelated.md",)))
        assert plan.selected_tests == ()
        assert plan.impacted_areas == ()

    def test_selected_tests_drive_targeted_command(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        plan = _engine(_validation_store(tmp_path)).build_plan(_qa_request(repo, changed_files=("src/app.py",)))
        assert len(plan.targeted_commands) == 1
        argv = plan.targeted_commands[0].argv
        assert argv[:3] == (PY, "-m", "pytest")
        assert "tests/test_app.py" in argv

    def test_no_selection_but_global_regression_configured_falls_back(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)  # no .qa/ knowledge at all
        store = _validation_store(tmp_path)
        store.set_project_commands(
            "proj-1", [ValidationCommand(validation_id="full-suite", kind=ValidationKind.UNIT_TEST, argv=(PY, "-m", "pytest", "-q"))],
        )
        plan = _engine(store).build_plan(_qa_request(repo))
        assert plan.selected_tests == ()
        assert len(plan.regression_commands) == 1

    def test_no_selection_and_no_regression_configured_is_no_evidence(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        store = _validation_store(tmp_path)
        with pytest.raises(NoEvidenceAvailableError):
            asyncio.run(_engine(store).run_async(_qa_request(repo)))

    def test_no_evidence_yields_inconclusive_never_pass(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        store = _validation_store(tmp_path)
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(store), run_store=run_store, request=_qa_request(repo),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.INCONCLUSIVE

    def test_pytest_id_forms_supported(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(
            repo, [RegressionMapEntry(
                entry_id="a", paths=("src/",),
                related_tests=("tests/test_app.py", "tests/test_app.py::test_add", "tests/test_app.py::Cls::test_x"),
            )],
        )
        plan = _engine(_validation_store(tmp_path)).build_plan(_qa_request(repo, changed_files=("src/app.py",)))
        argv = plan.targeted_commands[0].argv
        assert "tests/test_app.py::test_add" in argv
        assert "tests/test_app.py::Cls::test_x" in argv


# --- QualityGateRunner reuse -------------------------------------------------


class TestQualityGateRunnerReuse:
    def test_targeted_run_actually_invokes_quality_gate_runner(self, tmp_path: Path) -> None:
        repo = self._kb_repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        store = _validation_store(tmp_path)
        result = asyncio.run(_engine(store).run_async(_qa_request(repo, changed_files=("src/app.py",))))
        assert result.tests_executed == ("qa-targeted-pytest",)
        assert result.passed_count == 1
        assert result.failed_count == 0

    def test_no_duplicate_subprocess_test_runner_in_module(self) -> None:
        import inspect
        from orchestrator import internal_qa_engine as module

        source = inspect.getsource(module)
        # The only subprocess.run calls in this module are read-only git
        # fact-gathering (_run_git/_observed_head) — test execution must
        # go exclusively through QualityGateRunner.run_gate.
        assert "asyncio.create_subprocess" not in source
        assert 'subprocess.run(["git"' in source or "_run_git(" in source

    def test_shell_false_everywhere(self) -> None:
        import inspect
        from orchestrator import internal_qa_engine as module

        assert "shell=True" not in inspect.getsource(module)

    def test_non_zero_pytest_never_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
        _run_git(repo, "commit", "-am", "break add")
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        store = _validation_store(tmp_path)
        result = asyncio.run(_engine(store).run_async(_qa_request(repo, changed_files=("src/app.py",))))
        assert result.failed_count == 1
        assert result.regressions

    def test_timeout_never_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(5)\n")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "slow test")
        store = _validation_store(tmp_path)
        engine = InternalQAEngine(validation_store=store, gate_runner=QualityGateRunner(store, clock=lambda: UTC_NOW), clock=lambda: UTC_NOW)
        plan = InternalQAPlan(
            selected_tests=("tests/test_slow.py",),
            targeted_commands=(ValidationCommand(validation_id="qa-targeted-pytest", kind=ValidationKind.UNIT_TEST, argv=(PY, "-m", "pytest", "-q", "tests/test_slow.py"), timeout_seconds=0.2),),
        )
        result = asyncio.run(engine.run_async(_qa_request(repo), plan=plan))
        assert result.failed_count == 1


# --- QARun/QAResult/QAVerdict persistence -----------------------------------


class TestQARunPersistence:
    def test_qa_run_created_and_persisted(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run_store.get(run.run_id) == run
        assert run.status is QARunStatus.COMPLETED

    def test_qa_result_persisted(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run_store.get_result(run.run_id) is not None

    def test_verdict_computed_only_by_governance_evaluator(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        result = run_store.get_result(run.run_id)
        expected = evaluate_qa_verdict(run=run_store.get(run.run_id), result=result, now=UTC_NOW)
        assert run.verdict.status == expected.status

    def test_engine_claimed_pass_cannot_override_governed_fail(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        # observed_head_sha will be the real repo head (engine tells the
        # truth about the SHA), but we request a *different* expected
        # head — the engine's PASS-shaped result must not override the
        # governed SHA-mismatch FAIL.
        request = _qa_request(repo, changed_files=("src/app.py",), head_sha="0" * 40)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store, request=request,
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        result = run_store.get_result(run.run_id)
        assert result.engine_reported_status == "PASS"  # the tests really passed
        assert run.verdict.status is QAVerdictStatus.FAIL  # but SHA never matched
        assert "observed_head_sha" in run.verdict.reason

    def test_manifest_snapshot_used(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert not run.manifest.is_empty
        assert "tests/test_app.py" in run.manifest.required_test_ids

    def test_completed_run_reloads_after_restart(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        db_path = tmp_path / "restart_qa.sqlite3"
        run_store = QARunStore(db_path, clock=lambda: UTC_NOW)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        run_store.close()

        reopened = QARunStore(db_path, clock=lambda: UTC_NOW)
        reloaded = reopened.get(run.run_id)
        assert reloaded.status is QARunStatus.COMPLETED
        assert reloaded.verdict.status == run.verdict.status
        assert reopened.get_result(run.run_id) is not None

    def test_interrupted_run_never_yields_pass(self, tmp_path: Path) -> None:
        run_store = _qa_run_store(tmp_path)
        from orchestrator.qa import new_qa_run
        run = new_qa_run(
            project_id="p", mvp_id="m", work_item_id="wi", engine_id=ENGINE_ID, phase=QAPhase.FINAL_VERIFICATION,
            expected_base_sha="a" * 40, expected_head_sha="b" * 40, policy=_policy(),
            manifest=QAEvidenceManifest(required_test_ids=("t",), deterministic_commands=("x",)),
            clock=lambda: UTC_NOW,
        )
        run_store.create(run)
        run_store.update_status(run.run_id, QARunStatus.RUNNING)
        run_store.update_status(run.run_id, QARunStatus.INTERRUPTED)
        verdict = evaluate_qa_verdict(run=run_store.get(run.run_id), result=None, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.INCONCLUSIVE

    def test_result_and_verdict_are_immutable_historical_facts(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        run_store = _qa_run_store(tmp_path)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",)),
            phase=QAPhase.TEST_AUTHORING, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        from orchestrator.qa import ResultAlreadyRecordedError, VerdictAlreadyRecordedError
        with pytest.raises(ResultAlreadyRecordedError):
            run_store.record_result(run.run_id, run_store.get_result(run.run_id))
        with pytest.raises(VerdictAlreadyRecordedError):
            run_store.record_verdict(run.run_id, run.verdict)


# --- Part C: read-only Final Verification -----------------------------------


class TestFinalVerification:
    def _kb_repo(self, tmp_path: Path) -> Path:
        repo = _init_repo(tmp_path)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        return repo

    def test_read_only_success_yields_pass(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        run_store = _qa_run_store(tmp_path)
        head = _head(repo)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",), base_sha=head, head_sha=head),
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.PASS
        assert _head(repo) == head  # workspace genuinely untouched

    def test_head_mutation_during_final_verification_fails(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        run_store = _qa_run_store(tmp_path)
        store = _validation_store(tmp_path)
        head = _head(repo)
        # A "validation command" that mutates HEAD — simulates a rogue
        # command; run_final_verification_gate must catch this.
        store.set_project_commands(
            "proj-1", [ValidationCommand(validation_id="sneaky", kind=ValidationKind.CUSTOM, argv=("git", "commit", "--allow-empty", "-m", "sneaky"))],
        )
        engine = _engine(store)
        request = _qa_request(repo, changed_files=("src/app.py",), base_sha=head, head_sha=head)
        run = asyncio.run(run_qa_cycle(
            engine=engine, run_store=run_store, request=request,
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.FAIL
        assert "read-only" in run.verdict.reason

    def test_exact_sha_observed(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        run_store = _qa_run_store(tmp_path)
        head = _head(repo)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",), base_sha=head, head_sha=head),
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        result = run_store.get_result(run.run_id)
        assert result.observed_head_sha == head

    def test_noise_only_advance_before_verification_still_passes(self, tmp_path: Path) -> None:
        """Slice 24 fix, found via real-provider self-dogfood acceptance:
        a review execution this module never expected to touch git can
        still advance the real branch tip via Ralph's own housekeeping
        commit BEFORE Final QA Verification even starts — the SHA
        ``request.head_sha`` names is the one MVPManager governs
        (unaffected), but the *actual* live tip is now one commit ahead.
        This must not be reported as a wrong-SHA FAIL."""
        repo = self._kb_repo(tmp_path)
        run_store = _qa_run_store(tmp_path)
        expected_head = _head(repo)
        (repo / ".ralph").mkdir()
        _commit_file(repo, ".ralph/loop-state.json", "{}", "chore: auto-commit before merge (loop primary)")
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",), base_sha=expected_head, head_sha=expected_head),
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.PASS
        result = run_store.get_result(run.run_id)
        assert result.observed_head_sha == expected_head

    def test_a_real_change_before_verification_still_fails_wrong_sha(self, tmp_path: Path) -> None:
        """Noise tolerance never widens what counts as a real change: an
        actual production-file commit landing before verification starts
        still fails as a genuine SHA mismatch."""
        repo = self._kb_repo(tmp_path)
        run_store = _qa_run_store(tmp_path)
        expected_head = _head(repo)
        real_head = _commit_file(repo, "src/other.py", "x = 1\n", "an unaccounted-for real change")
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store,
            request=_qa_request(repo, changed_files=("src/app.py",), base_sha=expected_head, head_sha=expected_head),
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.FAIL
        assert "observed_head_sha" in run.verdict.reason
        result = run_store.get_result(run.run_id)
        assert result.observed_head_sha == real_head

    def test_inability_to_prove_read_only_is_inconclusive(self, tmp_path: Path) -> None:
        # A non-git workspace: run_final_verification_gate cannot compare
        # SHAs at all -> read_only_unprovable=True -> INCONCLUSIVE.
        repo = tmp_path / "not-a-repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        store = _validation_store(tmp_path)
        store.set_project_commands("proj-1", [ValidationCommand(validation_id="noop", kind=ValidationKind.CUSTOM, argv=(PY, "-c", "pass"))])
        run_store = _qa_run_store(tmp_path)
        request = QARequest(
            project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", workspace=str(repo),
            base_sha="a" * 40, head_sha="a" * 40, objective="check",
        )
        run = asyncio.run(run_qa_cycle(
            engine=_engine(store), run_store=run_store, request=request,
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.INCONCLUSIVE

    def test_protected_test_mutation_during_final_verification_fails(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        head = _head(repo)
        baseline = capture_protected_test_baseline(repo, ["tests/test_app.py"], base_sha=head)
        # Simulate an unauthorized change to the protected test file after
        # the baseline was captured (e.g. a rogue authoring step).
        (repo / "tests" / "test_app.py").write_text(
            "import sys\nsys.path.insert(0, 'src')\nfrom app import add\n\ndef test_add():\n    assert True\n"
        )
        run_store = _qa_run_store(tmp_path)
        request = _qa_request(repo, changed_files=("src/app.py",), base_sha=head, head_sha=head)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store, request=request,
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(), protected_baseline=baseline, clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.FAIL
        assert "protected" in run.verdict.reason

    def test_authorized_protected_test_change_does_not_block(self, tmp_path: Path) -> None:
        repo = self._kb_repo(tmp_path)
        head = _head(repo)
        baseline = capture_protected_test_baseline(repo, ["tests/test_app.py"], base_sha=head)
        (repo / "tests" / "test_app.py").write_text(
            "import sys\nsys.path.insert(0, 'src')\nfrom app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
        authorization = TestChangeAuthorization(
            path="tests/test_app.py", source=ExpectedChangeSource.HUMAN_APPROVAL,
            justification="reformatted, no semantic change", authorized_at=UTC_NOW,
        )
        run_store = _qa_run_store(tmp_path)
        request = _qa_request(repo, changed_files=("src/app.py",), base_sha=head, head_sha=head)
        run = asyncio.run(run_qa_cycle(
            engine=_engine(_validation_store(tmp_path)), run_store=run_store, request=request,
            phase=QAPhase.FINAL_VERIFICATION, policy=_policy(),
            protected_baseline=baseline, authorizations={"tests/test_app.py": authorization},
            clock=lambda: UTC_NOW,
        ))
        assert run.verdict.status is QAVerdictStatus.PASS


# --- Part F/G: failure classification + known-flaky -------------------------


class TestFailureClassification:
    def test_environment_failure_from_timeout(self) -> None:
        assert classify_validation_status(ValidationStatus.TIMEOUT) is FailureClassification.ENVIRONMENT_FAILURE

    def test_environment_failure_from_error(self) -> None:
        assert classify_validation_status(ValidationStatus.ERROR) is FailureClassification.ENVIRONMENT_FAILURE

    def test_plain_failure_stays_unknown_without_more_context(self) -> None:
        assert classify_validation_status(ValidationStatus.FAILED) is FailureClassification.UNKNOWN

    def test_regression_evidence_represented_in_result(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
        _run_git(repo, "commit", "-am", "break add")
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        result = asyncio.run(_engine(_validation_store(tmp_path)).run_async(_qa_request(repo, changed_files=("src/app.py",))))
        assert result.regressions
        assert FailureClassification.UNKNOWN in result.failure_classifications


class TestKnownFlaky:
    def test_known_flaky_never_auto_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
        _run_git(repo, "commit", "-am", "break add")
        write_known_flaky(repo, [KnownFlakyEntry(test_id="tests/test_app.py", evidence="seen intermittently", retry_policy="2")])
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        result = asyncio.run(_engine(_validation_store(tmp_path)).run_async(_qa_request(repo, changed_files=("src/app.py",))))
        # Deterministically broken every time: known-flaky does not turn
        # a real, reproducible failure into a pass.
        assert result.failed_count == 1
        assert result.regressions

    def test_bounded_retry_recovers_and_is_classified_flaky(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        # A genuinely flaky test: fails once (marker file absent), then
        # passes (marker file present after its first execution) — real,
        # deterministic flakiness via a side-effecting test, not a mock.
        (repo / "tests" / "test_flaky.py").write_text(
            "import pathlib\n"
            "def test_flaky():\n"
            "    marker = pathlib.Path(__file__).parent / '.seen'\n"
            "    if not marker.exists():\n"
            "        marker.write_text('x')\n"
            "        assert False, 'first attempt fails on purpose'\n"
            "    assert True\n"
        )
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "add flaky test")
        write_known_flaky(repo, [KnownFlakyEntry(test_id="tests/test_flaky.py", evidence="intermittent", retry_policy="2")])
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("tests/test_flaky.py",), related_tests=("tests/test_flaky.py",))])

        result = asyncio.run(_engine(_validation_store(tmp_path)).run_async(
            _qa_request(repo, changed_files=("tests/test_flaky.py",))
        ))
        assert result.failed_count == 0  # ultimately passed on retry
        assert not result.regressions
        assert FailureClassification.FLAKY_TEST in result.failure_classifications
        assert result.risks  # retry evidence retained, never silently dropped

    def test_bounded_retry_never_infinite(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "tests" / "test_always_fails.py").write_text("def test_always_fails():\n    assert False\n")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "add always-failing test")
        write_known_flaky(repo, [KnownFlakyEntry(test_id="tests/test_always_fails.py", evidence="thought flaky", retry_policy="2")])
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("tests/",), related_tests=("tests/test_always_fails.py",))])

        result = asyncio.run(_engine(_validation_store(tmp_path)).run_async(
            _qa_request(repo, changed_files=("tests/test_always_fails.py",))
        ))
        assert result.failed_count == 1  # never passed, exhausted its bounded budget
        assert result.regressions
        assert result.risks
        assert "attempts=" in result.risks[0]
        # exactly 1 initial + 2 retries = 3 attempts recorded, never unbounded
        import ast
        attempts = ast.literal_eval(result.risks[0].split("attempts=")[1])
        assert len(attempts) == 3


# --- Part J: provider independence, no schema leakage -----------------------


class TestExternalEngineCompatibility:
    def test_internal_engine_and_fake_external_share_the_same_evaluator(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        from orchestrator.qa import QAEngineCapabilities

        class _FakeExternalEngine:
            engine_id = "fake-external"

            def run(self, request: QARequest) -> QAResult:
                return QAResult(
                    engine_id=self.engine_id, observed_head_sha=request.head_sha,
                    started_at=UTC_NOW, finished_at=UTC_NOW, tests_executed=("suite",),
                    passed_count=1, engine_reported_status="PASS",
                )

        real_engine = _engine(_validation_store(tmp_path))
        assert isinstance(real_engine, InternalQAEngine)
        write_regression_map(repo, [RegressionMapEntry(entry_id="a", paths=("src/",), related_tests=("tests/test_app.py",))])
        internal_result = real_engine.run(_qa_request(repo, changed_files=("src/app.py",)))
        external_result = _FakeExternalEngine().run(_qa_request(repo))
        # Same downstream governance function, unmodified, for both.
        run = QARun(
            run_id="r1", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40,
            expected_head_sha=internal_result.observed_head_sha, policy=_policy(),
            manifest=QAEvidenceManifest(required_test_ids=("t",)), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        v1 = evaluate_qa_verdict(run=run, result=internal_result, now=UTC_NOW)
        run2 = QARun(
            run_id="r2", project_id="p", mvp_id="m", work_item_id="wi", engine_id="fake-external",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40,
            expected_head_sha=external_result.observed_head_sha, policy=_policy(),
            manifest=QAEvidenceManifest(required_test_ids=("t",)), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        v2 = evaluate_qa_verdict(run=run2, result=external_result, now=UTC_NOW)
        assert v1.status is QAVerdictStatus.PASS
        assert v2.status is QAVerdictStatus.PASS
        # QAEngineCapabilities carries no vendor-specific field structurally.
        caps = QAEngineCapabilities(engine_id="fake-external", can_execute_existing_tests=True)
        assert caps.engine_id == "fake-external"

    def test_no_provider_names_hardcoded_in_engine_module(self) -> None:
        # The module docstring legitimately *names* external products once,
        # as an honesty statement about what this MVP does NOT claim to
        # support (Part O) — that is not the same as depending on one.
        # What must never appear is a vendor name used as a live code
        # value (a string literal assigned to an id/capability).
        import inspect
        from orchestrator import internal_qa_engine as module

        source = inspect.getsource(module)
        code_text = source.replace(module.__doc__ or "", "")
        for forbidden in ("TestSprite", "BrowserStack", "Momentic", "Diffblue"):
            assert forbidden not in code_text
        assert ENGINE_ID == "internal"

    def test_no_internal_only_fields_on_qa_request_or_result(self) -> None:
        request_fields = set(QARequest.__dataclass_fields__)
        result_fields = set(QAResult.__dataclass_fields__)
        # These contracts were never touched by this slice (Slice 22
        # remains the sole owner) — asserted here as a durable regression
        # guard, not just "currently true by omission".
        assert "internal_qa_plan" not in request_fields
        assert "internal_qa_plan" not in result_fields
        assert "worker_id" not in request_fields  # engine-agnostic, no adaptive-worker leakage
        assert "worker_id" not in result_fields
