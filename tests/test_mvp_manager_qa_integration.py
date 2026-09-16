"""MVPManager <-> QA governance integration tests (Slice 24).

Like ``test_mvp_manager_git_governance.py`` (Slice 20), this file uses
real, temporary git repositories as the WorkItem's ``project.workspace``.
Development/review/QA-authoring worker selection reuses the exact same
adaptive mechanism already proven in ``test_internal_qa_engine.py``
(``AdaptiveExecutionSelector`` + a real ``WorkerSelector`` + a fake
``ExecutionRecommendationService``) — never a hand-rolled selector.

``QAEngine`` itself is a small, scriptable fake (``ScriptedQAEngine``,
satisfying only the ``QAEngine`` Protocol — no ``InternalQAEngine``
anywhere in this file's fixtures) so PASS/FAIL/INCONCLUSIVE outcomes are
fully deterministic without a real pytest subprocess — this file tests
MVPManager's own QA *orchestration* (wiring, state transitions,
SHA-binding, bounded cycles, wait/recovery), which is Slice 24's actual
scope; ``InternalQAEngine``'s own internal test-selection logic is already
exhaustively covered by ``test_internal_qa_engine.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.adaptive_execution import AdaptiveExecutionDecisionStore, AdaptiveExecutionSelector
from orchestrator.complexity_estimation import ComplexityEstimationRequest, ExecutionRecommendation
from orchestrator.execution_store import ExecutionRecord, ExecutionStatus, ExecutionStore
from orchestrator.git_governance import (
    GitGovernancePolicy,
    GitGovernanceService,
    GitWorkItemStatus,
    GitWorkItemStore,
    LocalGitWorkspace,
)
from orchestrator.handoff import HandoffStore
from orchestrator.internal_qa_engine import QA_TESTING_CAPABILITY, QA_TESTING_ROLE
from orchestrator.mvp_manager import MVPManager
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState
from orchestrator.qa import (
    FailureClassification,
    QAPolicy,
    QARequest,
    QARunStore,
    QAResult,
    QAVerdictStatus,
)
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.ralph_execution_engine import ExecutionResult
from orchestrator.release_manager import ReleaseManager
from orchestrator.review import ReviewPolicy, ReviewStatus, ReviewStore
from orchestrator.wait import WaitStore
from orchestrator.worker_selector import ExecutionProfile, QualityTier, Worker

UTC_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


# --- git repo helpers (mirrors test_mvp_manager_git_governance.py) ---------


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


def _worker(worker_id: str, *, provider: str = "anthropic", backend: str = "claude_code") -> Worker:
    return Worker(
        worker_id=worker_id, display_name=worker_id.title(), provider=provider, backend=backend,
        capabilities=frozenset({"developer", "code_review", QA_TESTING_CAPABILITY}),
        profiles=(ExecutionProfile(profile_id="economy", quality_tier=QualityTier.SIMPLE, model="m", cost_rank=10),),
        default_profile_id="economy",
    )


class _AlwaysAvailableAdapter(ProviderAdapter):
    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def probe(self) -> ProviderState:
        return ProviderState(
            provider=self._provider,
            availability=ProviderAvailability(available=True, observed_at=UTC_NOW, reason=None),
            observed_at=UTC_NOW,
        )


def _real_worker_selector(workers: list[Worker]):
    from orchestrator.worker_selector import WorkerSelector

    manager = QuotaManager(
        {"anthropic": _AlwaysAvailableAdapter("anthropic"), "openai": _AlwaysAvailableAdapter("openai")},
        QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW,
    )
    return WorkerSelector(workers, manager)


class _FakeRecommendationService:
    async def estimate(self, request: ComplexityEstimationRequest, *, force_refresh: bool = False):
        return ExecutionRecommendation(
            recommendation_id=f"rec-{request.role}", project_id=request.project_id, role=request.role,
            estimator_worker_id="alice", estimator_execution_id="exec-est", estimator_profile_id="economy",
            task_fingerprint=f"fp-{request.role}-{request.git_sha}", minimum_quality_tier=QualityTier.SIMPLE,
            reasons=("small change",), created_at=UTC_NOW,
        )


def _adaptive_selector(tmp_path: Path, workers: list[Worker], suffix: str = "") -> AdaptiveExecutionSelector:
    store = AdaptiveExecutionDecisionStore(tmp_path / f"decisions{suffix}.sqlite3", clock=lambda: UTC_NOW)
    return AdaptiveExecutionSelector(
        store, _FakeRecommendationService(), _real_worker_selector(workers),
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


class GitCommittingFakeEngine:
    """Simulates Ralph/a worker for development, review, AND QA authoring
    (Slice 24): a developer execution performs a REAL git commit; a QA
    authoring execution optionally adds a real test file + commit (per
    script) and emits the required structured event; a reviewer execution
    never touches git."""

    def __init__(
        self, *, review_outcomes: list[bool] | None = None,
        qa_authoring_adds_test: list[bool] | None = None,
    ) -> None:
        self._dev_call_count = 0
        self._review_outcomes = list(review_outcomes) if review_outcomes is not None else [True]
        self._qa_authoring_adds_test = list(qa_authoring_adds_test) if qa_authoring_adds_test is not None else [True]
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

        if request.role == QA_TESTING_ROLE:
            import json

            from orchestrator.ralph_execution_engine import RalphEvent

            adds_test = self._qa_authoring_adds_test.pop(0) if self._qa_authoring_adds_test else False
            sha_before = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
            tests_added: list[str] = []
            if adds_test:
                test_file = "test_qa_added.py"
                (request.workspace / test_file).write_text("def test_qa_added():\n    assert True\n")
                _run_git(request.workspace, "add", test_file)
                _run_git(request.workspace, "commit", "-m", "qa: add regression test")
                tests_added = [test_file]
            sha_after = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
            payload = json.dumps({
                "tests_added": tests_added, "tests_modified": [], "fixtures_added": [], "fixtures_modified": [],
                "production_files_modified": [], "findings": [], "risks": [], "recommended_actions": [],
            })
            record = ExecutionRecord(
                execution_id=request.execution_id, task_id=request.task_id, worker_id=request.worker.worker_id,
                provider=request.worker.provider, backend=request.worker.backend, model=request.model,
                role=request.role, started_at=UTC_NOW, finished_at=UTC_NOW, status=ExecutionStatus.SUCCEEDED,
                git_sha_before=sha_before, git_sha_after=sha_after,
            )
            event = RalphEvent(topic="qa.authoring.completed", timestamp=UTC_NOW, payload=payload)
            return ExecutionResult(record=record, exit_code=0, events=(event,))

        approved = self._review_outcomes.pop(0) if self._review_outcomes else True
        record = ExecutionRecord(
            execution_id=request.execution_id, task_id=request.task_id, worker_id=request.worker.worker_id,
            provider=request.worker.provider, backend=request.worker.backend, model=request.model,
            role=request.role, started_at=UTC_NOW, finished_at=UTC_NOW,
            status=ExecutionStatus.SUCCEEDED if approved else ExecutionStatus.FAILED,
        )
        return ExecutionResult(record=record, exit_code=0 if approved else 1)


class ScriptedQAEngine:
    """A minimal, scriptable fake satisfying only ``QAEngine``'s Protocol
    (``.run(request) -> QAResult``) — deliberately NOT ``InternalQAEngine``,
    proving MVPManager's QA integration depends on nothing but the
    Protocol (test #48/#49's concrete evidence, alongside
    ``TestNoProviderCoupling`` below)."""

    engine_id = "fake-qa"

    def __init__(self, results: list[QAResult]) -> None:
        self._results = list(results)
        self.requests: list[QARequest] = []

    def run(self, request: QARequest) -> QAResult:
        self.requests.append(request)
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def _qa_result(
    *, head_sha: str, requires_coding_agent: bool = False, regressions: tuple = (),
    read_only_violation: bool = False, read_only_unprovable: bool = False,
) -> QAResult:
    return QAResult(
        engine_id="fake-qa", observed_head_sha=head_sha, started_at=UTC_NOW, finished_at=UTC_NOW,
        tests_selected=("tests/test_x.py",), tests_executed=("tests/test_x.py",),
        passed_count=0 if regressions else 1, failed_count=len(regressions),
        regressions=regressions, requires_coding_agent=requires_coding_agent,
        failure_classifications=(FailureClassification.REGRESSION,) if regressions else (),
        read_only_violation=read_only_violation, read_only_unprovable=read_only_unprovable,
    )


_QA_POLICY = QAPolicy(required_invariant_ids=("qa-integration-check",))


def _manager(
    project_store, handoff_store, adaptive_selector, workers, engine, *, git_governance_service,
    review_store=None, wait_store=None, execution_store=None, qa_engine=None, qa_run_store=None,
    qa_policy=None, qa_protected_paths=(),
) -> MVPManager:
    return MVPManager(
        project_store, handoff_store, _real_worker_selector(workers), engine,
        review_store=review_store, review_policy=ReviewPolicy() if review_store is not None else None,
        wait_store=wait_store, execution_store=execution_store,
        adaptive_execution_selector=adaptive_selector,
        git_governance_service=git_governance_service,
        qa_engine=qa_engine, qa_policy=qa_policy or _QA_POLICY, qa_run_store=qa_run_store,
        qa_protected_paths=qa_protected_paths,
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


# --- nominal flow ------------------------------------------------------------


class TestNominalQAFlow:
    def test_development_qa_gates_review_final_qa_merge(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine()
        git_service, git_store = _git_service(
            tmp_path, policy=GitGovernancePolicy(auto_merge=True, require_required_gates=False),
        )
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
        qa_engine = ScriptedQAEngine([])  # populated below once we know the SHA sequence lazily

        # ScriptedQAEngine must answer with the *actual* observed head SHA
        # of each request — use a callable-backed engine instead of a
        # fixed list, since the exact commit SHAs aren't known upfront.
        class DynamicQAEngine:
            engine_id = "fake-qa"

            def __init__(self) -> None:
                self.requests: list[QARequest] = []

            def run(self, request: QARequest) -> QAResult:
                self.requests.append(request)
                return _qa_result(head_sha=request.head_sha)

        qa_engine = DynamicQAEngine()

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, review_store=review_store,
            qa_engine=qa_engine, qa_run_store=qa_run_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        # 2 QA runs: TEST_AUTHORING + FINAL_VERIFICATION.
        assert len(qa_engine.requests) == 2
        record = git_store.get("wi-a")
        assert record.status is GitWorkItemStatus.MERGED
        assert record.merged_sha == record.current_head_sha
        # QA authoring's committed test file made it into the final head.
        assert (repo / "test_qa_added.py").is_file()

    def test_qa_authoring_skips_adding_test_when_not_needed(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False, False])
        git_service, git_store = _git_service(tmp_path)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        class DynamicQAEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, review_store=review_store,
            qa_engine=DynamicQAEngine(), qa_run_store=qa_run_store,
        )
        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert not (repo / "test_qa_added.py").is_file()


# --- QA authoring FAIL -> rework ---------------------------------------------


class TestQAAuthoringFailRework:
    def test_qa_authoring_fail_requires_coding_triggers_rework_on_new_sha(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False, False])
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        calls = {"n": 0}

        class FirstFailsEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                calls["n"] += 1
                if calls["n"] == 1:
                    return _qa_result(head_sha=request.head_sha, requires_coding_agent=True, regressions=("t1 FAILED",))
                return _qa_result(head_sha=request.head_sha)

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, qa_engine=FirstFailsEngine(), qa_run_store=qa_run_store,
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.NEEDS_REWORK
        head_after_first = git_store.get("wi-a").current_head_sha

        second = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert second.work_item.status is WorkItemStatus.COMPLETED
        record = git_store.get("wi-a")
        assert record.current_head_sha != head_after_first  # rework produced new evidence

        # Old QA evidence (from the first, failing cycle) never counted for
        # the new head — the final QAVerdict.PASS is bound to the new SHA.
        runs = qa_run_store.list_for_work_item("wi-a")
        assert any(r.verdict.status is QAVerdictStatus.FAIL for r in runs)
        final_runs = [r for r in runs if r.verdict is not None and r.verdict.status is QAVerdictStatus.PASS]
        assert final_runs
        assert all(r.expected_head_sha == record.current_head_sha for r in final_runs)

    def test_bounded_qa_cycles_blocks_after_max(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False] * 5)
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        class AlwaysFailsEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha, requires_coding_agent=True, regressions=("t1 FAILED",))

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, qa_engine=AlwaysFailsEngine(), qa_run_store=qa_run_store,
            qa_policy=QAPolicy(required_invariant_ids=("x",), max_qa_cycles=2),
        )

        for _ in range(3):
            result = asyncio.run(manager.run_next_work_item("mvp-1"))
            if result.work_item.status is WorkItemStatus.BLOCKED:
                break
        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert "max_qa_cycles" in (result.work_item.blocked_reason or "")


# --- final QA FAIL loop -------------------------------------------------------


class TestFinalQAFailLoop:
    def test_final_qa_fail_after_review_approved_triggers_rework_and_new_review(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False, False])
        git_service, git_store = _git_service(tmp_path)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        calls = {"n": 0}

        class FinalFailsOnceEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                calls["n"] += 1
                if request.phase is not None and request.phase.value == "final_verification" and calls["n"] <= 2:
                    return _qa_result(head_sha=request.head_sha, requires_coding_agent=True, regressions=("late-caught FAILED",))
                return _qa_result(head_sha=request.head_sha)

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, review_store=review_store,
            qa_engine=FinalFailsOnceEngine(), qa_run_store=qa_run_store,
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.NEEDS_REWORK
        # The review that had approved the pre-final-QA-FAIL head is stale
        # by construction (SHA-binding) — a brand new review is required.
        approved_reviews_before = [r for r in review_store.list_for_work_item("wi-a") if r.status is ReviewStatus.APPROVED]

        second = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert second.work_item.status is WorkItemStatus.COMPLETED
        approved_reviews_after = [r for r in review_store.list_for_work_item("wi-a") if r.status is ReviewStatus.APPROVED]
        assert len(approved_reviews_after) > len(approved_reviews_before)
        latest_review = review_store.latest_for_work_item("wi-a")
        assert latest_review.git_sha_reviewed == git_store.get("wi-a").current_head_sha


# --- external pilot finding (Invariant 3): Final QA Verification must never
# run a single command against a workspace that doesn't functionally match
# `head_sha` — the existing read-only guarantee (`verify_repository_unchanged`)
# only ever proves nothing changed *during* the call, never that it started
# from the right place. A real review execution (found via
# scripts/run_external_project_pilot.py against a genuine external repo)
# left real, uncommitted source/test changes behind; Final QA then silently
# tested that alien state and reported PASS bound to a SHA it didn't
# actually reflect. Exercises ``MVPManager._run_final_qa_verification``
# directly — a meaningful decision point in its own right, same pattern as
# ``_reconcile_governed_head``'s direct tests elsewhere in this suite.


class TestFinalQAWorkspacePrecondition:
    def _base(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        service, git_store = _git_service(tmp_path)
        project = project_store.get_project("proj-1")
        work_item = project_store.get_work_item("wi-a")
        return project_store, handoff_store, service, project, work_item

    def _manager_for(self, tmp_path: Path, project_store, handoff_store, service, qa_engine) -> MVPManager:
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
        return MVPManager(
            project_store, handoff_store, _real_worker_selector([]), object(),
            git_governance_service=service, qa_engine=qa_engine, qa_run_store=qa_run_store,
            qa_policy=_QA_POLICY, clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
        )

    def test_clean_workspace_lets_final_qa_run(self, tmp_path: Path) -> None:
        project_store, handoff_store, service, project, work_item = self._base(tmp_path)
        head = _run_git(project.workspace, "rev-parse", "HEAD").stdout.strip()
        qa_engine = ScriptedQAEngine([_qa_result(head_sha=head)])
        manager = self._manager_for(tmp_path, project_store, handoff_store, service, qa_engine)

        updated, next_action, qa_passed = asyncio.run(manager._run_final_qa_verification(
            project=project, mvp_id="mvp-1", work_item=work_item, base_sha=head, head_sha=head,
        ))

        assert qa_passed is True
        assert len(qa_engine.requests) == 1

    def test_uncommitted_tracked_source_change_blocks_before_pytest(self, tmp_path: Path) -> None:
        project_store, handoff_store, service, project, work_item = self._base(tmp_path)
        head = _run_git(project.workspace, "rev-parse", "HEAD").stdout.strip()
        (project.workspace / "README.md").write_text("uncommitted, tracked change\n")
        qa_engine = ScriptedQAEngine([])  # must never be invoked
        manager = self._manager_for(tmp_path, project_store, handoff_store, service, qa_engine)

        updated, next_action, qa_passed = asyncio.run(manager._run_final_qa_verification(
            project=project, mvp_id="mvp-1", work_item=work_item, base_sha=head, head_sha=head,
        ))

        assert qa_passed is False
        assert updated.status is WorkItemStatus.BLOCKED
        assert qa_engine.requests == []

    def test_uncommitted_untracked_test_file_blocks_before_pytest(self, tmp_path: Path) -> None:
        project_store, handoff_store, service, project, work_item = self._base(tmp_path)
        head = _run_git(project.workspace, "rev-parse", "HEAD").stdout.strip()
        (project.workspace / "tests").mkdir()
        (project.workspace / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")
        qa_engine = ScriptedQAEngine([])  # must never be invoked
        manager = self._manager_for(tmp_path, project_store, handoff_store, service, qa_engine)

        updated, next_action, qa_passed = asyncio.run(manager._run_final_qa_verification(
            project=project, mvp_id="mvp-1", work_item=work_item, base_sha=head, head_sha=head,
        ))

        assert qa_passed is False
        assert updated.status is WorkItemStatus.BLOCKED
        assert qa_engine.requests == []

    def test_ralph_noise_only_still_lets_final_qa_run(self, tmp_path: Path) -> None:
        project_store, handoff_store, service, project, work_item = self._base(tmp_path)
        head = _run_git(project.workspace, "rev-parse", "HEAD").stdout.strip()
        (project.workspace / ".ralph").mkdir()
        (project.workspace / ".ralph" / "loop-state.json").write_text("{}")
        qa_engine = ScriptedQAEngine([_qa_result(head_sha=head)])
        manager = self._manager_for(tmp_path, project_store, handoff_store, service, qa_engine)

        updated, next_action, qa_passed = asyncio.run(manager._run_final_qa_verification(
            project=project, mvp_id="mvp-1", work_item=work_item, base_sha=head, head_sha=head,
        ))

        assert qa_passed is True
        assert len(qa_engine.requests) == 1


# --- SHA binding / merge eligibility -----------------------------------------


class TestQAMergeEligibility(object):
    def test_qa_pass_on_stale_sha_never_authorizes_merge(self, tmp_path: Path) -> None:
        from orchestrator.git_governance import MergeEligibilityResult

        store = GitWorkItemStore(tmp_path / "git.sqlite3", clock=lambda: UTC_NOW)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _git_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        LocalGitWorkspace(repo).switch("work/wi-1")
        (repo / "a.txt").write_text("a")
        _run_git(repo, "add", "a.txt")
        _run_git(repo, "commit", "-m", "dev commit")
        head = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
        service.capture_head("wi-1", repository_path=repo)

        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
            qa_required=True, qa_passed=True, qa_git_sha="stale-sha-0000", qa_run_terminal=True,
        )
        assert result.mergeable is False
        assert "QA evidence is for an old SHA" in result.reason

    def test_qa_pass_on_exact_sha_is_eligible(self, tmp_path: Path) -> None:
        store = GitWorkItemStore(tmp_path / "git.sqlite3", clock=lambda: UTC_NOW)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _git_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        LocalGitWorkspace(repo).switch("work/wi-1")
        (repo / "a.txt").write_text("a")
        _run_git(repo, "add", "a.txt")
        _run_git(repo, "commit", "-m", "dev commit")
        head = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
        service.capture_head("wi-1", repository_path=repo)

        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
            qa_required=True, qa_passed=True, qa_git_sha=head, qa_run_terminal=True,
        )
        assert result.mergeable is True

    def test_qa_not_required_preserves_pre_slice24_behavior(self, tmp_path: Path) -> None:
        store = GitWorkItemStore(tmp_path / "git.sqlite3", clock=lambda: UTC_NOW)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _git_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        LocalGitWorkspace(repo).switch("work/wi-1")
        (repo / "a.txt").write_text("a")
        _run_git(repo, "add", "a.txt")
        _run_git(repo, "commit", "-m", "dev commit")
        head = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
        service.capture_head("wi-1", repository_path=repo)

        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )
        assert result.mergeable is True  # no qa_required kwarg passed at all — untouched behavior


# --- protected test protection -----------------------------------------------


class TestProtectedTestIntegration:
    def test_unauthorized_protected_change_blocks_the_whole_workflow(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        (repo / "protected_test.py").write_text("def test_protected():\n    assert 1 == 1\n")
        _run_git(repo, "add", "protected_test.py")
        _run_git(repo, "commit", "-m", "seed protected test")

        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])

        class MutatesProtectedTestEngine(GitCommittingFakeEngine):
            async def execute(self, request):
                if request.role == QA_TESTING_ROLE:
                    sha_before = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
                    (request.workspace / "protected_test.py").write_text("def test_protected():\n    assert 1 == 2\n")
                    _run_git(request.workspace, "add", "protected_test.py")
                    _run_git(request.workspace, "commit", "-m", "qa: silently weaken protected test")
                    sha_after = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
                    import json

                    from orchestrator.ralph_execution_engine import RalphEvent
                    record = ExecutionRecord(
                        execution_id=request.execution_id, task_id=request.task_id,
                        worker_id=request.worker.worker_id, provider=request.worker.provider,
                        backend=request.worker.backend, model=request.model, role=request.role,
                        started_at=UTC_NOW, finished_at=UTC_NOW, status=ExecutionStatus.SUCCEEDED,
                        git_sha_before=sha_before, git_sha_after=sha_after,
                    )
                    payload = json.dumps({
                        "tests_added": [], "tests_modified": ["protected_test.py"], "fixtures_added": [],
                        "fixtures_modified": [], "production_files_modified": [], "findings": [], "risks": [],
                        "recommended_actions": [],
                    })
                    return ExecutionResult(
                        record=record, exit_code=0,
                        events=(RalphEvent(topic="qa.authoring.completed", timestamp=UTC_NOW, payload=payload),),
                    )
                return await super().execute(request)

        engine = MutatesProtectedTestEngine()
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        class TrivialPassEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, qa_engine=TrivialPassEngine(), qa_run_store=qa_run_store,
            qa_protected_paths=("protected_test.py",),
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        # verify_authoring_git_facts already blocks a test *file* mutation
        # outright (protected_test.py matches the pytest-filename
        # allowance, so the AuthoringViolationError path does not trigger
        # here) — the protected-baseline unauthorized-change check is what
        # catches it instead, at the QA verdict level.
        assert result.work_item.status is WorkItemStatus.BLOCKED


# --- provider independence ---------------------------------------------------


class TestProviderIndependence:
    def test_two_structurally_different_fake_engines_traverse_the_same_integration(self, tmp_path: Path) -> None:
        class BareEngine:
            """No build_plan/build_manifest at all — the leanest possible Protocol implementer."""
            engine_id = "bare-fake"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        class RichEngine:
            """Shaped like a future InternalQAEngine-style engine (duck-typed
            build_plan/build_manifest present) — MVPManager must use them
            opportunistically without ever isinstance-checking anything."""
            engine_id = "rich-fake"

            def build_plan(self, request, *, phase=None):
                return {"phase": phase}

            def build_manifest(self, plan):
                from orchestrator.qa import QAEvidenceManifest
                return QAEvidenceManifest(required_invariant_ids=("x",))

            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        for qa_engine_cls in (BareEngine, RichEngine):
            (tmp_path / qa_engine_cls.__name__).mkdir()
            repo = _git_repo(tmp_path / qa_engine_cls.__name__)
            project_store, handoff_store = _stores(tmp_path / qa_engine_cls.__name__, repo)
            project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
            alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
            selector = _adaptive_selector(tmp_path / qa_engine_cls.__name__, [alice, victor])
            engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False, False])
            git_service, git_store = _git_service(tmp_path / qa_engine_cls.__name__, policy=GitGovernancePolicy(require_review=False))
            qa_run_store = QARunStore(tmp_path / qa_engine_cls.__name__ / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

            manager = _manager(
                project_store, handoff_store, selector, [alice, victor], engine,
                git_governance_service=git_service, qa_engine=qa_engine_cls(), qa_run_store=qa_run_store,
            )
            result = asyncio.run(manager.run_next_work_item("mvp-1"))
            assert result.work_item.status is WorkItemStatus.COMPLETED, qa_engine_cls.__name__

    def test_no_qaengine_isinstance_or_type_check_in_mvp_manager_source(self) -> None:
        # MVPManager legitimately imports InternalQATestAuthor (the
        # adaptive worker-selection/execution composition for QA
        # authoring — analogous to it already using RalphExecutionEngine/
        # WorkerSelector directly for dev/review, never a "vendor"
        # concern) and mentions "InternalQAEngine" in one explanatory
        # docstring — what must never exist is an isinstance/type check
        # against the QAEngine *implementation* itself.
        import inspect

        from orchestrator import mvp_manager as module

        source = inspect.getsource(module)
        assert "isinstance(self._qa_engine" not in source
        assert "isinstance(qa_engine" not in source
        assert "type(self._qa_engine)" not in source

    def test_no_vendor_name_in_mvp_manager_source(self) -> None:
        import inspect

        from orchestrator import mvp_manager as module

        source = inspect.getsource(module)
        for forbidden in ("TestSprite", "BrowserStack", "Momentic", "Diffblue"):
            assert forbidden not in source


# --- release check integration ------------------------------------------------


class TestQAReleaseCheckIntegration:
    def test_release_check_reflects_real_qa_run_store(self, tmp_path: Path) -> None:
        from orchestrator.activity_report import ActivityReportStore
        from orchestrator.execution_store import ExecutionStore
        from orchestrator.release import ReleaseGateStatus, ReleaseStore

        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False, False])
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        class TrivialPassEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, qa_engine=TrivialPassEngine(), qa_run_store=qa_run_store,
        )
        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert result.work_item.status is WorkItemStatus.COMPLETED

        execution_store = ExecutionStore(tmp_path / "exec.sqlite3", clock=lambda: UTC_NOW)
        validation_store = None
        from orchestrator.validation import ValidationStore
        validation_store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
        review_store = ReviewStore(tmp_path / "review-rel.sqlite3", clock=lambda: UTC_NOW)
        release_store = ReleaseStore(tmp_path / "release.sqlite3", clock=lambda: UTC_NOW)
        activity_store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)

        release_manager = ReleaseManager(
            project_store, execution_store, validation_store, review_store, handoff_store,
            release_store, activity_store, git_work_item_store=git_store, qa_run_store=qa_run_store,
            clock=lambda: UTC_NOW,
        )
        release = release_manager.attempt_release("proj-1", "mvp-1", review_required=False)
        qa_check = next(c for c in release.checks if c.check_id == "qa-verdict-pass")
        assert qa_check.passed is True


# --- wait / recovery -----------------------------------------------------------


class TestQAAuthoringWait:
    def test_qa_authoring_quota_wait_never_falls_back_to_developer(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice = _worker("alice")  # only ONE worker at all -> QA selection (author-excluded) fails
        selector = _adaptive_selector(tmp_path, [alice])
        engine = GitCommittingFakeEngine()
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])

        class NeverCalledEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                raise AssertionError("QAEngine must never run when worker selection is blocked")

        manager = _manager(
            project_store, handoff_store, selector, [alice], engine,
            git_governance_service=git_service, wait_store=wait_store,
            qa_engine=NeverCalledEngine(), qa_run_store=qa_run_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert result.work_item.status is WorkItemStatus.BLOCKED  # no quota diagnostic available -> BLOCKED, not a fake wait
        assert result.work_item.work_item_id == "wi-a"


class TestQAAuthoringRecovery:
    def test_orphaned_qa_authoring_execution_is_reconciled_and_resumed(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _adaptive_selector(tmp_path, [alice, victor])
        git_service, git_store = _git_service(tmp_path, policy=GitGovernancePolicy(require_review=False))
        execution_store = ExecutionStore(tmp_path / "exec.sqlite3", clock=lambda: UTC_NOW)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        # Simulate a completed development execution followed by an
        # orphaned QA_TESTING_ROLE execution left RUNNING (as if the
        # process crashed mid-QA-authoring).
        git_service.prepare_work_item(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a", repository_path=repo)
        LocalGitWorkspace(repo).switch("work/wi-a")
        (repo / "dev.txt").write_text("dev change\n")
        _run_git(repo, "add", "dev.txt")
        _run_git(repo, "commit", "-m", "dev commit")
        dev_sha = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
        git_service.capture_head("wi-a", repository_path=repo)

        dev_execution = execution_store.create(
            execution_id="exec-dev", task_id="wi-a", worker_id="alice", provider="anthropic",
            backend="claude_code", model="m", role="developer", started_at=UTC_NOW,
        )
        execution_store.mark_succeeded(
            "exec-dev", finished_at=UTC_NOW, exit_code=0, git_sha_after=dev_sha,
        )
        dev_handoff = handoff_store.create(
            handoff_id="h-dev", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a", objective="A",
            execution_id="exec-dev", worker_id="alice", git_sha_after=dev_sha,
            next_action="development succeeded — proceeding to QA test authoring", created_at=UTC_NOW,
        )
        qa_execution = execution_store.create(
            execution_id="exec-qa", task_id="wi-a", worker_id="victor", provider="openai",
            backend="codex", model="m", role=QA_TESTING_ROLE, started_at=UTC_NOW,
        )
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")

        class TrivialPassEngine:
            engine_id = "fake-qa"
            def run(self, request: QARequest) -> QAResult:
                return _qa_result(head_sha=request.head_sha)

        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[False])
        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, execution_store=execution_store,
            qa_engine=TrivialPassEngine(), qa_run_store=qa_run_store,
        )

        result = asyncio.run(manager.run_next_work_item("mvp-1"))
        # The orphaned QA execution itself stays RECOVERY_REQUIRED forever
        # (permanent audit history) — a brand new attempt resumed QA
        # authoring onward and completed the WorkItem.
        assert execution_store.get("exec-qa").status.value == "recovery_required"
        assert result.work_item.status is WorkItemStatus.COMPLETED


# --- external pilot finding: Ralph runtime artifacts must never enter the
# governed product's own git history (mars-rover run3). Real evidence: a
# real Ralph execution — including a supposedly read-only complexity-
# estimation execution, which runs on ``main`` itself *before*
# ``prepare_work_item`` ever creates a WorkItem's own branch — performs a
# real ``git add -A`` + ``chore: auto-commit before merge (loop primary)``
# commit of its OWN runtime bookkeeping (``.ralph/*``). WI-1's own
# estimation commits landed on ``main``; WI-1's real merge then carried
# those tracked ``.ralph/*`` files into ``main``'s own history; WI-2's own
# estimation then modified an already-TRACKED ``.ralph/agent/handoff.md``,
# which correctly (and deliberately, never weakened) tripped
# ``prepare_work_item``'s ``require_clean_worktree`` check — crashing
# ``run_next_work_item`` outright, since nothing ever caught it.
#
# The fix (``GitGovernanceService.ensure_runtime_exclusion``, installed at
# every pre-flight-estimation call site in ``MVPManager``) prevents the
# contamination upstream: ``.ralph/*`` never becomes trackable in the
# first place, so it can never survive a real merge onto ``main``, and a
# second WorkItem's own estimation noise on ``main`` can never dirty a
# tracked file. This test reproduces run3's exact end-to-end scenario with
# fakes only (no real provider), asserting the crash can no longer happen.


class RalphNoiseInjectingRecommendationService:
    """Real evidence, reproduced deterministically: every real Ralph
    execution role observed in the external pilot (including read-only
    ones) performed a real, uncoached ``git add -A`` + commit of its own
    ``.ralph/*`` runtime bookkeeping as a side effect — regardless of
    which WorkItem or phase triggered it. This fake reproduces exactly
    that side effect inside ``estimate()`` (the one call
    ``AdaptiveExecutionSelector.select()`` always makes, for every dev/
    review/QA-authoring pre-flight), then returns the same canned,
    always-SIMPLE recommendation ``_FakeRecommendationService`` does.

    If ``ensure_runtime_exclusion`` was never installed (or was installed
    too late / incorrectly scoped), this ``git add -A`` would stage
    ``.ralph/*`` and the following commit would make it TRACKED — exactly
    reproducing run3's contamination. If the fix works, ``git add -A``
    stages nothing here (nothing else changed), so no commit happens at
    all.
    """

    async def estimate(self, request: ComplexityEstimationRequest, *, force_refresh: bool = False):
        workspace = Path(request.workspace)
        ralph_dir = workspace / ".ralph" / "agent"
        ralph_dir.mkdir(parents=True, exist_ok=True)
        (ralph_dir / "handoff.md").write_text(f"noise for {request.work_item_id}/{request.role}\n")
        _run_git(workspace, "add", "-A")
        staged = _run_git(workspace, "diff", "--cached", "--name-only").stdout.strip()
        if staged:
            _run_git(workspace, "commit", "-m", "chore: auto-commit before merge (loop primary)")
        return ExecutionRecommendation(
            recommendation_id=f"rec-{request.role}-{request.work_item_id}", project_id=request.project_id,
            role=request.role, estimator_worker_id="alice", estimator_execution_id="exec-est",
            estimator_profile_id="economy",
            task_fingerprint=f"fp-{request.role}-{request.work_item_id}-{request.git_sha}",
            minimum_quality_tier=QualityTier.SIMPLE, reasons=("small change",), created_at=UTC_NOW,
        )


def _noise_injecting_adaptive_selector(tmp_path: Path, workers: list[Worker], suffix: str = "") -> AdaptiveExecutionSelector:
    store = AdaptiveExecutionDecisionStore(tmp_path / f"decisions{suffix}.sqlite3", clock=lambda: UTC_NOW)
    return AdaptiveExecutionSelector(
        store, RalphNoiseInjectingRecommendationService(), _real_worker_selector(workers),
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


class TestRuntimeArtifactsNeverEnterProductHistory:
    def test_wi1_merge_then_wi2_estimation_never_contaminates_main(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="Movement")
        project_store.create_work_item(work_item_id="wi-2", mvp_id="mvp-1", title="Obstacles", dependencies=("wi-1",))
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        selector = _noise_injecting_adaptive_selector(tmp_path, [alice, victor])
        # WI-2's QA authoring does not add a *second* test file: the fake
        # engine's test file name/content is fixed, and it already made it
        # into `main` via WI-1's merge — nothing to add a second time.
        engine = GitCommittingFakeEngine(qa_authoring_adds_test=[True, False])
        git_service, git_store = _git_service(
            tmp_path, policy=GitGovernancePolicy(auto_merge=True, require_required_gates=False),
        )
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)

        class DynamicQAEngine:
            engine_id = "fake-qa"

            def __init__(self) -> None:
                self.requests: list[QARequest] = []

            def run(self, request: QARequest) -> QAResult:
                self.requests.append(request)
                return _qa_result(head_sha=request.head_sha)

        qa_engine = DynamicQAEngine()
        manager = _manager(
            project_store, handoff_store, selector, [alice, victor], engine,
            git_governance_service=git_service, review_store=review_store,
            qa_engine=qa_engine, qa_run_store=qa_run_store,
        )

        # Pre-flight (run4-style check, before anything runs): nothing
        # tracked yet, workspace clean.
        assert _run_git(repo, "ls-files", "--", ".ralph").stdout == ""
        assert _run_git(repo, "status", "--porcelain").stdout == ""

        wi1_result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert wi1_result.work_item.status is WorkItemStatus.COMPLETED
        wi1_record = git_store.get("wi-1")
        assert wi1_record.status is GitWorkItemStatus.MERGED

        # --- the exact run3 checkpoint: immediately after WI-1's merge,
        # BEFORE WI-2's own estimation ever runs ---
        assert _run_git(repo, "status", "--porcelain").stdout == ""
        assert _run_git(repo, "ls-files", "--", ".ralph").stdout == ""
        assert LocalGitWorkspace(repo).head_sha("main") == wi1_record.merged_sha

        # WI-2's own dev-complexity-estimation now runs on `main` itself
        # (before `work/wi-2` exists) — exactly the step that, pre-fix,
        # dirtied an already-tracked `.ralph/agent/handoff.md` and crashed
        # `prepare_work_item`. This call must complete normally.
        wi2_result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert wi2_result.work_item.status is WorkItemStatus.COMPLETED
        wi2_record = git_store.get("wi-2")
        assert wi2_record.status is GitWorkItemStatus.MERGED

        # `.ralph/*` never became trackable at any point across both
        # WorkItems and a real merge of each.
        assert _run_git(repo, "ls-files", "--", ".ralph").stdout == ""
        assert _run_git(repo, "status", "--porcelain").stdout == ""
        assert LocalGitWorkspace(repo).head_sha("main") == wi2_record.merged_sha
