"""Cross-worker cold-resume E2E — Slice 17 acceptance validation.

Validates, end to end and offline, the assembly the unit/integration
tests only exercise separately: a small real project (a disposable copy
of ``~/projects/ralph-spike``), Worker A starts real work, an execution
is interrupted (simulating a crashed process), every store/service is
closed, a cold restart reconstructs the entire graph from the persisted
SQLite files and workspace alone, ``RecoveryCoordinator`` reconciles the
orphaned execution into a durable handoff, adaptive selection (Slice 17)
re-runs the pre-flight and picks a *different* Worker B (Worker A's
provider is out of quota at restart — a realistic reason a process would
have been interrupted in the first place), and Worker B finishes the
work — never using anything from Worker A's "conversational memory",
only durable, persisted facts.

REAL, not mocked: ``ProjectStateStore``/``ExecutionStore``/``HandoffStore``/
``ExecutionRecommendationStore``/``AdaptiveExecutionDecisionStore`` are all
real sqlite3-backed stores, explicitly closed at the end of "Phase 1" and
reopened fresh in "Phase 2" from the same files (no Python object from
Phase 1 is ever reused in Phase 2 — see ``_run_phase_one``/``_run_phase_two``,
which communicate only through a plain, picklable-shaped fact bag).
``WorkerSelector``/``QuotaManager``/``ExecutionRecommendationService``/
``AdaptiveExecutionSelector``/``RalphExecutionEngine``/``MVPManager`` are
all the real classes. Only the two ``RalphExecutionEngine`` subprocess
boundaries are faked (no real Claude/Codex/Ralph invocation) — the fake
subprocess runner used here genuinely mutates files and makes real git
commits in the disposable workspace copy, so the scenario is concrete,
not simulated in memory.

The chosen "tiny but real" feature: ``review_candidate.py`` in
ralph-spike contains a deliberately-wrong ``add(a, b): return a - b``
(the exact artifact ``docs/SPIKE_RALPH.md``'s author!=reviewer spike
already used). Worker A writes the regression test (TDD red) before
being interrupted; Worker B fixes the bug (TDD green) after the cold
resume, and the mini-project's own test script is run for real at the
end.

~/projects/ralph-spike itself is NEVER modified — only a disposable
``tmp_path``-based copy (pytest's own tempfile-backed fixture) is ever
touched, and it is torn down automatically at the end of the test.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copytree

from orchestrator.adaptive_execution import AdaptiveExecutionDecisionStore, AdaptiveExecutionSelector
from orchestrator.complexity_estimation import (
    ComplexityEstimationRequest,
    ExecutionRecommendationService,
    ExecutionRecommendationStore,
)
from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.mvp_manager import DEFAULT_WORK_ITEM_ROLE, MVPManager
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason
from orchestrator.qa import QAPolicy, QARunStore, QAResult
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.ralph_execution_engine import RalphExecutionEngine
from orchestrator.worker_selector import ExecutionProfile, QualityTier, Worker, WorkerSelector

RALPH_SPIKE_SOURCE = Path.home() / "projects" / "ralph-spike"

UTC_T0 = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)
UTC_T1 = UTC_T0 + timedelta(minutes=1)
UTC_T2 = UTC_T0 + timedelta(hours=4)  # "restart" happens well after T0

PROJECT_ID = "proj-e2e"
MVP_ID = "mvp-e2e"
WORK_ITEM_ID = "wi-fix-add"

WORKER_A_ID = "worker-a-claude-like"
WORKER_B_ID = "worker-b-codex-like"
PROVIDER_A = "provider-a"
PROVIDER_B = "provider-b"

_STANDARD_TIER_PAYLOAD = json.dumps({
    "minimum_quality_tier": "STANDARD",
    "recommended_reasoning": None,
    "reasons": ["small, well-defined bug fix with a clear regression test"],
})

TEST_FILE_CONTENT = (
    "from review_candidate import add\n\n\n"
    "def test_add_returns_the_sum():\n"
    "    assert add(2, 3) == 5\n\n\n"
    "if __name__ == '__main__':\n"
    "    test_add_returns_the_sum()\n"
    "    print('OK')\n"
)

FIXED_ADD_CONTENT = "def add(a, b):\n    return a + b\n"


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _git_commit_all(cwd: Path, message: str) -> str:
    _run_git(cwd, "add", "-A")
    _run_git(cwd, "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E Test", "commit", "-m", message)
    return _run_git(cwd, "rev-parse", "HEAD")


def _copy_ralph_spike(tmp_path: Path) -> Path:
    """A disposable copy — the only thing this test ever writes to."""
    assert RALPH_SPIKE_SOURCE.is_dir(), f"expected {RALPH_SPIKE_SOURCE} to exist for this test"
    destination = tmp_path / "ralph-spike-copy"
    copytree(RALPH_SPIKE_SOURCE, destination)
    return destination


class _FakeAdapter(ProviderAdapter):
    def __init__(self, available: bool) -> None:
        self._available = available

    async def probe(self) -> ProviderState:
        return ProviderState(
            provider="fake",
            availability=ProviderAvailability(
                available=self._available, observed_at=UTC_T0,
                reason=None if self._available else UnavailabilityReason.QUOTA_EXHAUSTED,
            ),
            observed_at=UTC_T0,
        )


class _ScriptedFileMutatingRunner:
    """Stands in for the real `ralph` subprocess — genuinely mutates the
    workspace and makes real git commits, so the scenario stays concrete.
    Each queued step corresponds to exactly one RalphExecutionEngine call,
    in order.
    """

    def __init__(self, steps: list[dict]) -> None:
        self._steps = list(steps)
        self.calls: list[tuple] = []

    async def __call__(self, args: list[str], cwd: Path, timeout: float) -> tuple[int, bytes, bytes]:
        instructions = Path(args[args.index("-P") + 1]).read_text()
        self.calls.append((args, cwd, timeout, instructions))
        assert self._steps, "fake runner called more times than scripted"
        step = self._steps.pop(0)
        mutate = step.get("mutate")
        if mutate is not None:
            mutate(Path(cwd))

        ralph_dir = Path(cwd) / ".ralph"
        ralph_dir.mkdir(parents=True, exist_ok=True)
        (ralph_dir / "current-loop-id").write_text(step.get("loop_id", "e2e-loop"))
        events_filename = f"events-{len(self.calls)}.jsonl"
        (ralph_dir / "current-events").write_text(f".ralph/{events_filename}")
        line = json.dumps({
            "topic": step["topic"], "ts": UTC_T0.isoformat(), "payload": step.get("payload"),
        })
        (ralph_dir / events_filename).write_text(line + "\n")
        return step.get("exit_code", 0), step.get("stdout", b""), step.get("stderr", b"")


def _worker_a() -> Worker:
    return Worker(
        worker_id=WORKER_A_ID, display_name="Alice (E2E)", provider=PROVIDER_A, backend="claude_code",
        capabilities=frozenset({"development", "complexity_estimation"}), priority=100,
        profiles=(ExecutionProfile(profile_id="standard", quality_tier=QualityTier.STANDARD, model="model-a-standard", cost_rank=10),),
        default_profile_id="standard", estimator_profile_id="standard",
    )


def _worker_b() -> Worker:
    return Worker(
        worker_id=WORKER_B_ID, display_name="Bob (E2E)", provider=PROVIDER_B, backend="codex",
        capabilities=frozenset({"development", "complexity_estimation"}), priority=90,
        profiles=(
            ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="model-b-economy", cost_rank=10),
            ExecutionProfile(profile_id="standard", quality_tier=QualityTier.STANDARD, model="model-b-standard", cost_rank=20),
        ),
        default_profile_id="economy", estimator_profile_id="economy",
    )


def _worker_c() -> Worker:
    """A second worker on Worker B's own provider — mirrors this
    project's real worker-pool shape (>= 2 independent workers per
    participating provider, config/workers.yaml). Under
    WorkItem Flow, Worker B (resumed as the developer) needs a
    genuinely independent DEV B; lower priority than Worker B so it is
    never chosen over Worker B for the developer role itself, only for
    DEV B once Worker B is excluded as author."""
    return Worker.with_single_profile(
        worker_id="worker-c-codex-like", display_name="Carol (E2E)", provider=PROVIDER_B, backend="codex",
        model="model-c-standard", capabilities=frozenset({"development"}), priority=80,
    )


@dataclass(frozen=True)
class PhaseOneFacts:
    """Everything Phase 2 needs — paths and ids only, never a live Python
    object from Phase 1's service graph."""

    tmp_path: Path
    workspace: Path
    project_db: Path
    execution_db: Path
    handoff_db: Path
    recommendation_db: Path
    decision_db: Path
    execution_id_a: str
    git_sha_after_a: str
    recommendation_id_a: str
    decision_id_a: str


