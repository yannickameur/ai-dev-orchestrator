#!/usr/bin/env python3
"""Self-dogfood acceptance: ai-dev-orchestrator drives DEV -> QA on a
DISPOSABLE COPY OF ITSELF (Slice 23 acceptance, user-requested addendum).

CONTROL PLANE (this repo, ~/projects/ai-dev-orchestrator) is NEVER
modified while this runs — verified byte-identical before/after (HEAD +
`git status --short`), except the one historical report file this script
is explicitly allowed to write: docs/reports/self-dogfood-dev-qa-2026-09-14.html

TARGET PLANE is a full disposable copy of this repo under /tmp — never
the original, ever used as a workspace.

Sequence (all real, no shortcuts): controlled defect introduced only in
the copy -> real governed WorkItem -> real DEVELOPMENT worker (adaptive
selection, config/workers.yaml, real quota probe) fixes it -> real,
DISTINCT QA Test Authoring worker checks/adds regression coverage,
touching no production code -> real, read-only QA Final Verification on
the exact resulting HEAD -> governed QAVerdict via evaluate_qa_verdict
(never from LLM text) -> mandatory negative control (same QA verification
against the still-broken defect must never PASS).

NEVER run this via pytest. Run explicitly:

    python scripts/self_dogfood_dev_qa_real.py

If any required provider/worker is unavailable, this reports
SELF_DOGFOOD=BLOCKED_BY_PROVIDER and does not claim full acceptance —
this is an accepted, non-failing outcome, not a bug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from orchestrator.adaptive_execution import AdaptiveExecutionDecisionStore, AdaptiveExecutionSelector  # noqa: E402
from orchestrator.complexity_estimation import (  # noqa: E402
    ComplexityEstimationRequest,
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
)
from orchestrator.execution_store import ExecutionStore  # noqa: E402
from orchestrator.git_governance import GitWorkItemStore  # noqa: E402
from orchestrator.handoff import HandoffStore  # noqa: E402
from orchestrator.internal_qa_engine import (  # noqa: E402
    QA_TESTING_CAPABILITY,
    InternalQAEngine,
    InternalQATestAuthor,
    run_qa_cycle,
)
from orchestrator.mvp_manager import DEFAULT_WORK_ITEM_ROLE  # noqa: E402
from orchestrator.project_state import ProjectStateStore  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.qa import QAPhase, QAPolicy, QARequest, QARunStore, QAVerdictStatus  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import ExecutionRequest, RalphExecutionEngine  # noqa: E402
from orchestrator.realization_report import RealizationReportService, RealizationReportStore, write_html  # noqa: E402
from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore  # noqa: E402
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import NoEligibleWorkerError, WorkerSelector  # noqa: E402

WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"
PROJECT_ID = "proj-self-dogfood"
MVP_ID = "mvp-self-dogfood"
WORK_ITEM_ID = "wi-self-dogfood-fix-mismatch"

# --- the controlled defect: real, small, local, no-network, unambiguous ---
DEFECT_FILE = "src/orchestrator/internal_qa_engine.py"
DEFECT_FIND = 'def classify_validation_status(status: ValidationStatus) -> FailureClassification:\n    """Deterministic-first classification for one command\'s outcome.\n    Never claims REGRESSION/TEST_DEFECT/EXPECTED_CHANGE without more\n    context than a bare pytest exit code provides — those stay UNKNOWN,\n    a conservative and acceptable outcome per the task brief."""\n    if status is ValidationStatus.TIMEOUT:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    if status is ValidationStatus.ERROR:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    return FailureClassification.UNKNOWN'
DEFECT_REPLACE = 'def classify_validation_status(status: ValidationStatus) -> FailureClassification:\n    """Deterministic-first classification for one command\'s outcome.\n    Never claims REGRESSION/TEST_DEFECT/EXPECTED_CHANGE without more\n    context than a bare pytest exit code provides — those stay UNKNOWN,\n    a conservative and acceptable outcome per the task brief."""\n    if status is ValidationStatus.TIMEOUT:\n        return FailureClassification.UNKNOWN\n    if status is ValidationStatus.ERROR:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    return FailureClassification.UNKNOWN'
DEFECT_TEST_ID = "tests/test_internal_qa_engine.py::TestFailureClassification::test_environment_failure_from_timeout"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


