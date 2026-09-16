#!/usr/bin/env python3
"""Self-dogfood FULL-PIPELINE acceptance (Slice 24): ai-dev-orchestrator
drives an entire governed WorkItem — Development -> QA Test Authoring ->
Quality Gate -> independent Review -> Final QA Verification -> Merge
Eligibility -> real merge — through the real ``MVPManager``, on a
DISPOSABLE COPY OF ITSELF.

Unlike ``scripts/self_dogfood_dev_qa_real.py`` (Slice 23, dev+QA only,
manually wired, no gates/review/merge) and
``scripts/smoke_cross_worker_real.py`` (Slice 20, adaptive selection +
quota only, no MVPManager, no git governance), this script is the first
real-provider acceptance to actually drive
``MVPManager.run_next_work_item(...)`` end to end, including a real
``git merge --ff-only`` — strictly inside the disposable copy.

CONTROL PLANE (this repo, ~/projects/ai-dev-orchestrator) is NEVER a git
target of any operation here — verified byte-identical before/after (HEAD
+ `git status --short`), except the one historical report file this
script is explicitly allowed to write.

TARGET PLANE is a full disposable copy of this repo under /tmp — never
the original, ever used as a workspace or merge target.

MVPManager builds every worker's instructions itself (from the WorkItem's
title/acceptance_criteria) — this script never hand-crafts a prompt; it
only wires real stores/selectors/engines and drives the pipeline.

NEVER run this via pytest. Run explicitly:

    python scripts/self_dogfood_full_pipeline_real.py

If both providers are not simultaneously available (this pipeline
structurally needs two independent real workers: author != QA, author !=
reviewer), this reports SELF_DOGFOOD_FULL_PIPELINE=BLOCKED_BY_PROVIDER and
does not claim acceptance — an accepted, non-failing outcome, not a bug.
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
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
)
from orchestrator.execution_store import ExecutionStore  # noqa: E402
from orchestrator.git_governance import (  # noqa: E402
    GitGovernancePolicy,
    GitGovernanceService,
    GitWorkItemStatus,
    GitWorkItemStore,
)
from orchestrator.handoff import HandoffStore  # noqa: E402
from orchestrator.internal_qa_engine import InternalQAEngine, run_qa_cycle  # noqa: E402
from orchestrator.mvp_manager import MVPManager  # noqa: E402
from orchestrator.project_state import ProjectStateStore, WorkItemStatus  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.qa import QAPhase, QAPolicy, QARequest, QARunStore, QAVerdictStatus  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import RalphExecutionEngine  # noqa: E402
from orchestrator.realization_report import RealizationReportService, RealizationReportStore, write_html  # noqa: E402
from orchestrator.review import ReviewPolicy, ReviewStore  # noqa: E402
from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore  # noqa: E402
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import WorkerSelector  # noqa: E402

WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"
PROJECT_ID = "proj-self-dogfood-full"
MVP_ID = "mvp-self-dogfood-full"
WORK_ITEM_ID = "wi-self-dogfood-full-pipeline"
MAX_CYCLES = 10

# --- the same controlled defect proven by Slice 23's script -------------
DEFECT_FILE = "src/orchestrator/internal_qa_engine.py"
DEFECT_FIND = 'def classify_validation_status(status: ValidationStatus) -> FailureClassification:\n    """Deterministic-first classification for one command\'s outcome.\n    Never claims REGRESSION/TEST_DEFECT/EXPECTED_CHANGE without more\n    context than a bare pytest exit code provides — those stay UNKNOWN,\n    a conservative and acceptable outcome per the task brief."""\n    if status is ValidationStatus.TIMEOUT:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    if status is ValidationStatus.ERROR:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    return FailureClassification.UNKNOWN'
DEFECT_REPLACE = 'def classify_validation_status(status: ValidationStatus) -> FailureClassification:\n    """Deterministic-first classification for one command\'s outcome.\n    Never claims REGRESSION/TEST_DEFECT/EXPECTED_CHANGE without more\n    context than a bare pytest exit code provides — those stay UNKNOWN,\n    a conservative and acceptable outcome per the task brief."""\n    if status is ValidationStatus.TIMEOUT:\n        return FailureClassification.UNKNOWN\n    if status is ValidationStatus.ERROR:\n        return FailureClassification.ENVIRONMENT_FAILURE\n    return FailureClassification.UNKNOWN'
DEFECT_TEST_ID = "tests/test_internal_qa_engine.py::TestFailureClassification::test_environment_failure_from_timeout"