def _run_phase_one(tmp_path: Path) -> PhaseOneFacts:
    workspace = _copy_ralph_spike(tmp_path)
    project_db = tmp_path / "project.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    handoff_db = tmp_path / "handoff.sqlite3"
    recommendation_db = tmp_path / "recommendation.sqlite3"
    decision_db = tmp_path / "decision.sqlite3"

    project_store = ProjectStateStore(project_db, clock=lambda: UTC_T0)
    execution_store = ExecutionStore(execution_db, clock=lambda: UTC_T0)
    handoff_store = HandoffStore(handoff_db, clock=lambda: UTC_T0)
    recommendation_store = ExecutionRecommendationStore(recommendation_db, clock=lambda: UTC_T0)
    decision_store = AdaptiveExecutionDecisionStore(decision_db, clock=lambda: UTC_T0)

    project_store.create_project(project_id=PROJECT_ID, name="Ralph Spike E2E", workspace=workspace)
    project_store.create_mvp(mvp_id=MVP_ID, project_id=PROJECT_ID, objective="Fix add() in review_candidate.py")
    project_store.create_work_item(
        work_item_id=WORK_ITEM_ID, mvp_id=MVP_ID,
        title="Fix add() to actually add, backed by a regression test",
        required_capabilities=("development",),
        acceptance_criteria=("add(2, 3) returns 5", "test_review_candidate.py passes"),
    )
    project_store.refresh_readiness(MVP_ID)
    assert project_store.get_work_item(WORK_ITEM_ID).status is WorkItemStatus.READY
    project_store.mark_work_item_running(WORK_ITEM_ID)

    worker_a, worker_b = _worker_a(), _worker_b()
    quota_manager = QuotaManager(
        {PROVIDER_A: _FakeAdapter(available=True), PROVIDER_B: _FakeAdapter(available=True)},
        QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_T0,
    )
    worker_selector = WorkerSelector([worker_a, worker_b], quota_manager)

    runner = _ScriptedFileMutatingRunner([
        {"topic": "execution.profile_recommended", "payload": _STANDARD_TIER_PAYLOAD},
    ])
    execution_engine = RalphExecutionEngine(execution_store, subprocess_runner=runner, clock=lambda: UTC_T0)
    recommendation_service = ExecutionRecommendationService(
        recommendation_store, worker_selector, execution_engine, clock=lambda: UTC_T0, id_factory=lambda: "rec-a",
    )
    adaptive_selector = AdaptiveExecutionSelector(
        decision_store, recommendation_service, worker_selector, clock=lambda: UTC_T0, id_factory=lambda: "decision-a",
    )

    estimation_request = ComplexityEstimationRequest(
        project_id=PROJECT_ID, role=DEFAULT_WORK_ITEM_ROLE, workspace=workspace,
        objective="Fix add() to actually add, backed by a regression test",
        acceptance_criteria=("add(2, 3) returns 5", "test_review_candidate.py passes"),
        mvp_id=MVP_ID, work_item_id=WORK_ITEM_ID,
    )
    selection = asyncio.run(adaptive_selector.select(
        estimation_request=estimation_request, required_capabilities=frozenset({"development"}),
    ))
    assert selection.worker.worker_id == WORKER_A_ID  # priority 100 beats 90, both available
    assert selection.decision.quality_tier is QualityTier.STANDARD

    # Worker A's real, concrete contribution before being interrupted:
    # writes the regression test (TDD red) — never Worker B's job.
    (workspace / "test_review_candidate.py").write_text(TEST_FILE_CONTENT)
    git_sha_after_a = _git_commit_all(workspace, "Worker A: add regression test for add()")

    execution_id_a = "exec-a-orphaned"
    execution_store.create(
        execution_id=execution_id_a, task_id=WORK_ITEM_ID, worker_id=selection.worker.worker_id,
        provider=selection.worker.provider, backend=selection.worker.backend,
        model=selection.decision.model, reasoning_effort=selection.decision.reasoning_effort,
        role=DEFAULT_WORK_ITEM_ROLE, started_at=UTC_T1,
    )
    # execution_id_a stays RUNNING here — never finalized: this is exactly
    # what a process crash mid-run leaves behind. git_sha_after_a is real,
    # committed work; it was simply never recorded on the ExecutionRecord
    # because the process died before it could call mark_succeeded/etc.

    facts = PhaseOneFacts(
        tmp_path=tmp_path, workspace=workspace, project_db=project_db, execution_db=execution_db,
        handoff_db=handoff_db, recommendation_db=recommendation_db, decision_db=decision_db,
        execution_id_a=execution_id_a, git_sha_after_a=git_sha_after_a,
        recommendation_id_a=selection.decision.recommendation_id, decision_id_a=selection.decision.decision_id,
    )

    # A real process shutdown: every store connection is closed. Nothing
    # from these Python objects survives into Phase 2.
    project_store.close()
    execution_store.close()
    handoff_store.close()
    recommendation_store.close()
    decision_store.close()

    return facts