async def _check_quota() -> dict:
    manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()},
        QuotaPolicy(state_ttl=timedelta(seconds=1)), clock=_utcnow,
    )
    result = {}
    for provider in ("anthropic", "openai"):
        try:
            state = await manager.get(provider)
            result[provider] = {
                "available": state.availability.available,
                "reason": state.availability.reason.value if state.availability.reason else None,
            }
        except ProviderProbeError as exc:
            result[provider] = {"available": False, "reason": f"probe_error: {exc}"}
    return result


def _blocked(reason: str, quota: dict) -> int:
    print(f"[self-dogfood] BLOCKED_BY_PROVIDER: {reason}")
    print(json.dumps({"self_dogfood_status": "BLOCKED_BY_PROVIDER", "reason": reason, "quota": quota}, indent=2))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    print(f"[self-dogfood] pid={os.getpid()}")
    source_head_before = _run_git(REPO_ROOT, "rev-parse", "HEAD")
    source_status_before = subprocess.run(
        ["git", "status", "--short"], cwd=str(REPO_ROOT), capture_output=True, text=True,
    ).stdout
    print(f"[self-dogfood] control plane HEAD before: {source_head_before}")
    print(f"[self-dogfood] control plane status before:\n{source_status_before or '(clean)'}")

    print("[self-dogfood] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))
    available_providers = {p for p, v in quota.items() if v.get("available") is True}
    if not available_providers:
        return _blocked("no provider available at all", quota)

    ctx_dir = Path(tempfile.mkdtemp(prefix="ai-dev-orchestrator-self-dogfood-"))
    workspace = ctx_dir / "workspace"
    print(f"[self-dogfood] copying control plane -> disposable target plane: {workspace}")
    shutil.copytree(REPO_ROOT, workspace, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"))

    # CRITICAL ISOLATION FIX: this process's own venv has `orchestrator`
    # pip-installed in EDITABLE mode, resolving back to the CONTROL
    # plane's src/ regardless of cwd — without this, every pytest
    # invocation below (ours AND the real workers') would silently test
    # the control plane's code, not the disposable copy's, making the
    # entire self-dogfood scenario meaningless. Prepending PYTHONPATH to
    # the copy's own src/ makes it resolve first (verified: PYTHONPATH
    # entries precede the editable-install site-packages finder in
    # sys.path). Set on THIS process's environ so it propagates to every
    # subprocess spawned below, including the real worker's own process
    # tree (Ralph -> claude/codex CLI -> their shell tool calls).
    os.environ["PYTHONPATH"] = str(workspace / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    print(f"[self-dogfood] PYTHONPATH forced to target plane: {workspace / 'src'}")

    # --- baseline -----------------------------------------------------------
    target_head_baseline = _run_git(workspace, "rev-parse", "HEAD")
    target_status_baseline = subprocess.run(
        ["git", "status", "--short"], cwd=str(workspace), capture_output=True, text=True,
    ).stdout
    print(f"[self-dogfood] target plane baseline HEAD: {target_head_baseline}")
    if target_status_baseline.strip():
        print(f"[self-dogfood] target plane baseline is dirty, committing to establish a clean base:\n{target_status_baseline}")
        _run_git(workspace, "add", "-A")
        _run_git(workspace, "commit", "-m", "self-dogfood: establish clean baseline (disposable copy only)")
        target_head_baseline = _run_git(workspace, "rev-parse", "HEAD")

    print("[self-dogfood] running baseline test subset (targeted, not full suite, for speed)...")
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_internal_qa_engine.py::TestFailureClassification"],
        cwd=str(workspace), capture_output=True, text=True, timeout=120,
    )
    print(baseline.stdout[-2000:])
    if baseline.returncode != 0:
        print("[self-dogfood] FAIL: baseline is not green before the controlled defect is even introduced")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    # --- introduce the controlled defect (target plane ONLY) ---------------
    defect_path = workspace / DEFECT_FILE
    original_source = defect_path.read_text()
    if DEFECT_FIND not in original_source:
        print("[self-dogfood] FAIL: controlled-defect anchor text not found — script/source drifted")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1
    defect_path.write_text(original_source.replace(DEFECT_FIND, DEFECT_REPLACE))
    _run_git(workspace, "commit", "-am", "self-dogfood: introduce controlled defect (disposable copy only)")
    base_sha = _run_git(workspace, "rev-parse", "HEAD")
    print(f"[self-dogfood] controlled defect committed at {base_sha}")
    print(f"[self-dogfood] defect: {DEFECT_FILE} — classify_validation_status(TIMEOUT) now wrongly returns UNKNOWN "
          f"instead of ENVIRONMENT_FAILURE")

    # --- prove the negative control BEFORE any fix --------------------------
    neg_probe = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", DEFECT_TEST_ID], cwd=str(workspace), capture_output=True, text=True, timeout=60,
    )
    print(f"[self-dogfood] negative-control probe (defect present) exit={neg_probe.returncode} "
          f"({'RED as expected' if neg_probe.returncode != 0 else 'UNEXPECTED GREEN'})")
    if neg_probe.returncode == 0:
        print("[self-dogfood] FAIL: the controlled defect did not actually break the targeted test")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    # --- shared infra for both dev and QA phases -----------------------------
    exec_store = ExecutionStore(ctx_dir / "executions.sqlite3", clock=_utcnow)
    handoff_store = HandoffStore(ctx_dir / "handoffs.sqlite3", clock=_utcnow)
    rec_store = ExecutionRecommendationStore(ctx_dir / "recommendations.sqlite3", clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(ctx_dir / "decisions.sqlite3", clock=_utcnow)
    project_store = ProjectStateStore(ctx_dir / "project.sqlite3", clock=_utcnow)
    git_store = GitWorkItemStore(ctx_dir / "git_governance.sqlite3", clock=_utcnow)
    validation_store = ValidationStore(ctx_dir / "validation.sqlite3", clock=_utcnow)
    qa_run_store = QARunStore(ctx_dir / "qa_runs.sqlite3", clock=_utcnow)
    report_store = RealizationReportStore(ctx_dir / "reports.sqlite3", clock=_utcnow)

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=2)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(exec_store, clock=_utcnow)
    recommendation_service = ExecutionRecommendationService(rec_store, worker_selector, execution_engine, clock=_utcnow)
    adaptive_selector = AdaptiveExecutionSelector(decision_store, recommendation_service, worker_selector, clock=_utcnow)
    # git_store/GitGovernanceService is intentionally unused for actual
    # branch prep here — Part K/L forbid wiring InternalQAEngine into
    # GitGovernance this slice. It stays only as an empty, valid store so
    # RealizationReportService's qa/git sections render compatibly.

    project_store.create_project(project_id=PROJECT_ID, name="Self-dogfood", workspace=workspace)
    project_store.create_mvp(mvp_id=MVP_ID, project_id=PROJECT_ID, objective="Fix the controlled defect")
    project_store.create_work_item(
        work_item_id=WORK_ITEM_ID, mvp_id=MVP_ID,
        title="Restore the expected behavior broken by the controlled mutation, without weakening existing tests",
        required_capabilities=("development",),
        acceptance_criteria=(
            f"{DEFECT_TEST_ID} passes again",
            "no existing test is weakened or deleted to make it pass",
        ),
    )
    project_store.refresh_readiness(MVP_ID)
    project_store.mark_work_item_running(WORK_ITEM_ID)

    # --- real DEVELOPMENT phase ----------------------------------------------
    dev_estimation = ComplexityEstimationRequest(
        project_id=PROJECT_ID, role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        objective="Fix classify_validation_status so TIMEOUT maps to ENVIRONMENT_FAILURE again",
        acceptance_criteria=(f"{DEFECT_TEST_ID} passes", "no existing test is weakened"),
        mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
    )
    try:
        dev_selection = asyncio.run(adaptive_selector.select(
            estimation_request=dev_estimation, required_capabilities=frozenset({"development"}),
        ))
    except NoEligibleWorkerError as exc:
        _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
        return _blocked(f"no eligible development worker: {exc}", quota)

    print(f"[self-dogfood] real DEVELOPMENT worker: {dev_selection.worker.worker_id} "
          f"({dev_selection.worker.provider}/{dev_selection.worker.backend}, model={dev_selection.decision.model})")

    dev_request = ExecutionRequest(
        execution_id=dev_selection.decision.decision_id + "-exec", task_id=WORK_ITEM_ID, worker=dev_selection.worker,
        role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        instructions=(
            f"In {DEFECT_FILE}, the function classify_validation_status currently maps "
            "ValidationStatus.TIMEOUT to FailureClassification.UNKNOWN. This is a real regression: it "
            "should map to FailureClassification.ENVIRONMENT_FAILURE (a timeout IS concrete environment-level "
            "evidence). Fix ONLY this one mapping, in production code — do not modify any test file. "
            f"Run `{sys.executable} -m pytest -q {DEFECT_TEST_ID}` yourself and confirm it now PASSES. "
            "Commit your fix with git. "
            'Then emit exactly:\n\nralph emit "work.completed" "fixed classify_validation_status "'
            '"TIMEOUT mapping"\n\nThen output:\n\nLOOP_COMPLETE\n'
        ),
        initial_event_topic="work.start", success_topics=frozenset({"work.completed"}),
        failure_topics=frozenset({"work.failed"}), timeout_seconds=600.0,
        model=dev_selection.decision.model, reasoning_effort=dev_selection.decision.reasoning_effort,
    )
    dev_result = asyncio.run(execution_engine.execute(dev_request))
    dev_succeeded = dev_result.record.status.value == "succeeded"
    print(f"[self-dogfood] development execution status: {dev_result.record.status.value}")

    dev_head = _run_git(workspace, "rev-parse", "HEAD")
    handoff_store.create(
        handoff_id=f"{WORK_ITEM_ID}-handoff-dev", project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
        objective="Fix the controlled defect", execution_id=dev_request.execution_id,
        worker_id=dev_selection.worker.worker_id,
        completed_work="Fixed classify_validation_status TIMEOUT mapping." if dev_succeeded else "Development attempt did not succeed.",
        next_action="QA authoring/verification" if dev_succeeded else "investigate",
        git_sha_after=dev_head,
    )

    if not dev_succeeded:
        print("[self-dogfood] SELF_DOGFOOD = FAIL (development did not succeed)")
        _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
        return 1

    fixed_source = defect_path.read_text()
    production_diff_ok = "DEFECT_TEST_ID" not in fixed_source  # sanity: real file, not our script leaking in
    dev_test_diff = _run_git(workspace, "diff", "--name-only", base_sha, dev_head)
    test_files_touched_by_dev = [f for f in dev_test_diff.splitlines() if f.startswith("tests/")]
    if test_files_touched_by_dev:
        print(f"[self-dogfood] SELF_DOGFOOD = FAIL: developer modified test file(s): {test_files_touched_by_dev}")
        _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
        return 1
    print("[self-dogfood] developer touched no test files — OK")

    # --- real QA AUTHORING phase (must be a DISTINCT worker) ----------------
    qa_author = InternalQATestAuthor(
        adaptive_execution_selector=adaptive_selector, worker_selector=worker_selector,
        execution_engine=execution_engine, clock=_utcnow,
    )
    qa_estimation = ComplexityEstimationRequest(
        project_id=PROJECT_ID, role=QA_TESTING_CAPABILITY, workspace=workspace,
        objective="Check whether the fix to classify_validation_status is adequately covered by a regression test",
        acceptance_criteria=(f"{DEFECT_TEST_ID} exists and covers this exact mapping",),
        mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
    )
    try:
        qa_worker, qa_model, qa_reasoning_effort = asyncio.run(qa_author.select_worker(
            estimation_request=qa_estimation, developer_worker_id=dev_selection.worker.worker_id,
        ))
    except NoEligibleWorkerError as exc:
        print(f"[self-dogfood] SELF_DOGFOOD = BLOCKED_BY_PROVIDER: no distinct qa_testing worker available "
              f"(developer was {dev_selection.worker.worker_id!r}): {exc}")
        _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
        print(json.dumps({"self_dogfood_status": "BLOCKED_BY_PROVIDER", "developer_worker_id": dev_selection.worker.worker_id, "quota": quota}, indent=2))
        _restore_source_check(REPO_ROOT, source_head_before, source_status_before)
        if not args.keep_workspace:
            shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    assert qa_worker.worker_id != dev_selection.worker.worker_id, "author independence violated"
    print(
        f"[self-dogfood] real, DISTINCT QA worker: {qa_worker.worker_id} "
        f"({qa_worker.provider}/{qa_worker.backend}, model={qa_model}, reasoning_effort={qa_reasoning_effort})"
    )

    qa_outcome = asyncio.run(qa_author.run_authoring(
        worker=qa_worker, model=qa_model, reasoning_effort=qa_reasoning_effort,
        task_id=WORK_ITEM_ID, workspace=workspace,
        objective="Check regression coverage for the classify_validation_status TIMEOUT-mapping fix",
        acceptance_criteria=(f"{DEFECT_TEST_ID} exists and actually exercises this mapping",),
        base_sha=dev_head, head_sha=dev_head,
        changed_files=(DEFECT_FILE,), existing_coverage=(DEFECT_TEST_ID,),
    ))
    print(f"[self-dogfood] QA authoring succeeded={qa_outcome.succeeded} "
          f"tests_added={list(qa_outcome.report.tests_added) if qa_outcome.report else None} "
          f"unauthorized_files={list(qa_outcome.unauthorized_files)}")

    if qa_outcome.unauthorized_files:
        print(f"[self-dogfood] SELF_DOGFOOD = FAIL: QA worker touched unauthorized files: {qa_outcome.unauthorized_files}")
        _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
        return 1

    qa_authoring_head = _run_git(workspace, "rev-parse", "HEAD")

    # --- real QA FINAL_VERIFICATION phase (read-only) ------------------------
    validation_store.set_project_commands(
        PROJECT_ID,
        [ValidationCommand(validation_id="targeted-fix-test", kind=ValidationKind.UNIT_TEST, argv=(sys.executable, "-m", "pytest", "-q", DEFECT_TEST_ID))],
    )
    gate_runner = QualityGateRunner(validation_store, clock=_utcnow)
    qa_engine = InternalQAEngine(validation_store=validation_store, gate_runner=gate_runner, clock=_utcnow)
    final_request = QARequest(
        project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID, workspace=str(workspace),
        base_sha=base_sha, head_sha=qa_authoring_head, objective="Final QA verification of the defect fix",
        required_test_ids=(DEFECT_TEST_ID,),
    )
    qa_run = asyncio.run(run_qa_cycle(
        engine=qa_engine, run_store=qa_run_store, request=final_request, phase=QAPhase.FINAL_VERIFICATION,
        policy=QAPolicy(), clock=_utcnow,
    ))
    print(f"[self-dogfood] QA Final Verification verdict: {qa_run.verdict.status.value} — {qa_run.verdict.reason}")
    head_after_final = _run_git(workspace, "rev-parse", "HEAD")
    final_read_only_ok = head_after_final == qa_authoring_head

    # --- mandatory negative control: same verification against the *unfixed* defect ---
    neg_workspace = ctx_dir / "workspace_negative_control"
    shutil.copytree(workspace, neg_workspace, ignore=shutil.ignore_patterns(".venv"))
    _run_git(neg_workspace, "checkout", base_sha, "--", DEFECT_FILE)
    _run_git(neg_workspace, "commit", "-am", "self-dogfood: negative control (still-broken defect)")
    # Same isolation fix as above, re-pointed: this copy's own src/ must
    # be what pytest actually imports, never workspace's (already fixed).
    os.environ["PYTHONPATH"] = str(neg_workspace / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    neg_validation_store = ValidationStore(ctx_dir / "validation_negative.sqlite3", clock=_utcnow)
    neg_validation_store.set_project_commands(
        PROJECT_ID, [ValidationCommand(validation_id="targeted-fix-test", kind=ValidationKind.UNIT_TEST, argv=(sys.executable, "-m", "pytest", "-q", DEFECT_TEST_ID))],
    )
    neg_gate_runner = QualityGateRunner(neg_validation_store, clock=_utcnow)
    neg_engine = InternalQAEngine(validation_store=neg_validation_store, gate_runner=neg_gate_runner, clock=_utcnow)
    neg_qa_run_store = QARunStore(ctx_dir / "qa_runs_negative.sqlite3", clock=_utcnow)
    neg_head = _run_git(neg_workspace, "rev-parse", "HEAD")
    neg_request = QARequest(
        project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID + "-negative", workspace=str(neg_workspace),
        base_sha=base_sha, head_sha=neg_head, objective="Negative control: unfixed defect must never PASS",
        required_test_ids=(DEFECT_TEST_ID,),
    )
    neg_run = asyncio.run(run_qa_cycle(
        engine=neg_engine, run_store=neg_qa_run_store, request=neg_request, phase=QAPhase.FINAL_VERIFICATION,
        policy=QAPolicy(), clock=_utcnow,
    ))
    print(f"[self-dogfood] negative-control verdict (must NEVER be PASS): {neg_run.verdict.status.value}")
    negative_control_ok = neg_run.verdict.status is not QAVerdictStatus.PASS

    # --- RealizationReport ----------------------------------------------------
    report_service = RealizationReportService(
        project_store, exec_store, handoff_store, report_store,
        validation_store=validation_store, recommendation_store=rec_store, decision_store=decision_store,
        git_work_item_store=git_store, qa_run_store=qa_run_store, clock=_utcnow,
    )
    if qa_run.verdict.status is QAVerdictStatus.PASS:
        project_store.mark_work_item_completed(WORK_ITEM_ID)
    report = report_service.generate(project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID)
    docs_reports_dir = REPO_ROOT / "docs" / "reports"
    docs_reports_dir.mkdir(parents=True, exist_ok=True)
    historical_path = docs_reports_dir / "self-dogfood-dev-qa-2026-09-14.html"
    write_html(report, historical_path)
    print(f"[self-dogfood] RealizationReport written to: {historical_path}")

    success = (
        dev_succeeded and not test_files_touched_by_dev and not qa_outcome.unauthorized_files
        and final_read_only_ok and qa_run.verdict.status is QAVerdictStatus.PASS and negative_control_ok
    )
    status = "PASS" if success else "FAIL"
    print(f"[self-dogfood] SELF_DOGFOOD STATUS: {status}")

    _cleanup(project_store, git_store, exec_store, handoff_store, rec_store, decision_store, validation_store, qa_run_store, report_store)
    if not args.keep_workspace:
        shutil.rmtree(ctx_dir, ignore_errors=True)

    _restore_source_check(REPO_ROOT, source_head_before, source_status_before, allow_new_report=historical_path)
    return 0 if success else 1


def _cleanup(*stores) -> None:
    for s in stores:
        try:
            s.close()
        except Exception:
            pass


def _restore_source_check(repo: Path, head_before: str, status_before: str, *, allow_new_report: Path | None = None) -> None:
    head_after = _run_git(repo, "rev-parse", "HEAD")
    status_after = subprocess.run(["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True).stdout
    print(f"[self-dogfood] control plane HEAD after: {head_after} (unchanged: {head_after == head_before})")
    extra = set(status_after.splitlines()) - set(status_before.splitlines())
    if allow_new_report is not None:
        rel = str(allow_new_report.relative_to(repo))
        extra = {line for line in extra if rel not in line}
    print(f"[self-dogfood] control plane unexpected status changes: {extra or '(none)'}")


if __name__ == "__main__":
    raise SystemExit(main())
