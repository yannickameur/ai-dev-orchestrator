#!/usr/bin/env python3
"""External project pilot harness: drives a real, approved
SPEC.md/ROADMAP.md through the real ai-dev-orchestrator MVPManager
(WorkItem Flow — the only WorkItem execution pipeline this project
implements) against a REAL external project workspace — never a
disposable /tmp copy, never a synthetic defect, never a negative
control (those only make sense for self-dogfood on this control plane
itself).

Target workspace: the external project repo (e.g. ~/projects/mars-rover-kata)
IS the governed workspace, directly — real commits, real branches, a real
`git merge --ff-only` land there. This control plane
(~/projects/ai-dev-orchestrator) is never a git target of any operation
here.

This harness is intentionally thin: it only turns an approved
Project/MVP/WorkItem decomposition (mirroring the target repo's own
ROADMAP.md, never re-decomposed here) into calls against the REAL,
already-existing MVPManager/WorkerSelector/RalphExecutionEngine/
GitGovernanceService/InternalQAEngine/QualityGateRunner. It never
reimplements any of them, and never writes a single line of the target
project's own source itself — all real code changes are produced
exclusively by the real workers Ralph launches.

NEVER run this via pytest. Run explicitly:

    python scripts/run_external_project_pilot.py [--check-quota-only] [--max-cycles N]

If a WorkItem needs to wait for quota, this exits reporting WAITING with
the known eligible_at (from WaitStore) rather than blocking/sleeping —
the caller is expected to reschedule and re-run this same script later,
exactly like the self-dogfood acceptance script's own real-provider runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from orchestrator.execution_store import ExecutionStore  # noqa: E402
from orchestrator.git_governance import GitGovernancePolicy, GitGovernanceService, GitWorkItemStatus, GitWorkItemStore  # noqa: E402
from orchestrator.handoff import HandoffStore  # noqa: E402
from orchestrator.internal_qa_engine import InternalQAEngine  # noqa: E402
from orchestrator.mvp_manager import MVPManager  # noqa: E402
from orchestrator.project_state import ProjectStateStore, WorkItemStatus  # noqa: E402
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from orchestrator.providers.codex_adapter import CodexAdapter  # noqa: E402
from orchestrator.qa import QAPolicy, QARunStore  # noqa: E402
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy  # noqa: E402
from orchestrator.ralph_execution_engine import RalphExecutionEngine  # noqa: E402
from orchestrator.realization_report import RealizationReportService, RealizationReportStore, write_html  # noqa: E402
from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore  # noqa: E402
from orchestrator.wait import WaitStore  # noqa: E402
from orchestrator.worker_registry import WorkerRegistry  # noqa: E402
from orchestrator.worker_selector import WorkerSelector  # noqa: E402

# --- pilot-specific, approved facts (mirrors mars-rover-kata's own ROADMAP.md exactly) ---
TARGET_REPO = Path.home() / "projects" / "mars-rover-kata"
WORKERS_CONFIG = REPO_ROOT / "config" / "workers.yaml"
PROJECT_ID = "proj-mars-rover-kata"
MVP_ID = "mvp-1-mars-rover-kata"

# Injected into every worker's actual prompt (development, QA authoring,
# AND review all consume WorkItem.title verbatim as their objective/story
# text — this is the only lever this harness has to reach real worker
# instructions without touching MVPManager's own prompt-building code).
_SCOPE_PREAMBLE = (
    "[STRICT SCOPE — applies to whichever role you are: developer, QA "
    "test author, or independent reviewer]\n"
    "Implement/verify ONLY the acceptance criteria of the CURRENT "
    "WorkItem below. SPEC.md and ROADMAP.md in this repository are the "
    "only functional sources of truth — nothing else.\n"
    "Do not implement or anticipate a future WorkItem's scope ahead of "
    "time, even partially.\n"
    "Do not search the web, browse external sites, or consult any "
    "external repository, blog, tutorial, or Q&A site for a Mars Rover "
    "kata solution or approach. Solve this strictly from SPEC.md, "
    "ROADMAP.md, this repository's own existing code/tests, and general "
    "Python language knowledge — nothing fetched from the internet.\n"
    "Do not add features, files, dependencies, or abstractions beyond "
    "what the acceptance criteria explicitly require (no wrap-around, no "
    "map bounds, no history/undo, no CLI, no HTTP API, no UI, no "
    "database/persistence, no extra commands, no frameworks). Keep the "
    "solution minimal — YAGNI.\n"
    "If you are the reviewer: explicitly flag any scope creep you "
    "observe as a finding, but never treat an unrequested feature "
    "(e.g. wrap-around) as a missing requirement or a blocking finding "
    "on its own — the acceptance criteria are the sole authority.\n\n"
)

WORK_ITEMS = [
    dict(
        work_item_id="WI-1",
        title=(
            _SCOPE_PREAMBLE
            + "WI-1 — Rover movement and rotation. Objective: implement the core deterministic "
            "rover state transitions without obstacle handling, per SPEC.md and ROADMAP.md WI-1. "
            "Obstacle handling and the public simulate(...) API belong to WI-2 — do not implement "
            "them now."
        ),
        required_capabilities=("development",),
        dependencies=(),
        acceptance_criteria=(
            "The rover accepts an initial (x, y, direction) state.",
            "Directions supported are exactly: N, E, S, W",
            "L rotates left by 90 degrees.",
            "R rotates right by 90 degrees.",
            "Four identical rotations return to the original direction.",
            "F moves one coordinate in the current direction.",
            "Multiple commands are executed sequentially.",
            "Invalid directions fail explicitly.",
            "Invalid commands fail explicitly.",
            "Tests cover the behaviours above.",
        ),
    ),
    dict(
        work_item_id="WI-2",
        title=(
            _SCOPE_PREAMBLE
            + "WI-2 — Obstacles and public simulator API. Objective: complete the Mars Rover "
            "behaviour by adding obstacle handling and the approved public API, per SPEC.md and "
            "ROADMAP.md WI-2. WI-1 (movement/rotation) is already implemented, reviewed, and "
            "merged — do not redo it; only add what this WorkItem's acceptance criteria require "
            "on top of it."
        ),
        required_capabilities=("development",),
        dependencies=("WI-1",),
        acceptance_criteria=(
            "Expose the public simulate(...) behaviour defined in SPEC.md.",
            "Obstacles are provided as coordinate positions.",
            "Before each F, the next position is checked.",
            "A rover never enters an obstacle position.",
            "A blocked F leaves the rover position unchanged.",
            "A blocked movement does not prevent subsequent commands from running.",
            "Turns remain possible after a blocked movement.",
            'simulate((0,0,"N"), {(0,1)}, "FRF") returns (1,0,"E").',
            "No map wrap-around behaviour is introduced.",
            "Regression tests cover obstacle behaviour and command sequencing.",
            "Full pytest suite passes.",
        ),
    ),
]

MAX_CYCLES = 40
TERMINAL_STATUSES = frozenset({WorkItemStatus.COMPLETED, WorkItemStatus.FAILED, WorkItemStatus.BLOCKED})


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
    parser.add_argument("--ctx-dir", type=str, default=None, help="Reuse an existing store directory to resume.")
    args = parser.parse_args()

    if args.check_quota_only:
        print(json.dumps(asyncio.run(_check_quota()), indent=2))
        return 0

    print(f"[pilot] target workspace: {TARGET_REPO}")
    if not (TARGET_REPO / ".git").is_dir():
        print("[pilot] FAIL: target is not a git repository")
        return 1
    status = __import__("subprocess").run(
        ["git", "status", "--short"], cwd=str(TARGET_REPO), capture_output=True, text=True,
    ).stdout
    if status.strip():
        print(f"[pilot] FAIL: target workspace is not clean:\n{status}")
        return 1

    print("[pilot] checking real provider availability (read-only)...")
    quota = asyncio.run(_check_quota())
    print(json.dumps(quota, indent=2))
    if not (quota.get("anthropic", {}).get("available") and quota.get("openai", {}).get("available")):
        print("[pilot] BLOCKED_BY_PROVIDER: this pipeline needs BOTH providers available "
              "(author != QA, author != reviewer) — see reset_at above.")
        return 1

    # PERSISTENT by default, outside /tmp — a Morpion Web 3D pilot run
    # (WI-10) proved that cross-process persistence under /tmp survives a
    # crashed process but NOT a machine reboot (/tmp is cleared on boot),
    # silently losing a durable WAITING WorkItem's entire state. This
    # script's own docstring promises "re-run this same script later" to
    # resume a WAITING WorkItem — a random tempfile.mkdtemp() default
    # broke that promise even before considering reboots (the caller
    # would have to remember and pass back an exact random path). Default
    # is now a STABLE path derived from PROJECT_ID, so simply re-running
    # this script with no arguments resumes correctly; --ctx-dir still
    # overrides it for an explicitly isolated/disposable run.
    default_ctx_dir = Path.home() / ".local" / "state" / "ai-dev-orchestrator" / "projects" / PROJECT_ID
    ctx_dir = Path(args.ctx_dir) if args.ctx_dir else default_ctx_dir
    ctx_dir.mkdir(parents=True, exist_ok=True)
    print(f"[pilot] governance/execution store directory (persistent): {ctx_dir}")

    project_store = ProjectStateStore(ctx_dir / "project.sqlite3", clock=_utcnow)
    handoff_store = HandoffStore(ctx_dir / "handoffs.sqlite3", clock=_utcnow)
    exec_store = ExecutionStore(ctx_dir / "executions.sqlite3", clock=_utcnow)
    qa_validation_store = ValidationStore(ctx_dir / "validation_qa.sqlite3", clock=_utcnow)
    qa_run_store = QARunStore(ctx_dir / "qa_runs.sqlite3", clock=_utcnow)
    git_store = GitWorkItemStore(ctx_dir / "git_governance.sqlite3", clock=_utcnow)
    report_store = RealizationReportStore(ctx_dir / "reports.sqlite3", clock=_utcnow)
    wait_store = WaitStore(ctx_dir / "waits.sqlite3", clock=_utcnow)

    target_python = TARGET_REPO / ".venv" / "bin" / "python"
    targeted_command = ValidationCommand(
        validation_id="pytest-suite", kind=ValidationKind.UNIT_TEST,
        argv=(str(target_python), "-m", "pytest"),
    )
    # The single QA phase's own deterministic gate — under WorkItem Flow
    # this command run IS the quality gate (no separate governed_full-style
    # quality-gate step exists in this pipeline at all).
    qa_validation_store.set_project_commands(PROJECT_ID, [targeted_command])

    registry = WorkerRegistry.load(WORKERS_CONFIG)
    quota_manager = QuotaManager(
        {"anthropic": ClaudeCodeAdapter(), "openai": CodexAdapter()}, QuotaPolicy(state_ttl=timedelta(minutes=2)),
        clock=_utcnow,
    )
    worker_selector = WorkerSelector(list(registry.enabled_workers()), quota_manager)
    execution_engine = RalphExecutionEngine(exec_store, clock=_utcnow)
    qa_gate_runner = QualityGateRunner(qa_validation_store, clock=_utcnow)
    qa_engine = InternalQAEngine(validation_store=qa_validation_store, gate_runner=qa_gate_runner, clock=_utcnow)

    # require_review/require_required_gates default to True in
    # GitGovernancePolicy but WorkItem Flow (the only workflow
    # MVPManager implements — GOVERNED_FULL was removed before the first
    # public release, see ROADMAP.md's dated removal entry) wires neither
    # a quality_gate_runner nor a review_store (the QA phase's own
    # deterministic commands ARE the gate; DEV B's corrective review
    # replaces independent review) — left at their default, merge
    # eligibility can never be satisfied no matter how many real QA
    # commands pass. Exactly the wiring
    # tests/test_mvp_manager_workitem_flow.py's own fixture uses.
    # Found via a real external-project pilot (Morpion Web 3D WI-11): a
    # WorkItem reached COMPLETED with a real QA PASS but its git record
    # stayed IN_PROGRESS forever.
    git_service = GitGovernanceService(
        git_store,
        policy=GitGovernancePolicy(auto_merge=True, base_branch="main", require_review=False, require_required_gates=False),
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
            project_store.create_project(project_id=PROJECT_ID, name="Mars Rover Kata", workspace=TARGET_REPO)
            project_store.create_mvp(
                mvp_id=MVP_ID, project_id=PROJECT_ID,
                objective="Deliver the Mars Rover behaviour defined in SPEC.md",
            )
        for wi in WORK_ITEMS:
            project_store.create_work_item(mvp_id=MVP_ID, **wi)
    project_store.refresh_readiness(MVP_ID)

    results = []
    for cycle in range(1, args.max_cycles + 1):
        print(f"[pilot] run_next_work_item cycle {cycle}...")
        result = asyncio.run(manager.run_next_work_item(MVP_ID))
        if result is None:
            all_terminal = all(
                wi.status in TERMINAL_STATUSES for wi in project_store.list_work_items(MVP_ID)
            )
            print(f"[pilot] nothing eligible/due (all_terminal={all_terminal})")
            break
        results.append(result)
        print(f"[pilot] cycle {cycle}: {result.work_item.work_item_id} -> {result.work_item.status.value}"
              + (f" ({result.work_item.blocked_reason})" if result.work_item.blocked_reason else ""))
        if result.work_item.status is WorkItemStatus.WAITING and result.wait is not None:
            print(f"[pilot] WAITING: phase={result.wait.phase.value} eligible_at={result.wait.eligible_at}")
        all_terminal = all(
            wi.status in TERMINAL_STATUSES for wi in project_store.list_work_items(MVP_ID)
        )
        if all_terminal:
            break

    print("\n[pilot] === final WorkItem states ===")
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
        report_path = ctx_dir / f"realization-{wi.work_item_id}.html"
        write_html(report, report_path)
        print(f"[pilot] RealizationReport for {wi.work_item_id} written to: {report_path}")

    all_completed_merged = all(
        wi.status is WorkItemStatus.COMPLETED
        and (git_store.try_get(wi.work_item_id) is not None)
        and git_store.get(wi.work_item_id).status is GitWorkItemStatus.MERGED
        and git_store.get(wi.work_item_id).merged_sha == git_store.get(wi.work_item_id).current_head_sha
        for wi in final_items
    )
    print(f"\n[pilot] MVP_1_PILOT_STATUS: {'PASS' if all_completed_merged else 'INCOMPLETE'}")
    print(f"[pilot] ctx_dir preserved for inspection/resume: {ctx_dir}")
    return 0 if all_completed_merged else 2


if __name__ == "__main__":
    raise SystemExit(main())