class _AlwaysPassQAEngine:
    """Minimal ``QAEngine`` Protocol fake for this E2E scenario — no
    LLM/Ralph execution. Real deterministic QA behavior (bounded retry,
    verdict computation) is already exhaustively covered by
    ``tests/test_mvp_manager_workitem_flow.py`` and ``tests/test_qa.py``;
    this file's own scope is cross-worker cold-resume + adaptive
    selection, so QA here only needs to genuinely run and PASS."""

    engine_id = "fake-qa-e2e"

    def run(self, request) -> QAResult:
        return QAResult(
            engine_id=self.engine_id, observed_head_sha=request.head_sha,
            started_at=UTC_T2, finished_at=UTC_T2,
            tests_selected=("test_review_candidate.py",), tests_executed=("test_review_candidate.py",),
            passed_count=1, failed_count=0,
            regressions=(), requires_coding_agent=False, failure_classifications=(),
        )


@dataclass(frozen=True)
class PhaseTwoResult:
    work_item_status: WorkItemStatus
    execution_a_status: ExecutionStatus
    execution_b_id: str
    execution_b_status: ExecutionStatus
    decision_b_worker_id: str
    decision_b_profile_id: str
    decision_b_quality_tier: QualityTier
    decision_b_model: str
    decision_b_reasoning_effort: str | None
    recommendation_b_id: str
    handoff_ids: list[str]
    dev_b_instructions: str
    git_sha_final: str
    qa_passed: bool
    workspace: Path


