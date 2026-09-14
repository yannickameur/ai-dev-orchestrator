#!/usr/bin/env python3
"""Manual, real InternalQAEngine QA-Test-Authoring smoke test (Slice 23).

Runs a real ``qa_testing``-capable worker (chosen via the real, adaptive
mechanism — never hardcoded) against a disposable copy of
``~/projects/ralph-spike``, whose ``review_candidate.py::add()`` already
contains a known bug (returns ``a - b``). The QA Test Author's job is to
analyze this, add a regression test that proves the bug (RED), and
**never touch production code itself** — fixing it is a future Slice 24
coding-agent's job, not this one's.

NEVER run this via pytest — real cost, real provider quota (read-only
probed first, never a reset credit consumed). Run explicitly:

    python scripts/smoke_internal_qa_real.py

~/projects/ralph-spike is NEVER modified: only a disposable copy under
``/tmp`` is ever touched, removed afterward (unless ``--keep-workspace``).
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

import yaml

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
from orchestrator.internal_qa_engine import QA_TESTING_CAPABILITY, InternalQATestAuthor  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import RalphExecutionEngine  # noqa: E402
from orchestrator.qa import QARunStatus  # noqa: E402 (status only, no QAEngine.run() in this smoke)
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import WorkerSelector  # noqa: E402

RALPH_SPIKE_SOURCE = Path.home() / "projects" / "ralph-spike"
WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"
WORK_ITEM_ID = "wi-smoke-qa-authoring"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-quota-only", action="store_true")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    print(f"[smoke] pid={os.getpid()}")
    before = subprocess.run(
        ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
    ).stdout
    print(f"[smoke] ~/projects/ralph-spike status BEFORE:\n{before or '(clean)'}")

    print("[smoke] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))
    if args.check_quota_only:
        return 0

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    qa_capable = [
        w for w in registry.enabled_workers()
        if QA_TESTING_CAPABILITY in w.capabilities and quota.get(w.provider, {}).get("available") is True
    ]
    if not qa_capable:
        print("[smoke] BLOCKED_BY_PROVIDER: no qa_testing-capable worker has an available provider "
              "right now — never consuming a reset credit to force this through.")
        _print_result("BLOCKED_BY_PROVIDER", quota=quota)
        return 1

    ctx_dir = Path(tempfile.mkdtemp(prefix="ai-dev-orchestrator-qa-smoke-"))
    workspace = ctx_dir / "workspace"
    shutil.copytree(RALPH_SPIKE_SOURCE, workspace)
    # Marker file so InternalQAEngine.stack_supported() (and any future
    # real ValidationCommand execution) recognizes this as a Python/pytest
    # project — never written to the real ~/projects/ralph-spike.
    (workspace / "pyproject.toml").write_text('[project]\nname = "ralph-spike-smoke"\nversion = "0.0.0"\n')
    # A governed base_sha must start from a genuinely clean working tree
    # (exactly what GitGovernanceService.prepare_work_item's
    # require_clean_worktree already enforces, Slice 20) — this manual
    # smoke bypasses that layer (Part K: no MVPManager/GitGovernance
    # integration in Slice 23) but must still establish that same
    # invariant itself: `git add -A` here commits ralph-spike's own
    # pre-existing untracked review_candidate.py into the baseline too,
    # so only what the QA worker itself does afterward can ever be
    # flagged as a change.
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", "smoke: establish clean baseline (disposable copy only)")
    print(f"[smoke] disposable workspace: {workspace}")

    base_sha = _run_git(workspace, "rev-parse", "HEAD")

    exec_store = ExecutionStore(ctx_dir / "executions.sqlite3", clock=_utcnow)
    rec_store = ExecutionRecommendationStore(ctx_dir / "recommendations.sqlite3", clock=_utcnow)
    decision_store = AdaptiveExecutionDecisionStore(ctx_dir / "decisions.sqlite3", clock=_utcnow)

    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=1)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(exec_store, clock=_utcnow)  # real subprocess runner (default)
    recommendation_service = ExecutionRecommendationService(rec_store, worker_selector, execution_engine, clock=_utcnow)
    adaptive_selector = AdaptiveExecutionSelector(decision_store, recommendation_service, worker_selector, clock=_utcnow)
    author = InternalQATestAuthor(
        adaptive_execution_selector=adaptive_selector, worker_selector=worker_selector,
        execution_engine=execution_engine, clock=_utcnow,
    )

    estimation_request = ComplexityEstimationRequest(
        project_id="proj-qa-smoke", role=QA_TESTING_CAPABILITY, workspace=workspace,
        objective="Analyze add() and add a regression test proving its current bug",
        acceptance_criteria=("a failing regression test exists for add(2, 3) == 5", "production code is untouched"),
        mvp_id="mvp-qa-smoke", work_item_id=WORK_ITEM_ID,
    )
    try:
        # No development-fix phase in this smoke (Part R scope: QA
        # analysis only, against already-broken code) — no author to
        # exclude, so developer_worker_id is genuinely None here.
        worker = asyncio.run(author.select_worker(estimation_request=estimation_request))
    except Exception as exc:  # NoEligibleWorkerError et al.
        print(f"[smoke] BLOCKED_BY_PROVIDER: no eligible qa_testing worker: {exc}")
        _print_result("BLOCKED_BY_PROVIDER", quota=quota)
        shutil.rmtree(ctx_dir, ignore_errors=True)
        return 1

    print(f"[smoke] real QA worker selected: {worker.worker_id} ({worker.provider}/{worker.backend})")

    outcome = asyncio.run(author.run_authoring(
        worker=worker, model=worker.profile().model, reasoning_effort=worker.profile().reasoning_effort,
        task_id=WORK_ITEM_ID, workspace=workspace,
        objective="review_candidate.py::add(a, b) is suspected buggy — analyze and add a regression test.",
        acceptance_criteria=(
            "A new test file demonstrates add(2, 3) == 5 and currently FAILS (RED) against the "
            "existing add() implementation.", "review_candidate.py itself is never modified.",
        ),
        base_sha=base_sha, head_sha=base_sha, changed_files=(), existing_coverage=(),
    ))

    head_after = _run_git(workspace, "rev-parse", "HEAD")
    production_untouched = "review_candidate.py" not in outcome.unauthorized_files
    red_confirmed = False
    if outcome.report is not None and outcome.report.tests_added:
        new_test_files = [f for f in outcome.report.tests_added if (workspace / f).is_file()]
        for f in new_test_files:
            probe = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", f], cwd=str(workspace), capture_output=True, text=True, timeout=60,
            )
            if probe.returncode != 0:
                red_confirmed = True

    verdict = "FAIL"
    requires_coding_agent = True
    if not outcome.succeeded:
        verdict = "INCONCLUSIVE" if outcome.report is None else "FAIL"
    elif not production_untouched:
        verdict = "FAIL"
    elif outcome.report and outcome.report.tests_added and red_confirmed:
        verdict = "FAIL"  # governed: a real, reproducing test exists and fails -> QA correctly reports FAIL
    smoke_status = "PASS" if (production_untouched and outcome.report is not None) else "FAIL"

    after = subprocess.run(
        ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
    ).stdout
    ralph_spike_unchanged = before == after
    print(f"[smoke] ~/projects/ralph-spike unchanged: {ralph_spike_unchanged}")
    if not ralph_spike_unchanged:
        smoke_status = "FAIL"

    audit = {
        "worker_id": worker.worker_id, "provider": worker.provider, "backend": worker.backend,
        "model": worker.profile().model, "reasoning_effort": worker.profile().reasoning_effort,
        "execution_id": outcome.execution_id, "authoring_outcome_succeeded": outcome.succeeded,
        "tests_added": list(outcome.report.tests_added) if outcome.report else [],
        "unauthorized_files": list(outcome.unauthorized_files), "production_untouched": production_untouched,
        "red_confirmed": red_confirmed, "governed_qa_verdict": verdict,
        "requires_coding_agent": requires_coding_agent, "head_before": base_sha, "head_after": head_after,
        "ralph_spike_unchanged": ralph_spike_unchanged, "smoke_status": smoke_status,
    }
    print(json.dumps(audit, indent=2, default=str))
    _print_result(smoke_status, quota=quota, audit=audit)

    if not args.keep_workspace:
        shutil.rmtree(ctx_dir, ignore_errors=True)
        print(f"[smoke] disposable workspace removed: {ctx_dir}")
    else:
        print(f"[smoke] disposable workspace kept: {ctx_dir}")

    return 0 if smoke_status == "PASS" else 1


def _print_result(status: str, *, quota: dict, audit: dict | None = None) -> None:
    print(f"[smoke] SMOKE STATUS: {status}")


if __name__ == "__main__":
    raise SystemExit(main())
