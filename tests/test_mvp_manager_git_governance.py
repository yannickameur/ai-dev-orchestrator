"""MVPManager <-> GitGovernanceService integration tests (Slice 20).

Unlike ``test_mvp_manager.py`` (fully offline, no real Git), this file
uses real, temporary git repositories as the WorkItem's ``project.workspace``
— GitGovernanceService itself performs real ``git`` subprocess calls
against them. The Ralph execution engine is still faked (no real
Claude/Codex/Ralph), but the fake developer execution performs a real git
commit into the workspace, exactly like a real worker/Ralph auto-commit
would, so branch/SHA bookkeeping is exercised for real end to end.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus, ExecutionStore
from orchestrator.git_governance import (
    GitGovernancePolicy,
    GitGovernanceService,
    GitWorkItemStatus,
    GitWorkItemStore,
    LocalGitWorkspace,
    work_branch_name,
)
from orchestrator.handoff import HandoffStore
from orchestrator.mvp_manager import MVPManager
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.ralph_execution_engine import ExecutionResult
from orchestrator.review import ReviewPolicy, ReviewStatus, ReviewStore
from orchestrator.wait import WaitStore
from orchestrator.worker_selector import NoEligibleWorkerError, ProviderSelectionDiagnostic, Worker, WorkerSelectionRequest

UTC_NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial commit")
    return repo


def _stores(tmp_path: Path, repo: Path):
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    handoff_store = HandoffStore(tmp_path / "handoff.sqlite3", clock=lambda: UTC_NOW)
    project_store.create_project(project_id="proj-1", name="Demo", workspace=repo)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")
    return project_store, handoff_store


def _git_service(tmp_path: Path, *, policy: GitGovernancePolicy | None = None) -> tuple[GitGovernanceService, GitWorkItemStore]:
    store = GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)
    service = GitGovernanceService(store, policy=policy, clock=lambda: UTC_NOW, id_factory=_counting_id_factory())
    return service, store


def _counting_id_factory():
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return id_factory


def _alice() -> Worker:
    return Worker.with_single_profile(
        worker_id="alice", display_name="Alice", provider="anthropic",
        backend="claude_code", model="sonnet", capabilities=frozenset({"developer", "code_review"}),
    )


def _victor() -> Worker:
    return Worker.with_single_profile(
        worker_id="victor", display_name="Victor", provider="openai",
        backend="codex", model="terra", capabilities=frozenset({"developer", "code_review"}),
    )


class FakeWorkerSelector:
    """Alice always develops, Victor always reviews — deterministic, no
    randomness, so branch/SHA assertions stay simple."""

    def __init__(self, *, dev: Worker, reviewer: Worker) -> None:
        self._dev = dev
        self._reviewer = reviewer
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        if request.author_worker_id is not None:
            return self._reviewer
        return self._dev


class ScriptedWorkerSelector:
    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class GitCommittingFakeEngine:
    """Simulates Ralph/a worker: a developer execution performs a REAL git
    commit in ``request.workspace``; a reviewer execution never touches
    git, and simply succeeds (approves) or fails (rejects) per script."""

    def __init__(self, *, review_outcomes: list[bool] | None = None) -> None:
        self._dev_call_count = 0
        self._review_outcomes = list(review_outcomes) if review_outcomes is not None else [True]
        self.requests: list = []

    async def execute(self, request) -> ExecutionResult:
        self.requests.append(request)
        if request.role == "developer":
            self._dev_call_count += 1
            sha_before = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
            file_name = f"change_{self._dev_call_count}.txt"
            (request.workspace / file_name).write_text(f"dev change {self._dev_call_count}\n")
            _run_git(request.workspace, "add", file_name)
            _run_git(request.workspace, "commit", "-m", f"dev commit {self._dev_call_count}")
            sha_after = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
            record = ExecutionRecord(
                execution_id=request.execution_id, task_id=request.task_id, worker_id=request.worker.worker_id,
                provider=request.worker.provider, backend=request.worker.backend, model=request.model,
                role=request.role, started_at=UTC_NOW, finished_at=UTC_NOW, status=ExecutionStatus.SUCCEEDED,
                git_sha_before=sha_before, git_sha_after=sha_after,
            )
            return ExecutionResult(record=record, exit_code=0)

        approved = self._review_outcomes.pop(0) if self._review_outcomes else True
        record = ExecutionRecord(
            execution_id=request.execution_id, task_id=request.task_id, worker_id=request.worker.worker_id,
            provider=request.worker.provider, backend=request.worker.backend, model=request.model,
            role=request.role, started_at=UTC_NOW, finished_at=UTC_NOW,
            status=ExecutionStatus.SUCCEEDED if approved else ExecutionStatus.FAILED,
        )
        return ExecutionResult(record=record, exit_code=0 if approved else 1)


def _manager(
    project_store, handoff_store, selector, engine, *, git_governance_service,
    review_store=None, wait_store=None, execution_store=None,
) -> MVPManager:
    return MVPManager(
        project_store, handoff_store, selector, engine,
        review_store=review_store, review_policy=ReviewPolicy() if review_store is not None else None,
        wait_store=wait_store, execution_store=execution_store,
        git_governance_service=git_governance_service,
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


# --- 45: first dev prepares branch ------------------------------------------


class TestFirstDevPreparesBranch:
    def test_first_development_prepares_a_governed_branch(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False, require_required_gates=False))
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(project_store, handoff_store, selector, engine, git_governance_service=service)

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.work_branch == work_branch_name("wi-a")
        assert record.current_head_sha is not None
        assert record.base_sha != record.current_head_sha


# --- 46: rework reuses the same branch --------------------------------------


class TestReworkReusesBranch:
    def test_rework_after_rejection_reuses_the_same_branch_and_gets_a_new_head(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_required_gates=False))
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine(review_outcomes=[False, True])  # rejected, then approved
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.NEEDS_REWORK
        head_after_first = git_store.get("wi-a").current_head_sha

        second = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert second.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.work_branch == work_branch_name("wi-a")  # never a second branch
        assert record.current_head_sha != head_after_first  # new commit, new evidence
        # The final review approved exactly the latest head, never the stale one.
        latest_review = review_store.latest_for_work_item("wi-a")
        assert latest_review.git_sha_reviewed == record.current_head_sha


# --- 47/48: wait resume / recovery resume reuse the branch ------------------


class TestWaitResumeReusesBranch:
    def test_wait_resume_reuses_the_same_governed_branch(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False, require_required_gates=False))
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=2)
        error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset()),
            diagnostics=(
                ProviderSelectionDiagnostic(
                    provider="anthropic", available=False, reason="quota_exhausted", reset_at=(reset_at,),
                ),
            ),
        )
        alice = _alice()
        selector = ScriptedWorkerSelector([error, alice])
        engine = GitCommittingFakeEngine()
        manager = MVPManager(
            project_store, handoff_store, selector, engine, wait_store=wait_store,
            git_governance_service=service, clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.WAITING
        assert git_store.try_get("wi-a") is None  # no worker was ever selected — nothing to prepare yet

        clock_box["now"] = reset_at + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.work_branch == work_branch_name("wi-a")


class TestRecoveryResumeReusesBranch:
    def test_recovery_resume_reuses_the_branch_prepared_before_the_crash(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False, require_required_gates=False))

        # Simulate: a prior (now-crashed) process already prepared the
        # governed branch before it was ever interrupted.
        prepared = service.prepare_work_item(
            project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a", repository_path=repo,
        )

        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        execution_store = ExecutionStore(tmp_path / "executions.sqlite3", clock=lambda: UTC_NOW)
        execution_store.create(
            execution_id="exec-old", task_id="wi-a", worker_id="alice",
            provider="anthropic", backend="claude_code", model="sonnet", role="developer",
        )  # orphaned by the simulated crash

        alice = _alice()
        selector = FakeWorkerSelector(dev=alice, reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, execution_store=execution_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.work_branch == prepared.work_branch  # never a second branch
        assert record.base_sha == prepared.base_sha  # never re-captured


# --- 49/50: review head binding ---------------------------------------------


class TestReviewUsesExactHead:
    def test_review_targets_the_exact_dev_commit(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_required_gates=False))
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        record = git_store.get("wi-a")
        review = review_store.latest_for_work_item("wi-a")
        assert review.git_sha_reviewed == record.current_head_sha
        assert review.status is ReviewStatus.APPROVED


class TestNewCommitInvalidatesOldEvidence:
    def test_rework_produces_fresh_evidence_never_the_stale_review(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        policy = GitGovernancePolicy(require_required_gates=False, auto_merge=False)
        service, git_store = _git_service(tmp_path, policy=policy)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine(review_outcomes=[False, True])
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))  # dev1 -> review rejected
        result = asyncio.run(manager.run_next_work_item("mvp-1"))  # dev2 (rework) -> review approved

        assert result.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.status is GitWorkItemStatus.MERGE_READY
        latest_review = review_store.latest_for_work_item("wi-a")
        assert latest_review.git_sha_reviewed == record.current_head_sha

        # Directly prove the OLD (rejected) review's SHA would never have
        # sufficed for the current head, even if it had been "approved".
        reviews = [r for r in review_store.list_for_work_item("wi-a")]
        stale_sha = reviews[0].git_sha_reviewed
        assert stale_sha != record.current_head_sha
        eligibility = service.compute_merge_eligibility(
            "wi-a", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=record.current_head_sha,
            review_approved=True, review_git_sha=stale_sha,
        )
        assert eligibility.mergeable is False


# --- 51/52/53: merge readiness / auto_merge -----------------------------------


class TestMergeReadyOnlyForCurrentSha:
    def test_merge_ready_exactly_for_the_current_head(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        policy = GitGovernancePolicy(require_required_gates=False, auto_merge=False)
        service, git_store = _git_service(tmp_path, policy=policy)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        record = git_store.get("wi-a")
        assert record.status is GitWorkItemStatus.MERGE_READY


class TestAutoMergeFalseLeavesBaseUntouched:
    def test_auto_merge_false_never_advances_main(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        base_before = LocalGitWorkspace(repo).head_sha("main")
        policy = GitGovernancePolicy(require_required_gates=False, auto_merge=False)
        service, git_store = _git_service(tmp_path, policy=policy)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))

        assert LocalGitWorkspace(repo).head_sha("main") == base_before
        assert git_store.get("wi-a").status is GitWorkItemStatus.MERGE_READY  # not MERGED


class TestAutoMergeTrueMergesInTempRepo:
    def test_auto_merge_true_merges_into_main_on_a_temp_repo(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        policy = GitGovernancePolicy(require_required_gates=False, auto_merge=True)
        service, git_store = _git_service(tmp_path, policy=policy)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.status is GitWorkItemStatus.MERGED
        assert LocalGitWorkspace(repo).head_sha("main") == record.current_head_sha == record.merged_sha


class TestLocalGovernedSmokeE2E:
    """The required 'no extra LLM provider' local smoke: main -> WorkItem ->
    prepare branch -> real worker commit -> gate PASS -> review APPROVED ->
    merge eligibility PASS -> auto_merge=True -> ff-only merge -> main
    advances exactly to the work head -> fresh store instances (simulated
    restart) still observe MERGED."""

    def test_full_governed_lifecycle_survives_a_simulated_restart(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        policy = GitGovernancePolicy(require_required_gates=False, auto_merge=True)
        service, git_store = _git_service(tmp_path, policy=policy)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        selector = FakeWorkerSelector(dev=_alice(), reviewer=_victor())
        engine = GitCommittingFakeEngine()
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=service, review_store=review_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert result.work_item.status is WorkItemStatus.COMPLETED
        merged_sha = git_store.get("wi-a").merged_sha
        assert merged_sha is not None
        assert LocalGitWorkspace(repo).head_sha("main") == merged_sha

        # Simulated restart: brand new store/service handles reading the
        # same sqlite files + the same real repository.
        restarted_store = GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)
        restarted_service = GitGovernanceService(restarted_store, policy=policy, clock=lambda: UTC_NOW)
        reconciled = restarted_service.reconcile("wi-a", repository_path=repo)
        assert reconciled.status is GitWorkItemStatus.MERGED
        assert reconciled.merged_sha == merged_sha