def _run_phase_two(facts: PhaseOneFacts) -> PhaseTwoResult:
    # Brand-new instances only — a real cold restart, reconstructed
    # purely from the sqlite files and workspace Phase 1 left behind.
    project_store = ProjectStateStore(facts.project_db, clock=lambda: UTC_T2)
    execution_store = ExecutionStore(facts.execution_db, clock=lambda: UTC_T2)
    handoff_store = HandoffStore(facts.handoff_db, clock=lambda: UTC_T2)
    recommendation_store = ExecutionRecommendationStore(facts.recommendation_db, clock=lambda: UTC_T2)
    decision_store = AdaptiveExecutionDecisionStore(facts.decision_db, clock=lambda: UTC_T2)
    qa_run_store = QARunStore(facts.tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_T2)

    worker_a, worker_b, worker_c = _worker_a(), _worker_b(), _worker_c()
    # Worker A's provider is now out of quota — a realistic reason its
    # process could have been killed in the first place. Never a fake
    # "no capable profile" situation: Worker B genuinely has one. Worker
    # C shares Worker B's own provider (real product worker-pool shape)
    # and exists only so WorkItem Flow's own DEV B has a genuinely
    # independent developer available once Worker B is excluded as
    # author for that role.
    quota_manager = QuotaManager(
        {PROVIDER_A: _FakeAdapter(available=False), PROVIDER_B: _FakeAdapter(available=True)},
        QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_T2,
    )
    worker_selector = WorkerSelector([worker_a, worker_b, worker_c], quota_manager)

    def _mutate_fix(cwd: Path) -> None:
        (cwd / "review_candidate.py").write_text(FIXED_ADD_CONTENT)
        _git_commit_all(cwd, "Worker B: fix add() to actually add")

    runner = _ScriptedFileMutatingRunner([
        {"topic": "execution.profile_recommended", "payload": _STANDARD_TIER_PAYLOAD},
        {"topic": "work.completed", "payload": "done", "mutate": _mutate_fix},
        # WorkItem Flow's own DEV B corrective review (Worker C) —
        # the fix Worker B already committed is genuinely fine, so DEV B
        # makes no further change.
        {"topic": "work.completed", "payload": "done"},
    ])
    execution_engine = RalphExecutionEngine(execution_store, subprocess_runner=runner, clock=lambda: UTC_T2)
    recommendation_service = ExecutionRecommendationService(
        recommendation_store, worker_selector, execution_engine, clock=lambda: UTC_T2, id_factory=lambda: "rec-b",
    )
    adaptive_selector = AdaptiveExecutionSelector(
        decision_store, recommendation_service, worker_selector, clock=lambda: UTC_T2, id_factory=lambda: "decision-b",
    )

    id_counter = {"n": 0}

    def _mgr_id_factory() -> str:
        id_counter["n"] += 1
        return f"mgr-e2e-{id_counter['n']}"

    manager = MVPManager(
        project_store, handoff_store, worker_selector, execution_engine,
        execution_store=execution_store, adaptive_execution_selector=adaptive_selector,
        qa_engine=_AlwaysPassQAEngine(), qa_policy=QAPolicy(required_invariant_ids=("e2e-qa-check",)), qa_run_store=qa_run_store,
        clock=lambda: UTC_T2, id_factory=_mgr_id_factory,
    )

    result = asyncio.run(manager.run_next_work_item(MVP_ID))
    assert result is not None

    # The second scripted RalphExecutionEngine call in this phase is
    # Worker B's (resumed) development attempt (the first is the
    # estimator's, the third is DEV B/Worker C's corrective review).
    dev_b_instructions = runner.calls[1][3]

    decisions = decision_store.list_for_work_item(WORK_ITEM_ID)
    decision_b = next(d for d in decisions if d.decision_id != facts.decision_id_a)

    # Worker B's own execution record — never read off the final
    # handoff, which now belongs to the QA phase (QA is not a Ralph
    # execution and has no ExecutionRecord of its own).
    execution_b = next(
        (e for e in execution_store.list_for_task(WORK_ITEM_ID) if e.worker_id == WORKER_B_ID), None,
    )
    git_sha_final = _run_git(facts.workspace, "rev-parse", "HEAD")

    outcome = PhaseTwoResult(
        work_item_status=project_store.get_work_item(WORK_ITEM_ID).status,
        execution_a_status=execution_store.get(facts.execution_id_a).status,
        execution_b_id=execution_b.execution_id if execution_b else "",
        execution_b_status=execution_b.status if execution_b else ExecutionStatus.FAILED,
        decision_b_worker_id=decision_b.worker_id, decision_b_profile_id=decision_b.profile_id,
        decision_b_quality_tier=decision_b.quality_tier, decision_b_model=decision_b.model,
        decision_b_reasoning_effort=decision_b.reasoning_effort, recommendation_b_id=decision_b.recommendation_id,
        handoff_ids=[h.handoff_id for h in handoff_store.list_for_work_item(WORK_ITEM_ID)],
        dev_b_instructions=dev_b_instructions,
        git_sha_final=git_sha_final,
        qa_passed=project_store.get_work_item(WORK_ITEM_ID).status is WorkItemStatus.COMPLETED,
        workspace=facts.workspace,
    )

    project_store.close()
    execution_store.close()
    handoff_store.close()
    recommendation_store.close()
    decision_store.close()
    qa_run_store.close()

    return outcome