TERMINAL_STATUSES = frozenset({WorkItemStatus.COMPLETED, WorkItemStatus.FAILED, WorkItemStatus.BLOCKED})


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
    print(f"[self-dogfood-full] BLOCKED_BY_PROVIDER: {reason}")
    print(json.dumps({"self_dogfood_full_pipeline_status": "BLOCKED_BY_PROVIDER", "reason": reason, "quota": quota}, indent=2))
    return 1


def _sanitize_html(html: str, *, workspace: Path) -> str:
    sanitized = html.replace(str(workspace), "<workspace>")
    sanitized = sanitized.replace(str(Path.home()), "~")
    return sanitized


def _cleanup(*stores) -> None:
    for s in stores:
        try:
            s.close()
        except Exception:
            pass


def _restore_source_check(repo: Path, head_before: str, status_before: str, *, allow_new_report: Path | None = None) -> None:
    head_after = _run_git(repo, "rev-parse", "HEAD")
    status_after = subprocess.run(["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True).stdout
    print(f"[self-dogfood-full] control plane HEAD after: {head_after} (unchanged: {head_after == head_before})")
    extra = set(status_after.splitlines()) - set(status_before.splitlines())
    if allow_new_report is not None:
        rel = str(allow_new_report.relative_to(repo))
        extra = {line for line in extra if rel not in line}
    print(f"[self-dogfood-full] control plane unexpected status changes: {extra or '(none)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-quota-only", action="store_true", help="Only probe provider availability and exit.")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    if args.check_quota_only:
        print(json.dumps(asyncio.run(_check_quota()), indent=2))
        return 0

    print(f"[self-dogfood-full] pid={os.getpid()}")
    source_head_before = _run_git(REPO_ROOT, "rev-parse", "HEAD")
    source_status_before = subprocess.run(
        ["git", "status", "--short"], cwd=str(REPO_ROOT), capture_output=True, text=True,
    ).stdout
    print(f"[self-dogfood-full] control plane HEAD before: {source_head_before}")
    print(f"[self-dogfood-full] control plane status before:\n{source_status_before or '(clean)'}")

    print("[self-dogfood-full] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))
    if not (quota.get("anthropic", {}).get("available") and quota.get("openai", {}).get("available")):
        return _blocked("this pipeline needs BOTH anthropic and openai available (author != QA, author != reviewer)", quota)

    ctx_dir = Path(tempfile.mkdtemp(prefix="ai-dev-orchestrator-self-dogfood-full-"))
    workspace = ctx_dir / "workspace"
    print(f"[self-dogfood-full] copying control plane -> disposable target plane: {workspace}")
    shutil.copytree(REPO_ROOT, workspace, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"))

    # Same critical isolation fix as Slice 23's script — see its comment.
    os.environ["PYTHONPATH"] = str(workspace / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    print(f"[self-dogfood-full] PYTHONPATH forced to target plane: {workspace / 'src'}")

    target_head_baseline = _run_git(workspace, "rev-parse", "HEAD")
    target_status_baseline = subprocess.run(
        ["git", "status", "--short"], cwd=str(workspace), capture_output=True, text=True,
    ).stdout
    print(f"[self-dogfood-full] target plane baseline HEAD: {target_head_baseline}")
    if target_status_baseline.strip():
        print(f"[self-dogfood-full] target plane baseline is dirty, committing to establish a clean base:\n{target_status_baseline}")
        _run_git(workspace, "add", "-A")
        _run_git(workspace, "commit", "-m", "self-dogfood-full: establish clean baseline (disposable copy only)")

    print("[self-dogfood-full] running baseline test subset (targeted, not full suite, for speed)...")
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_internal_qa_engine.py::TestFailureClassification"],
        cwd=str(workspace), capture_output=True, text=True, timeout=120,
    )
    print(baseline.stdout[-2000:])
    if baseline.returncode != 0:
        print("[self-dogfood-full] FAIL: baseline is not green before the controlled defect is even introduced")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    defect_path = workspace / DEFECT_FILE
    original_source = defect_path.read_text()
    if DEFECT_FIND not in original_source:
        print("[self-dogfood-full] FAIL: controlled-defect anchor text not found — script/source drifted")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1
    defect_path.write_text(original_source.replace(DEFECT_FIND, DEFECT_REPLACE))
    _run_git(workspace, "commit", "-am", "self-dogfood-full: introduce controlled defect (disposable copy only)")
    base_sha = _run_git(workspace, "rev-parse", "HEAD")
    print(f"[self-dogfood-full] controlled defect committed at {base_sha}")

    neg_probe = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", DEFECT_TEST_ID], cwd=str(workspace), capture_output=True, text=True, timeout=60,
    )
    print(f"[self-dogfood-full] negative-control pre-probe (defect present) exit={neg_probe.returncode} "
          f"({'RED as expected' if neg_probe.returncode != 0 else 'UNEXPECTED GREEN'})")
    if neg_probe.returncode == 0:
        print("[self-dogfood-full] FAIL: the controlled defect did not actually break the targeted test")
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    # --- real infra, one sqlite file per store -------------------------------
    project_store = ProjectStateStore(ctx_dir / "project.sqlite3", clock=_utcnow)
    handoff_store = HandoffStore(ctx_dir / "handoffs.sqlite3", clock=_utcnow)
    exec_store = ExecutionStore(ctx_dir / "executions.sqlite3", clock=_utcnow)
    rec_store = ExecutionRecommendationStore(ctx_dir / "recommendations.sqlite3", clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(ctx_dir / "decisions.sqlite3", clock=_utcnow)
    gate_validation_store = ValidationStore(ctx_dir / "validation_gate.sqlite3", clock=_utcnow)
    qa_validation_store = ValidationStore(ctx_dir / "validation_qa.sqlite3", clock=_utcnow)
    review_store = ReviewStore(ctx_dir / "review.sqlite3", clock=_utcnow)
    qa_run_store = QARunStore(ctx_dir / "qa_runs.sqlite3", clock=_utcnow)
    git_store = GitWorkItemStore(ctx_dir / "git_governance.sqlite3", clock=_utcnow)
    report_store = RealizationReportStore(ctx_dir / "reports.sqlite3", clock=_utcnow)

    targeted_command = ValidationCommand(
        validation_id="targeted-fix-test", kind=ValidationKind.UNIT_TEST,
        argv=(sys.executable, "-m", "pytest", "-q", DEFECT_TEST_ID),
    )
    gate_validation_store.set_project_commands(PROJECT_ID, [targeted_command])
    qa_validation_store.set_project_commands(PROJECT_ID, [targeted_command])

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=2)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(exec_store, clock=_utcnow)
    recommendation_service = ExecutionRecommendationService(rec_store, worker_selector, execution_engine, clock=_utcnow)
    adaptive_selector = AdaptiveExecutionSelector(decision_store, recommendation_service, worker_selector, clock=_utcnow)

    gate_runner = QualityGateRunner(gate_validation_store, clock=_utcnow)
    qa_gate_runner = QualityGateRunner(qa_validation_store, clock=_utcnow)
    qa_engine = InternalQAEngine(validation_store=qa_validation_store, gate_runner=qa_gate_runner, clock=_utcnow)

    git_service = GitGovernanceService(
        git_store, policy=GitGovernancePolicy(auto_merge=True, base_branch="main"), clock=_utcnow,
    )

    manager = MVPManager(
        project_store, handoff_store, worker_selector, execution_engine,
        quality_gate_runner=gate_runner, review_store=review_store, review_policy=ReviewPolicy(),
        execution_store=exec_store, adaptive_execution_selector=adaptive_selector,
        validation_store=gate_validation_store, git_governance_service=git_service,
        qa_engine=qa_engine, qa_policy=QAPolicy(), qa_run_store=qa_run_store,
        clock=_utcnow,
    )

    project_store.create_project(project_id=PROJECT_ID, name="Self-dogfood full pipeline", workspace=workspace)
    project_store.create_mvp(mvp_id=MVP_ID, project_id=PROJECT_ID, objective="Fix the controlled defect end to end")
    project_store.create_work_item(
        work_item_id=WORK_ITEM_ID, mvp_id=MVP_ID,
        title=(
            f"In {DEFECT_FILE}, classify_validation_status currently maps ValidationStatus.TIMEOUT to "
            "FailureClassification.UNKNOWN. This is a real regression: it should map to "
            "FailureClassification.ENVIRONMENT_FAILURE (a timeout IS concrete environment-level evidence). "
            "Fix ONLY this one mapping, in production code — do not modify any test file."
        ),
        required_capabilities=("development",),
        acceptance_criteria=(
            f"{DEFECT_TEST_ID} passes again",
            "no existing test is weakened or deleted to make it pass",
        ),
    )
    project_store.refresh_readiness(MVP_ID)

    result = None
    for cycle in range(1, MAX_CYCLES + 1):
        print(f"[self-dogfood-full] run_next_work_item cycle {cycle}...")
        result = asyncio.run(manager.run_next_work_item(MVP_ID))
        if result is None:
            print("[self-dogfood-full] run_next_work_item returned None (nothing eligible/due)")
            break
        status = result.work_item.status
        print(f"[self-dogfood-full] cycle {cycle} -> WorkItem status: {status.value}")
        if status in TERMINAL_STATUSES:
            break

    git_record = git_store.try_get(WORK_ITEM_ID)
    pipeline_completed = result is not None and result.work_item.status is WorkItemStatus.COMPLETED
    pipeline_merged = (
        git_record is not None and git_record.status is GitWorkItemStatus.MERGED
        and git_record.merged_sha == git_record.current_head_sha
    )
    print(f"[self-dogfood-full] final WorkItem status: {result.work_item.status.value if result else '(none)'}")
    print(f"[self-dogfood-full] git work item status: {git_record.status.value if git_record else '(none)'}")

    if not (pipeline_completed and pipeline_merged):
        # --- forensic diagnostics (never affects the verdict) ---------------
        stored_wi = project_store.get_work_item(WORK_ITEM_ID)
        print(f"[self-dogfood-full] DIAGNOSTIC blocked_reason: {stored_wi.blocked_reason}")
        diag_head = _run_git(workspace, "rev-parse", "HEAD")
        print(f"[self-dogfood-full] DIAGNOSTIC HEAD: {diag_head} (base_sha={base_sha})")
        print("[self-dogfood-full] DIAGNOSTIC git log base_sha..HEAD:")
        print(subprocess.run(["git", "log", "--stat", "--oneline", f"{base_sha}..HEAD"], cwd=str(workspace), capture_output=True, text=True).stdout)
        print("[self-dogfood-full] DIAGNOSTIC git status --porcelain (working tree, incl. untracked):")
        print(subprocess.run(["git", "status", "--porcelain=v1"], cwd=str(workspace), capture_output=True, text=True).stdout)
        print("[self-dogfood-full] DIAGNOSTIC full diff base_sha..working-tree:")
        diff_out = subprocess.run(["git", "diff", base_sha], cwd=str(workspace), capture_output=True, text=True).stdout
        print(diff_out[:8000])
        for suspect in ("README.md", "uv.lock"):
            content = subprocess.run(["git", "diff", base_sha, "--", suspect], cwd=str(workspace), capture_output=True, text=True).stdout
            if content.strip():
                print(f"[self-dogfood-full] DIAGNOSTIC diff for {suspect}:\n{content[:4000]}")
        ralph_dir = workspace / ".ralph"
        if ralph_dir.is_dir():
            print(f"[self-dogfood-full] DIAGNOSTIC .ralph/ contents: {sorted(p.name for p in ralph_dir.iterdir())}")
            events_ptr = ralph_dir / "current-events"
            if events_ptr.is_file():
                events_path = workspace / events_ptr.read_text().strip()
                if events_path.is_file():
                    print(f"[self-dogfood-full] DIAGNOSTIC ralph events ({events_path}):")
                    print(events_path.read_text()[-6000:])

    # --- mandatory negative control: Final Verification against the still-broken defect ---
    # Cheap and provider-free: FINAL_VERIFICATION never invokes a worker,
    # only real pytest — so this reuses InternalQAEngine directly (as
    # Slice 23's script did) instead of spending 3 more real sessions on a
    # doomed-by-design pipeline attempt.
    neg_workspace = ctx_dir / "workspace_negative_control"
    shutil.copytree(workspace, neg_workspace, ignore=shutil.ignore_patterns(".venv"))
    _run_git(neg_workspace, "checkout", base_sha, "--", DEFECT_FILE)
    _run_git(neg_workspace, "commit", "-am", "self-dogfood-full: negative control (still-broken defect)")
    os.environ["PYTHONPATH"] = str(neg_workspace / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    neg_validation_store = ValidationStore(ctx_dir / "validation_negative.sqlite3", clock=_utcnow)
    neg_validation_store.set_project_commands(PROJECT_ID, [targeted_command])
    neg_gate_runner = QualityGateRunner(neg_validation_store, clock=_utcnow)
    neg_engine = InternalQAEngine(validation_store=neg_validation_store, gate_runner=neg_gate_runner, clock=_utcnow)
    neg_qa_run_store = QARunStore(ctx_dir / "qa_runs_negative.sqlite3", clock=_utcnow)
    neg_head = _run_git(neg_workspace, "rev-parse", "HEAD")
    neg_request = QARequest(
        project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID + "-negative", workspace=str(neg_workspace),
        base_sha=base_sha, head_sha=neg_head, objective="Negative control: unfixed defect must never PASS",
    )
    neg_run = asyncio.run(run_qa_cycle(
        engine=neg_engine, run_store=neg_qa_run_store, request=neg_request, phase=QAPhase.FINAL_VERIFICATION,
        policy=QAPolicy(), clock=_utcnow,
    ))
    print(f"[self-dogfood-full] negative-control verdict (must NEVER be PASS): {neg_run.verdict.status.value}")
    negative_control_ok = neg_run.verdict.status is not QAVerdictStatus.PASS

    # --- RealizationReport ----------------------------------------------------
    report_service = RealizationReportService(
        project_store, exec_store, handoff_store, report_store,
        validation_store=gate_validation_store, recommendation_store=rec_store, decision_store=decision_store,
        git_work_item_store=git_store, qa_run_store=qa_run_store, clock=_utcnow,
    )
    report = report_service.generate(project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID)
    docs_reports_dir = REPO_ROOT / "docs" / "reports"
    docs_reports_dir.mkdir(parents=True, exist_ok=True)
    tmp_report_path = ctx_dir / "report.html"
    write_html(report, tmp_report_path)
    historical_path = docs_reports_dir / "self-dogfood-full-pipeline-2026-09-15.html"
    historical_path.write_text(_sanitize_html(tmp_report_path.read_text(encoding="utf-8"), workspace=workspace), encoding="utf-8")
    print(f"[self-dogfood-full] RealizationReport written to: {historical_path}")

    success = pipeline_completed and pipeline_merged and negative_control_ok
    status = "PASS" if success else "FAIL"
    print(f"[self-dogfood-full] SELF_DOGFOOD_FULL_PIPELINE STATUS: {status}")
    print(json.dumps({
        "self_dogfood_full_pipeline_status": status,
        "work_item_status": result.work_item.status.value if result else None,
        "git_work_item_status": git_record.status.value if git_record else None,
        "negative_control_ok": negative_control_ok,
    }, indent=2))

    _cleanup(
        project_store, handoff_store, exec_store, rec_store, decision_store, gate_validation_store,
        qa_validation_store, review_store, qa_run_store, git_store, report_store, neg_validation_store,
        neg_qa_run_store,
    )
    if not args.keep_workspace:
        shutil.rmtree(ctx_dir, ignore_errors=True)

    _restore_source_check(REPO_ROOT, source_head_before, source_status_before, allow_new_report=historical_path)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
