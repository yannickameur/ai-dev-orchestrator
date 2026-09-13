#!/usr/bin/env python3
"""Manual, real cross-worker cold-resume smoke test (Slice 18 acceptance).

Runs the SAME scenario already validated offline
(``tests/integration/test_cross_worker_resume_e2e.py``), but for real:
two real, distinct workers from ``config/workers.yaml`` (one
``claude_code`` backend, one ``codex`` backend), a real
``ExecutionRecommendationService`` pre-flight (a real estimator execution
via Ralph), a real ``AdaptiveExecutionSelector``, and a real
``RalphExecutionEngine`` launching the real ``ralph`` binary — never a
fake subprocess runner.

NEVER run this via pytest — it is deliberately excluded from the normal
offline suite (real cost, real time, real provider quota). Run it
explicitly:

    python scripts/smoke_cross_worker_real.py

Architecture (per the task brief): a controller process (this script,
invoked with no phase flag) spawns Phase A as a genuinely separate OS
process (``python scripts/smoke_cross_worker_real.py --phase a ...``),
waits for it to exit, then spawns Phase B the same way. No Python object
from Phase A is ever passed to Phase B — only the workspace, the sqlite
files, and small JSON fact files in the context directory survive.

~/projects/ralph-spike is NEVER modified: only a disposable copy under
``/tmp`` is ever touched, torn down (or kept, see ``--keep-workspace``)
after the run.
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

from orchestrator.adaptive_execution import (  # noqa: E402
    AdaptiveExecutionDecisionStore,
    AdaptiveExecutionSelector,
)
from orchestrator.complexity_estimation import (  # noqa: E402
    ComplexityEstimationRequest,
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
)
from orchestrator.execution_store import ExecutionStore  # noqa: E402
from orchestrator.handoff import HandoffStore  # noqa: E402
from orchestrator.mvp_manager import DEFAULT_WORK_ITEM_ROLE  # noqa: E402
from orchestrator.project_state import ProjectStateStore  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import ExecutionRequest, RalphExecutionEngine  # noqa: E402
from orchestrator.realization_report import RealizationReportService, RealizationReportStore, write_html  # noqa: E402
from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore  # noqa: E402
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import WorkerSelector  # noqa: E402

RALPH_SPIKE_SOURCE = Path.home() / "projects" / "ralph-spike"
WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"

PROJECT_ID = "proj-smoke"
MVP_ID = "mvp-smoke"
WORK_ITEM_ID = "wi-smoke-fix-add"


def _db_paths(ctx_dir: Path) -> dict:
    return {
        "project_db": ctx_dir / "project.sqlite3",
        "execution_db": ctx_dir / "execution.sqlite3",
        "handoff_db": ctx_dir / "handoff.sqlite3",
        "recommendation_db": ctx_dir / "recommendation.sqlite3",
        "decision_db": ctx_dir / "decision.sqlite3",
        "validation_db": ctx_dir / "validation.sqlite3",
        "report_db": ctx_dir / "reports.sqlite3",
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _find_workers() -> tuple[str, str]:
    registry = WorkerRegistry.load(WORKERS_CONFIG)
    claude_worker_id = None
    codex_worker_id = None
    for worker in registry.enabled_workers():
        if worker.backend == "claude_code" and claude_worker_id is None:
            claude_worker_id = worker.worker_id
        if worker.backend == "codex" and codex_worker_id is None:
            codex_worker_id = worker.worker_id
    if claude_worker_id is None or codex_worker_id is None:
        raise SystemExit(
            f"config/workers.yaml must declare at least one enabled claude_code and one enabled "
            f"codex worker (found claude={claude_worker_id!r}, codex={codex_worker_id!r})"
        )
    return claude_worker_id, codex_worker_id


async def _check_quota() -> dict:
    """Read-only availability check — no reset credit consumed, no usage
    dashboard, no quota workaround. ``ClaudeCodeAdapter.probe()`` performs
    one minimal headless call by design (the sanctioned way this project
    determines Claude Code availability, see docs/SPIKE_RALPH.md);
    ``CodexAdapter.probe()`` is a pure read-only JSON-RPC call."""
    manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()},
        QuotaPolicy(state_ttl=timedelta(seconds=1)),
        clock=_utcnow,
    )
    result = {}
    for provider in ("anthropic", "openai"):
        try:
            state = await manager.get(provider)
            result[provider] = {"available": state.availability.available, "reason": (
                state.availability.reason.value if state.availability.reason else None
            )}
        except ProviderProbeError as exc:
            result[provider] = {"available": False, "reason": f"probe_error: {exc}"}
    return result


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def _phase_a(ctx_dir: Path) -> None:
    """Real Phase A: TDD red only. Writes facts.json, then exits — this OS
    process terminates for real once this function returns."""
    workspace = ctx_dir / "workspace"
    db = _db_paths(ctx_dir)

    project_store = ProjectStateStore(db["project_db"], clock=_utcnow)
    execution_store = ExecutionStore(db["execution_db"], clock=_utcnow)
    handoff_store = HandoffStore(db["handoff_db"], clock=_utcnow)
    recommendation_store = ExecutionRecommendationStore(db["recommendation_db"], clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(db["decision_db"], clock=_utcnow)

    project_store.create_project(project_id=PROJECT_ID, name="Ralph Spike Real Smoke", workspace=workspace)
    project_store.create_mvp(mvp_id=MVP_ID, project_id=PROJECT_ID, objective="Fix add() in review_candidate.py")
    project_store.create_work_item(
        work_item_id=WORK_ITEM_ID, mvp_id=MVP_ID,
        title="Fix add() to actually add, using TDD, with a real regression test",
        required_capabilities=("development",),
        acceptance_criteria=("add(2, 3) returns 5", "test_review_candidate.py passes"),
    )
    project_store.refresh_readiness(MVP_ID)
    project_store.mark_work_item_running(WORK_ITEM_ID)

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=1)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(execution_store, clock=_utcnow)  # real subprocess runner (default)
    recommendation_service = ExecutionRecommendationService(recommendation_store, worker_selector, execution_engine, clock=_utcnow)
    adaptive_selector = AdaptiveExecutionSelector(decision_store, recommendation_service, worker_selector, clock=_utcnow)

    estimation_request = ComplexityEstimationRequest(
        project_id=PROJECT_ID, role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        objective="Fix add() to actually add, using TDD, with a real regression test",
        acceptance_criteria=("add(2, 3) returns 5", "test_review_candidate.py passes"),
        mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
    )
    selection = asyncio.run(adaptive_selector.select(
        estimation_request=estimation_request, required_capabilities=frozenset({"development"}),
    ))

    dev_request = ExecutionRequest(
        execution_id=selection.decision.decision_id + "-exec", task_id=WORK_ITEM_ID, worker=selection.worker,
        role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        instructions=(
            "You are working in a small git repository. Inspect review_candidate.py. "
            "Following TDD (RED phase only): create a NEW file named test_review_candidate.py "
            "that imports add from review_candidate and asserts add(2, 3) == 5, runnable as a "
            "plain script with `python test_review_candidate.py` (it should print OK and exit 0 "
            "once add() is fixed — but do NOT fix add() yourself in this phase). "
            "Run `python test_review_candidate.py` yourself and confirm it currently FAILS "
            "(RED) because add() is still buggy. Do not modify review_candidate.py. "
            "Commit your new test file with git (git add test_review_candidate.py && git commit "
            "-m 'Add regression test for add()'). "
            f'Then emit exactly:\n\nralph emit "work.completed" "RED confirmed: test added, add() '
            f'not yet fixed"\n\nThen output:\n\nLOOP_COMPLETE\n'
        ),
        initial_event_topic="work.start", success_topics=frozenset({"work.completed"}),
        failure_topics=frozenset({"work.failed"}), timeout_seconds=600.0,
        model=selection.decision.model, reasoning_effort=selection.decision.reasoning_effort,
    )
    result = asyncio.run(execution_engine.execute(dev_request))

    # WorkItem is deliberately left RUNNING here: this is a controlled,
    # explicit handoff between two phases of the same task, never a crash
    # (that path is already validated by the offline E2E test). Phase B
    # is the one that drives the WorkItem to its real terminal status.
    handoff = handoff_store.create(
        handoff_id=f"{WORK_ITEM_ID}-handoff-a", project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
        objective="Fix add() to actually add", execution_id=dev_request.execution_id, worker_id=selection.worker.worker_id,
        completed_work="Added test_review_candidate.py asserting add(2, 3) == 5 (TDD RED).",
        next_action="Fix add() in review_candidate.py to make the regression test pass (TDD GREEN).",
        git_sha_after=_run_git(workspace, "rev-parse", "HEAD"),
    )

    facts = {
        "worker_id": selection.worker.worker_id, "provider": selection.worker.provider,
        "backend": selection.worker.backend, "model": selection.decision.model,
        "reasoning_effort": selection.decision.reasoning_effort, "profile_id": selection.decision.profile_id,
        "quality_tier": selection.decision.quality_tier.name, "decision_id": selection.decision.decision_id,
        "recommendation_id": selection.decision.recommendation_id, "execution_id": dev_request.execution_id,
        "execution_status": result.record.status.value, "exit_code": result.exit_code,
        "handoff_id": handoff.handoff_id, "pid": os.getpid(), "git_sha_after": handoff.git_sha_after,
    }
    _write_json(ctx_dir / "phase_a_facts.json", facts)

    project_store.close()
    execution_store.close()
    handoff_store.close()
    recommendation_store.close()
    decision_store.close()


def _phase_b(ctx_dir: Path, exclude_worker_id: str) -> None:
    workspace = ctx_dir / "workspace"
    db = _db_paths(ctx_dir)

    project_store = ProjectStateStore(db["project_db"], clock=_utcnow)
    execution_store = ExecutionStore(db["execution_db"], clock=_utcnow)
    handoff_store = HandoffStore(db["handoff_db"], clock=_utcnow)
    recommendation_store = ExecutionRecommendationStore(db["recommendation_db"], clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(db["decision_db"], clock=_utcnow)
    validation_store = ValidationStore(db["validation_db"], clock=_utcnow)
    validation_store.set_project_commands(
        PROJECT_ID,
        [ValidationCommand(validation_id="mini-project-tests", kind=ValidationKind.UNIT_TEST, argv=(sys.executable, "test_review_candidate.py"))],
    )
    gate_runner = QualityGateRunner(validation_store, clock=_utcnow)

    latest_handoff = handoff_store.latest_for_work_item(WORK_ITEM_ID)

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=1)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(execution_store, clock=_utcnow)
    recommendation_service = ExecutionRecommendationService(recommendation_store, worker_selector, execution_engine, clock=_utcnow)
    adaptive_selector = AdaptiveExecutionSelector(decision_store, recommendation_service, worker_selector, clock=_utcnow)

    estimation_request = ComplexityEstimationRequest(
        project_id=PROJECT_ID, role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        objective="Fix add() to actually add, using TDD, with a real regression test",
        acceptance_criteria=("add(2, 3) returns 5", "test_review_candidate.py passes"),
        mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID, latest_handoff=latest_handoff, git_sha=latest_handoff.git_sha_after if latest_handoff else None,
    )
    selection = asyncio.run(adaptive_selector.select(
        estimation_request=estimation_request, required_capabilities=frozenset({"development"}),
        excluded_worker_ids=frozenset({exclude_worker_id}),
    ))

    resume_context = (
        "This work item was previously attempted by a different worker (now excluded). "
        f"Last handoff — completed_work: {latest_handoff.completed_work if latest_handoff else '(none)'}; "
        f"next_action: {latest_handoff.next_action if latest_handoff else '(none)'}; "
        f"git SHA after that attempt: {latest_handoff.git_sha_after if latest_handoff else '(none)'}."
    )
    dev_request = ExecutionRequest(
        execution_id=selection.decision.decision_id + "-exec", task_id=WORK_ITEM_ID, worker=selection.worker,
        role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        instructions=(
            f"{resume_context}\n\n"
            "Following TDD (GREEN phase): fix add(a, b) in review_candidate.py so that it "
            "actually returns a + b. Do not modify test_review_candidate.py. Run "
            "`python test_review_candidate.py` yourself and confirm it now PASSES (prints OK, "
            "exit code 0). Commit your fix with git (git add review_candidate.py && git commit "
            "-m 'Fix add() to actually add'). "
            'Then emit exactly:\n\nralph emit "work.completed" "GREEN confirmed: add() fixed, '
            'tests pass"\n\nThen output:\n\nLOOP_COMPLETE\n'
        ),
        initial_event_topic="work.start", success_topics=frozenset({"work.completed"}),
        failure_topics=frozenset({"work.failed"}), timeout_seconds=600.0,
        model=selection.decision.model, reasoning_effort=selection.decision.reasoning_effort,
    )
    result = asyncio.run(execution_engine.execute(dev_request))

    dev_succeeded = result.record.status.value == "succeeded"
    gate_result = asyncio.run(gate_runner.run_gate(
        project_id=PROJECT_ID, cwd=workspace, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
    )) if dev_succeeded else None

    final_handoff = handoff_store.create(
        handoff_id=f"{WORK_ITEM_ID}-handoff-b", project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
        objective="Fix add() to actually add", execution_id=dev_request.execution_id, worker_id=selection.worker.worker_id,
        completed_work="Fixed add() to return a + b; regression test passes." if dev_succeeded else "Development attempt did not succeed.",
        test_results=f"quality_gate={'PASSED' if gate_result and gate_result.passed else 'not run/failed'}",
        next_action="none — work item complete" if (dev_succeeded and gate_result and gate_result.passed) else "investigate failure",
        git_sha_after=_run_git(workspace, "rev-parse", "HEAD"),
    )

    if dev_succeeded and gate_result is not None and gate_result.passed:
        project_store.mark_work_item_completed(WORK_ITEM_ID)
    else:
        project_store.mark_work_item_failed(WORK_ITEM_ID)

    facts = {
        "worker_id": selection.worker.worker_id, "provider": selection.worker.provider,
        "backend": selection.worker.backend, "model": selection.decision.model,
        "reasoning_effort": selection.decision.reasoning_effort, "profile_id": selection.decision.profile_id,
        "quality_tier": selection.decision.quality_tier.name, "decision_id": selection.decision.decision_id,
        "recommendation_id": selection.decision.recommendation_id, "execution_id": dev_request.execution_id,
        "execution_status": result.record.status.value, "exit_code": result.exit_code,
        "handoff_id": final_handoff.handoff_id, "pid": os.getpid(),
        "git_sha_final": final_handoff.git_sha_after,
        "gate_passed": bool(gate_result and gate_result.passed),
        "work_item_status": project_store.get_work_item(WORK_ITEM_ID).status.value,
    }
    _write_json(ctx_dir / "phase_b_facts.json", facts)

    project_store.close()
    execution_store.close()
    handoff_store.close()
    recommendation_store.close()
    decision_store.close()
    validation_store.close()


def _generate_report(ctx_dir: Path) -> Path:
    workspace = ctx_dir / "workspace"
    db = _db_paths(ctx_dir)
    project_store = ProjectStateStore(db["project_db"], clock=_utcnow)
    execution_store = ExecutionStore(db["execution_db"], clock=_utcnow)
    handoff_store = HandoffStore(db["handoff_db"], clock=_utcnow)
    recommendation_store = ExecutionRecommendationStore(db["recommendation_db"], clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(db["decision_db"], clock=_utcnow)
    validation_store = ValidationStore(db["validation_db"], clock=_utcnow)
    report_store = RealizationReportStore(db["report_db"], clock=_utcnow)

    service = RealizationReportService(
        project_store, execution_store, handoff_store, report_store,
        validation_store=validation_store, recommendation_store=recommendation_store,
        decision_store=decision_store, clock=_utcnow,
    )
    report = service.generate(project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID)
    output_path = workspace / "reports" / "realizations" / WORK_ITEM_ID / f"{report.report_id}.html"
    write_html(report, output_path)

    for store in (project_store, execution_store, handoff_store, recommendation_store, decision_store, validation_store, report_store):
        store.close()
    return output_path


def _sanitize_html(html: str, *, workspace: Path) -> str:
    """Neutralizes any absolute temp-workspace path before this HTML is
    versioned as historical proof — never a secret scrub (this report
    never contained any secret: no API key/token/credential/env var is
    ever aggregated by RealizationReportService in the first place), just
    removing an uninteresting personal filesystem path."""
    sanitized = html.replace(str(workspace), "<workspace>")
    sanitized = sanitized.replace(str(Path.home()), "~")
    return sanitized


def cmd_check_quota() -> None:
    result = asyncio.run(_check_quota())
    print(json.dumps(result, indent=2))


def cmd_phase(args: argparse.Namespace) -> None:
    ctx_dir = Path(args.context_dir)
    if args.phase == "a":
        _phase_a(ctx_dir)
    else:
        _phase_b(ctx_dir, args.exclude_worker_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["a", "b"], help=argparse.SUPPRESS)
    parser.add_argument("--context-dir", help=argparse.SUPPRESS)
    parser.add_argument("--exclude-worker-id", help=argparse.SUPPRESS)
    parser.add_argument("--check-quota-only", action="store_true", help="Only probe provider availability and exit.")
    parser.add_argument("--keep-workspace", action="store_true", help="Do not delete the disposable copy afterward.")
    args = parser.parse_args()

    if args.phase is not None:
        cmd_phase(args)
        return 0

    if args.check_quota_only:
        cmd_check_quota()
        return 0

    print(f"[controller] pid={os.getpid()}")
    before = subprocess.run(
        ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
    ).stdout
    print(f"[controller] ~/projects/ralph-spike status BEFORE:\n{before or '(clean)'}")

    print("[controller] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))
    claude_available = quota.get("anthropic", {}).get("available") is True
    codex_available = quota.get("openai", {}).get("available") is True
    if not (claude_available and codex_available):
        print("[controller] BLOCKED: at least one provider is not available right now — "
              "never consuming a reset credit to force this smoke through.")
        return _emit_blocked_report(quota)

    claude_worker_id, codex_worker_id = _find_workers()
    print(f"[controller] real workers: claude_code={claude_worker_id!r} codex={codex_worker_id!r}")

    ctx_dir = Path(tempfile.mkdtemp(prefix="ai-dev-orchestrator-smoke-"))
    workspace = ctx_dir / "workspace"
    shutil.copytree(RALPH_SPIKE_SOURCE, workspace)
    print(f"[controller] disposable workspace: {workspace}")

    print("[controller] launching Phase A as a separate OS process...")
    proc_a = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--phase", "a", "--context-dir", str(ctx_dir)],
        cwd=str(REPO_ROOT),
    )
    if proc_a.returncode != 0:
        print(f"[controller] Phase A process failed (exit {proc_a.returncode})")
        return 1
    facts_a = _read_json(ctx_dir / "phase_a_facts.json")
    print(f"[controller] Phase A done, pid={facts_a['pid']}, worker={facts_a['worker_id']}")

    print("[controller] launching Phase B as a separate OS process...")
    proc_b = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--phase", "b", "--context-dir", str(ctx_dir),
         "--exclude-worker-id", facts_a["worker_id"]],
        cwd=str(REPO_ROOT),
    )
    if proc_b.returncode != 0:
        print(f"[controller] Phase B process failed (exit {proc_b.returncode})")
        return 1
    facts_b = _read_json(ctx_dir / "phase_b_facts.json")
    print(f"[controller] Phase B done, pid={facts_b['pid']}, worker={facts_b['worker_id']}")

    assert facts_a["pid"] != facts_b["pid"], "Phase A and Phase B must be genuinely separate OS processes"
    assert facts_a["worker_id"] != facts_b["worker_id"], "cross-worker smoke requires two distinct workers"

    print("[controller] generating RealizationReport via the real feature...")
    report_path = _generate_report(ctx_dir)
    print(f"[controller] report written to: {report_path}")

    docs_reports_dir = REPO_ROOT / "docs" / "reports"
    docs_reports_dir.mkdir(parents=True, exist_ok=True)
    historical_path = docs_reports_dir / "real-cross-worker-resume-2026-09-13.html"
    sanitized = _sanitize_html(report_path.read_text(encoding="utf-8"), workspace=workspace)
    historical_path.write_text(sanitized, encoding="utf-8")
    print(f"[controller] sanitized historical copy: {historical_path}")

    after = subprocess.run(
        ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
    ).stdout
    print(f"[controller] ~/projects/ralph-spike status AFTER:\n{after or '(clean)'}")
    print(f"[controller] ralph-spike unchanged: {before == after}")

    print(f"[controller] final work_item_status={facts_b['work_item_status']} gate_passed={facts_b['gate_passed']}")
    smoke_status = "PASS" if (facts_b["work_item_status"] == "completed" and facts_b["gate_passed"] and before == after) else "FAIL"
    print(f"[controller] SMOKE STATUS: {smoke_status}")

    audit = {
        "workspace": str(workspace), "work_item_id": WORK_ITEM_ID, "phase_a": facts_a, "phase_b": facts_b,
        "ralph_spike_unchanged": before == after, "smoke_status": smoke_status,
        "report_path": str(report_path), "historical_report_path": str(historical_path),
    }
    _write_json(ctx_dir / "smoke_audit.json", audit)
    print(json.dumps(audit, indent=2, default=str))

    if not args.keep_workspace:
        shutil.rmtree(ctx_dir, ignore_errors=True)
        print(f"[controller] disposable workspace removed: {ctx_dir}")
    else:
        print(f"[controller] disposable workspace kept: {ctx_dir}")

    return 0 if smoke_status == "PASS" else 1


def _emit_blocked_report(quota: dict) -> int:
    ctx_dir = Path(tempfile.mkdtemp(prefix="ai-dev-orchestrator-smoke-blocked-"))
    workspace = ctx_dir / "workspace"
    shutil.copytree(RALPH_SPIKE_SOURCE, workspace)
    db = _db_paths(ctx_dir)
    project_store = ProjectStateStore(db["project_db"], clock=_utcnow)
    execution_store = ExecutionStore(db["execution_db"], clock=_utcnow)
    handoff_store = HandoffStore(db["handoff_db"], clock=_utcnow)
    report_store = RealizationReportStore(db["report_db"], clock=_utcnow)

    project_store.create_project(project_id=PROJECT_ID, name="Ralph Spike Real Smoke (blocked)", workspace=workspace)
    project_store.create_mvp(mvp_id=MVP_ID, project_id=PROJECT_ID, objective="Fix add() in review_candidate.py")
    project_store.create_work_item(
        work_item_id=WORK_ITEM_ID, mvp_id=MVP_ID, title="Fix add() (blocked before any real worker ran)",
        acceptance_criteria=("add(2, 3) returns 5",),
    )
    project_store.mark_work_item_blocked(
        WORK_ITEM_ID,
        reason=f"real cross-worker smoke blocked before any worker ran — provider availability: {json.dumps(quota)}",
    )
    service = RealizationReportService(project_store, execution_store, handoff_store, report_store, clock=_utcnow)
    report = service.generate(project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID)
    output_path = workspace / "reports" / "realizations" / WORK_ITEM_ID / f"{report.report_id}.html"
    write_html(report, output_path)
    print(f"[controller] BLOCKED report written to: {output_path}")

    for store in (project_store, execution_store, handoff_store, report_store):
        store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