class TestCrossWorkerColdResumeE2E:
    def test_worker_a_interrupted_worker_b_completes_after_cold_restart(self, tmp_path: Path) -> None:
        facts = _run_phase_one(tmp_path)
        result = _run_phase_two(facts)

        # --- worker identity: never the same worker ---------------------
        assert result.decision_b_worker_id == WORKER_B_ID
        assert result.decision_b_worker_id != WORKER_A_ID

        # --- execution identity: never reused, never blindly relaunched -
        assert result.execution_b_id != facts.execution_id_a
        assert result.execution_a_status is ExecutionStatus.RECOVERY_REQUIRED
        assert result.execution_b_status is ExecutionStatus.SUCCEEDED

        # --- no-downgrade: Worker B has a cheaper SIMPLE profile (cost_rank
        # 10 < 20) but the recommendation asked for STANDARD -> never chosen.
        assert result.decision_b_quality_tier is QualityTier.STANDARD
        assert result.decision_b_profile_id == "standard"
        assert result.decision_b_model == "model-b-standard"

        # --- handoff durable, readable after restart ---------------------
        assert len(result.handoff_ids) >= 1  # at least the recovery handoff

        # --- a fresh recommendation was produced on resume (the recovery
        # handoff is a new fact the fingerprint didn't have in Phase 1) —
        # never a cache hit silently reusing Worker A's own recommendation.
        assert result.recommendation_b_id != facts.recommendation_id_a

        # --- Worker B's prompt is grounded in the persisted handoff, never
        # in anything Worker A "remembers" ----------------------------------
        assert "previously attempted" in result.dev_b_instructions
        assert "recovery required" in result.dev_b_instructions.lower()

        # --- final state on disk: real code, real green tests -----------
        assert (result.workspace / "review_candidate.py").read_text() == FIXED_ADD_CONTENT
        assert (result.workspace / "test_review_candidate.py").read_text() == TEST_FILE_CONTENT
        assert result.qa_passed is True

        final = subprocess.run(
            [sys.executable, "test_review_candidate.py"], cwd=str(result.workspace),
            capture_output=True, text=True, timeout=10,
        )
        assert final.returncode == 0
        assert "OK" in final.stdout

        # --- WorkItem reached a real terminal success ---------------------
        assert result.work_item_status is WorkItemStatus.COMPLETED

    def test_ralph_spike_original_is_never_modified(self, tmp_path: Path) -> None:
        before = subprocess.run(
            ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
        ).stdout
        _run_phase_two(_run_phase_one(tmp_path))
        after = subprocess.run(
            ["git", "status", "--short"], cwd=str(RALPH_SPIKE_SOURCE), capture_output=True, text=True,
        ).stdout
        assert before == after
