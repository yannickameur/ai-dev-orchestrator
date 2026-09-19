#!/usr/bin/env python3
"""Morpion Web 3D — WI-11 driver (NOT committed): governed
validation/integration of the preserved computer-turn regression fix.

Context (see docs/reports/morpion-computer-turn-regression-2026-09-18.md):
main (edbc576) is broken (computer never plays). wi-9 produced a real
candidate fix (commit 12f9079 on work/wi-9-computer-turn-regression,
verified this run to be RED on edbc576 / GREEN on 12f9079 via the
existing tests/browser/computer_turn_regression.test.mjs) but never
reached QA (DEV B failed with max_iterations before any verdict) and was
never merged. wi-10's own runtime state was durably WAITING but was lost
when the machine rebooted, because its store lived under /tmp.

This driver creates exactly one new WorkItem (WI-11) through the real,
unmodified MVPManager/WorkerSelector/RalphExecutionEngine/
GitGovernanceService/InternalQAEngine/QualityGateRunner, using a
PERSISTENT state root outside /tmp (survives reboots):

    ~/.local/state/ai-dev-orchestrator/projects/morpion-web-3d/

GitGovernanceService.prepare_work_item() always branches from main's own
current tip (confirmed by reading src/orchestrator/git_governance.py —
no candidate-SHA adoption primitive exists anywhere in this codebase).
So WI-11 is NOT seeded with 12f9079's content by this driver — DEV A is
instead instructed, via WorkItem.title/acceptance_criteria (the only
channel _build_dev_instructions consumes), to treat 12f9079 as an
already-established historical reference (root cause + minimal diff
shape) and reproduce the equivalent minimal correction + regression test
under normal governance, never to redesign or expand scope. This is the
explicitly instructed fallback path when governance cannot adopt a
candidate SHA directly.

Run explicitly (never via pytest):

    python scripts/run_morpion_wi11.py [--check-quota-only] [--max-cycles N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from orchestrator.execution_store import ExecutionStore  # noqa: E402
from orchestrator.git_governance import (  # noqa: E402
    RALPH_RUNTIME_NOISE_PREFIXES,
    GitGovernancePolicy,
    GitGovernanceService,
    GitWorkItemStatus,
    GitWorkItemStore,
    LocalGitWorkspace,
)
from orchestrator.handoff import HandoffStore  # noqa: E402
from orchestrator.internal_qa_engine import InternalQAEngine  # noqa: E402
from orchestrator.mvp_manager import MVPManager  # noqa: E402
from orchestrator.project_state import ProjectStateStore, WorkItemStatus  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.providers.mistral_vibe_adapter import MistralVibeAdapter  # noqa: E402
from orchestrator.qa import QAPhase, QAPolicy, QARunStatus, QARunStore, QAVerdictStatus  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import RalphExecutionEngine  # noqa: E402
from orchestrator.realization_report import RealizationReportService, RealizationReportStore, write_html  # noqa: E402
from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore  # noqa: E402
from orchestrator.wait import WaitStore  # noqa: E402
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import WorkerSelector  # noqa: E402

TARGET_REPO = Path.home() / "projects" / "morpion-web-3d"
WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"
PROJECT_ID = "proj-morpion-web-3d"
MVP_ID = "mvp-1-morpion-web-3d"
WORK_ITEM_ID = "WI-11"

# Persistent — survives a machine reboot, unlike /tmp (the cause of
# WI-10's lost runtime state; see docs/reports/morpion-computer-turn-
# regression-2026-09-18.md §27).
STATE_ROOT = Path.home() / ".local" / "state" / "ai-dev-orchestrator" / "projects" / "morpion-web-3d"

# Playwright package survived the reboot under the npm _npx cache (the
# browser binaries under ~/.cache/ms-playwright also survived); only the
# module resolution path needs to be supplied via NODE_PATH so the
# browser regression command (a plain `node --test ...` argv, no shell)
# can `require("playwright")`.
_PLAYWRIGHT_NODE_PATH = str(Path.home() / ".npm" / "_npx" / "e41f203b7505f1fb" / "node_modules")

_CANDIDATE_REFERENCE = (
    "This is a VALIDATION/INTEGRATION task, not a fresh bug discovery task. "
    "The bug, its root cause, and a working candidate fix are already "
    "established as historical fact:\n\n"
    "Symptom: after a human plays a valid move, the status shows "
    "\"L'ordinateur joue\" and the computer never plays — the app is "
    "stuck. Root cause (in assets/js/main.js's scheduleComputerMove()): "
    "the code captured `thisGeneration` from a counter incremented on "
    "every scheduled computer turn (`computerMoveGeneration`) but later "
    "compared it, inside the setTimeout callback, against a DIFFERENT "
    "counter incremented only on \"Nouvelle partie\" (`currentGameGeneration`). "
    "On the very first computer turn of any game these two counters are "
    "never equal (1 != 0), so the guard cancels the move unconditionally, "
    "every game, every difficulty.\n\n"
    "A candidate fix for this exact root cause already exists as evidence "
    "on the preserved (unmerged, FAILED WorkItem, never reopen it) branch "
    "work/wi-9-computer-turn-regression, commit 12f9079. Its diff is "
    "minimal: remove the separate `computerMoveGeneration` counter and "
    "capture `thisGeneration` directly from `currentGameGeneration` at "
    "schedule time, so the guard compares the correct pair of values (a "
    "2-line change to assets/js/main.js), plus a new self-contained "
    "Playwright browser regression test at "
    "tests/browser/computer_turn_regression.test.mjs (starts its own "
    "local HTTP server, drives a real Chromium page, asserts exactly one "
    "computer O appears and the status leaves \"L'ordinateur joue\" after "
    "a human move, and that starting a new game during the AI's think-"
    "delay never produces a phantom/double move). This driver has "
    "independently re-verified this run: that test FAILS against edbc576 "
    "(RED) and PASSES against 12f9079 (GREEN).\n\n"
    "You do not have git access to that branch/commit from this WorkItem "
    "(governance only lets you branch from main). Inspect main's current "
    "assets/js/main.js yourself, reproduce the equivalent minimal "
    "correction (same root cause, same shape of fix — do not invent a "
    "different mechanism), and add an equivalent browser regression test "
    "covering the same scenarios. Do not redesign the scheduling "
    "mechanism, do not add generation counters back, do not touch "
    "anything unrelated to this scheduling guard.\n\n"
)

WORK_ITEM = dict(
    work_item_id=WORK_ITEM_ID,
    title=(
        _CANDIDATE_REFERENCE
        + "WI-11 — Validate and integrate computer-turn regression fix."
    ),
    required_capabilities=("development",),
    dependencies=(),
    acceptance_criteria=(
        "After a human plays a valid first move, the computer plays exactly "
        "one O within the app's own think-delay, and the status leaves "
        "'L'ordinateur joue' (proven with real browser automation over "
        "HTTP, never file://).",
        "Root cause is the mismatched generation-counter comparison in "
        "scheduleComputerMove() (assets/js/main.js) — fix that comparison "
        "directly; do not reintroduce a second, differently-scoped counter.",
        "Starting a new game while a computer move is pending (think-delay "
        "in flight) never produces a stale/phantom O and never produces a "
        "double computer move.",
        "No computer move is ever played after the game has already ended.",
        "A browser regression test for the above (equivalent to "
        "tests/browser/computer_turn_regression.test.mjs) exists, passes, "
        "and produces zero browser console/page errors.",
        "The existing pytest suite (tests/*.py) still passes unmodified.",
        "The existing Node unit test suite (tests/js/*.test.mjs) still "
        "passes unmodified.",
        "No unrelated redesign: only the scheduling-guard bug and its "
        "regression coverage are touched.",
    ),
)

MAX_CYCLES = 20
TERMINAL_STATUSES = frozenset({WorkItemStatus.COMPLETED, WorkItemStatus.FAILED, WorkItemStatus.BLOCKED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _check_quota() -> dict:
    manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter(), "mistral": MistralVibeAdapter()},
        QuotaPolicy(state_ttl=timedelta(seconds=1)), clock=_utcnow,
    )
    result = {}
    for provider in ("anthropic", "openai", "mistral"):
        try:
            state = await manager.get(provider)
            result[provider] = {
                "available": state.availability.available,
                "reason": state.availability.reason.value if state.availability.reason else None,
                "windows": [
                    {"type": w.window_type, "reset_at": w.reset_at.isoformat() if w.reset_at else None, "utilization": w.utilization}
                    for w in state.quota_windows
                ],
            }
        except ProviderProbeError as exc:
            result[provider] = {"available": False, "reason": f"probe_error: {exc}"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-quota-only", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=MAX_CYCLES)
    args = parser.parse_args()

    if args.check_quota_only:
        print(json.dumps(asyncio.run(_check_quota()), indent=2))
        return 0

    print(f"[wi11] target workspace: {TARGET_REPO}")
    if not (TARGET_REPO / ".git").is_dir():
        print("[wi11] FAIL: target is not a git repository")
        return 1
    status = __import__("subprocess").run(
        ["git", "status", "--short"], cwd=str(TARGET_REPO), capture_output=True, text=True,
    ).stdout
    if status.strip():
        print(f"[wi11] FAIL: target workspace is not clean:\n{status}")
        return 1

    print("[wi11] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"[wi11] PERSISTENT state root: {STATE_ROOT}")

    project_store = ProjectStateStore(STATE_ROOT / "project.sqlite3", clock=_utcnow)
    handoff_store = HandoffStore(STATE_ROOT / "handoffs.sqlite3", clock=_utcnow)
    exec_store = ExecutionStore(STATE_ROOT / "executions.sqlite3", clock=_utcnow)
    qa_validation_store = ValidationStore(STATE_ROOT / "validation_qa.sqlite3", clock=_utcnow)
    qa_run_store = QARunStore(STATE_ROOT / "qa_runs.sqlite3", clock=_utcnow)
    git_store = GitWorkItemStore(STATE_ROOT / "git_governance.sqlite3", clock=_utcnow)
    report_store = RealizationReportStore(STATE_ROOT / "reports.sqlite3", clock=_utcnow)
    wait_store = WaitStore(STATE_ROOT / "waits.sqlite3", clock=_utcnow)

    # Required QA commands: pytest + Node unit tests + browser regression.
    # NODE_PATH lets the plain `node --test ...` argv (shell=False, no
    # wrapper) resolve `require("playwright")` from the surviving npm
    # cache copy — see _PLAYWRIGHT_NODE_PATH comment above.
    os.environ["NODE_PATH"] = _PLAYWRIGHT_NODE_PATH

    qa_commands = [
        ValidationCommand(
            validation_id="pytest-suite", kind=ValidationKind.UNIT_TEST,
            argv=("python3", "-m", "pytest", "-q"),
        ),
        ValidationCommand(
            validation_id="node-unit-tests", kind=ValidationKind.UNIT_TEST,
            argv=("node", "--test", "tests/js/ai.test.mjs", "tests/js/engine.test.mjs", "tests/js/storage.test.mjs"),
        ),
        ValidationCommand(
            validation_id="browser-computer-turn-regression", kind=ValidationKind.CUSTOM,
            argv=("node", "--test", "tests/browser/computer_turn_regression.test.mjs"),
            timeout_seconds=60.0,
        ),
    ]
    qa_validation_store.set_project_commands(PROJECT_ID, qa_commands)

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter(), "mistral": MistralVibeAdapter()},
        QuotaPolicy(state_ttl=timedelta(minutes=2)), clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(exec_store, clock=_utcnow)
    qa_gate_runner = QualityGateRunner(qa_validation_store, clock=_utcnow)
    qa_engine = InternalQAEngine(validation_store=qa_validation_store, gate_runner=qa_gate_runner, clock=_utcnow)

    # require_review=False / require_required_gates=False: exactly what
    # tests/test_mvp_manager_lean_feature_flow.py's own fixture uses for
    # LEAN_FEATURE_FLOW — this manager wires no quality_gate_runner and no
    # review_store at all (DEV B's corrective review replaces review;
    # the QA phase's own deterministic commands ARE the gate), so those
    # two independent eligibility checks must be turned off or they can
    # never be satisfied regardless of how many real QA commands pass.
    git_service = GitGovernanceService(
        git_store,
        policy=GitGovernancePolicy(
            auto_merge=True, base_branch="main", require_review=False, require_required_gates=False,
        ),
        clock=_utcnow,
    )

    manager = MVPManager(
        project_store, handoff_store, worker_selector, execution_engine,
        wait_store=wait_store, execution_store=exec_store, git_governance_service=git_service,
        qa_engine=qa_engine, qa_policy=QAPolicy(), qa_run_store=qa_run_store,
        clock=_utcnow,
    )

    if project_store.list_work_items(MVP_ID) == []:
        try:
            project_store.get_project(PROJECT_ID)
        except Exception:
            project_store.create_project(project_id=PROJECT_ID, name="Morpion Web 3D", workspace=TARGET_REPO)
            project_store.create_mvp(
                mvp_id=MVP_ID, project_id=PROJECT_ID,
                objective="Validate and integrate the preserved computer-turn regression fix (WI-11).",
            )
        project_store.create_work_item(mvp_id=MVP_ID, **WORK_ITEM)
    project_store.refresh_readiness(MVP_ID)

    results = []
    for cycle in range(1, args.max_cycles + 1):
        print(f"[wi11] run_next_work_item cycle {cycle}...")
        result = asyncio.run(manager.run_next_work_item(MVP_ID))
        if result is None:
            all_terminal = all(
                wi.status in TERMINAL_STATUSES for wi in project_store.list_work_items(MVP_ID)
            )
            print(f"[wi11] nothing eligible/due (all_terminal={all_terminal})")
            break
        results.append(result)
        print(f"[wi11] cycle {cycle}: {result.work_item.work_item_id} -> {result.work_item.status.value}"
              + (f" ({result.work_item.blocked_reason})" if result.work_item.blocked_reason else ""))
        if result.work_item.status is WorkItemStatus.WAITING and result.wait is not None:
            print(f"[wi11] WAITING: phase={result.wait.phase.value} eligible_at={result.wait.eligible_at}")
        all_terminal = all(
            wi.status in TERMINAL_STATUSES for wi in project_store.list_work_items(MVP_ID)
        )
        if all_terminal:
            break

    # Finalize: a WorkItem already COMPLETED (real QA PASS persisted) but
    # whose governed git record never reached MERGED (e.g. a prior run of
    # this same driver used a misconfigured policy) is not re-run through
    # DEV A/DEV B/QA — that evidence already exists and is still valid.
    # This calls the exact real GitGovernanceService primitives
    # (compute_merge_eligibility -> merge -> tag), never a manual
    # branch/cherry-pick/fast-forward bypass.
    for wi in project_store.list_work_items(MVP_ID):
        if wi.status is not WorkItemStatus.COMPLETED:
            continue
        record = git_store.try_get(wi.work_item_id)
        if record is None or record.status is GitWorkItemStatus.MERGED:
            continue
        latest_qa = qa_run_store.latest_for_work_item(wi.work_item_id)
        qa_passed = (
            latest_qa is not None
            and latest_qa.phase is QAPhase.FINAL_VERIFICATION
            and latest_qa.verdict is not None
            and latest_qa.verdict.status is QAVerdictStatus.PASS
        )
        qa_run_terminal = latest_qa is not None and latest_qa.status in (QARunStatus.COMPLETED, QARunStatus.FAILED)
        qa_git_sha = latest_qa.expected_head_sha if latest_qa is not None else None
        eligibility = git_service.compute_merge_eligibility(
            wi.work_item_id, repository_path=TARGET_REPO, work_item_status=wi.status.value,
            gate_passed=None, gate_git_sha=None, review_approved=None, review_git_sha=None,
            qa_required=True, qa_passed=qa_passed, qa_git_sha=qa_git_sha, qa_run_terminal=qa_run_terminal,
            noise_path_prefixes=RALPH_RUNTIME_NOISE_PREFIXES,
        )
        print(f"[wi11] finalize {wi.work_item_id}: eligibility.mergeable={eligibility.mergeable} reason={eligibility.reason}")
        if eligibility.mergeable:
            merged_record = git_service.merge(
                wi.work_item_id, repository_path=TARGET_REPO, eligibility=eligibility,
                noise_path_prefixes=RALPH_RUNTIME_NOISE_PREFIXES,
            )
            tag_name = f"feature/{wi.work_item_id}/done"
            LocalGitWorkspace(TARGET_REPO).create_tag(tag_name, sha=merged_record.merged_sha)
            print(f"[wi11] finalize {wi.work_item_id}: MERGED sha={merged_record.merged_sha} tag={tag_name}")

    print("\n[wi11] === final WorkItem states ===")
    final_items = project_store.list_work_items(MVP_ID)
    for wi in final_items:
        git_record = git_store.try_get(wi.work_item_id)
        print(f"  {wi.work_item_id}: status={wi.status.value} "
              f"git={git_record.status.value if git_record else '(none)'} "
              f"merged_sha={git_record.merged_sha if git_record else None} "
              f"blocked_reason={wi.blocked_reason}")

    report_service = RealizationReportService(
        project_store, exec_store, handoff_store, report_store,
        validation_store=qa_validation_store,
        git_work_item_store=git_store, qa_run_store=qa_run_store, clock=_utcnow,
    )
    for wi in final_items:
        report = report_service.generate(project_id=PROJECT_ID, mvp_id=MVP_ID, work_item_id=wi.work_item_id)
        report_path = STATE_ROOT / f"realization-{wi.work_item_id}.html"
        write_html(report, report_path)
        print(f"[wi11] RealizationReport for {wi.work_item_id} written to: {report_path}")

    all_completed_merged = all(
        wi.status is WorkItemStatus.COMPLETED
        and (git_store.try_get(wi.work_item_id) is not None)
        and git_store.get(wi.work_item_id).status is GitWorkItemStatus.MERGED
        and git_store.get(wi.work_item_id).merged_sha == git_store.get(wi.work_item_id).current_head_sha
        for wi in final_items
    )
    print(f"\n[wi11] WI11_STATUS: {'PASS' if all_completed_merged else 'INCOMPLETE'}")
    print(f"[wi11] state root (persistent, reboot-safe): {STATE_ROOT}")
    return 0 if all_completed_merged else 2


if __name__ == "__main__":
    raise SystemExit(main())
