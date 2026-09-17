"""Tests for LEAN_FEATURE_FLOW (product decision 2026-09-16): the new
DEFAULT MVPManager workflow — DEV A -> DEV B corrective review -> a
single, deterministic QA phase -> merge -> tag. No complexity
estimation, no isolated QA Test Authoring/promotion, no separate
read-only Review, no separate Final QA phase.

Uses real, temporary git repositories (mirrors
``test_mvp_manager_git_governance.py``/``test_mvp_manager_qa_integration.py``)
so branch/SHA/tag bookkeeping is exercised for real. The QA engine is a
small, scriptable fake satisfying only the ``QAEngine`` Protocol
(``.run(request) -> QAResult``) — deterministic, no LLM/Ralph execution
involved in QA at all in this workflow.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.git_governance import (
    GitGovernancePolicy,
    GitGovernanceService,
    GitWorkItemStatus,
    GitWorkItemStore,
    LocalGitWorkspace,
)
from orchestrator.handoff import HandoffStore
from orchestrator.mvp_manager import MVPManager, WorkflowMode
from orchestrator.project_state import ProjectStateStore, WorkItemStatus
from orchestrator.qa import FailureClassification, QAPhase, QAPolicy, QARunStore, QAResult
from orchestrator.ralph_execution_engine import ExecutionResult
from orchestrator.wait import WaitStore
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ProviderSelectionDiagnostic,
    Worker,
    WorkerSelectionRequest,
)

UTC_NOW = datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc)


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
    (repo / "ROADMAP.md").write_text("# Roadmap\n")
    _run_git(repo, "add", "README.md", "ROADMAP.md")
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
    service = GitGovernanceService(
        store, policy=policy or GitGovernancePolicy(auto_merge=True, require_review=False, require_required_gates=False),
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )
    return service, store


def _counting_id_factory():
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return id_factory


def _worker(worker_id: str, *, provider: str = "anthropic", backend: str = "claude_code", priority: int = 0) -> Worker:
    return Worker.with_single_profile(
        worker_id=worker_id, display_name=worker_id.title(), provider=provider, backend=backend,
        model="m", capabilities=frozenset({"developer"}), priority=priority,
    )


class ScriptedWorkerSelector:
    """A minimal, ordered script of outcomes (a ``Worker`` or an
    ``Exception`` instance to raise) — no quota/provider machinery, used
    only for the wait/resume tests where exact control over "who is
    eligible on which call" matters."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests: list = []

    async def select(self, request):
        self.requests.append(request)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _real_worker_selector(workers: list[Worker]):
    from orchestrator.providers.adapter import ProviderAdapter
    from orchestrator.providers.contracts import ProviderAvailability, ProviderState
    from orchestrator.quota_manager import QuotaManager, QuotaPolicy
    from orchestrator.worker_selector import WorkerSelector

    class _AlwaysAvailableAdapter(ProviderAdapter):
        def __init__(self, provider: str) -> None:
            self._provider = provider

        async def probe(self) -> ProviderState:
            return ProviderState(
                provider=self._provider,
                availability=ProviderAvailability(available=True, observed_at=UTC_NOW, reason=None),
                observed_at=UTC_NOW,
            )

    manager = QuotaManager(
        {"anthropic": _AlwaysAvailableAdapter("anthropic"), "openai": _AlwaysAvailableAdapter("openai")},
        QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW,
    )
    return WorkerSelector(workers, manager)


def _real_worker_selector_with_unavailable(workers: list[Worker], *, unavailable_provider: str):
    """Same real ``WorkerSelector``/``QuotaManager`` wiring as
    ``_real_worker_selector``, except ``unavailable_provider`` reports
    QUOTA_EXHAUSTED (with a known ``reset_at``, like a real probe would).
    Used to prove the worker-pool fallback (>= 2 independent workers per
    participating provider, 2026-09-17 product decision): DEV B selection
    must fall back to a second worker on the *other*, still-available
    provider-mate rather than ever waiting while an eligible worker
    exists — see ROADMAP.md, "Worker pool"."""
    from orchestrator.providers.adapter import ProviderAdapter
    from orchestrator.providers.contracts import (
        ProviderAvailability,
        ProviderState,
        QuotaWindow,
        UnavailabilityReason,
    )
    from orchestrator.quota_manager import QuotaManager, QuotaPolicy
    from orchestrator.worker_selector import WorkerSelector

    class _FixedAdapter(ProviderAdapter):
        def __init__(self, provider: str, *, available: bool) -> None:
            self._provider = provider
            self._available = available

        async def probe(self) -> ProviderState:
            if self._available:
                return ProviderState(
                    provider=self._provider,
                    availability=ProviderAvailability(available=True, observed_at=UTC_NOW, reason=None),
                    observed_at=UTC_NOW,
                )
            return ProviderState(
                provider=self._provider,
                availability=ProviderAvailability(
                    available=False, observed_at=UTC_NOW, reason=UnavailabilityReason.QUOTA_EXHAUSTED,
                ),
                observed_at=UTC_NOW,
                quota_windows=(
                    QuotaWindow(
                        window_type="primary", source="fake", observed_at=UTC_NOW,
                        reset_at=UTC_NOW + timedelta(hours=2),
                    ),
                ),
            )

    manager = QuotaManager(
        {
            "anthropic": _FixedAdapter("anthropic", available=unavailable_provider != "anthropic"),
            "openai": _FixedAdapter("openai", available=unavailable_provider != "openai"),
        },
        QuotaPolicy(state_ttl=timedelta(hours=1)), clock=lambda: UTC_NOW,
    )
    return WorkerSelector(workers, manager)


def _commit_action(filename: str, content: str, message: str):
    def _do(workspace: str | Path) -> None:
        path = Path(workspace) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _run_git(Path(workspace), "add", filename)
        _run_git(Path(workspace), "commit", "-m", message)

    return _do


def _dirty_untracked_action(filename: str, content: str):
    def _do(workspace: str | Path) -> None:
        (Path(workspace) / filename).write_text(content)  # never staged/committed

    return _do


class LeanFakeEngine:
    """Every real execution in LEAN_FEATURE_FLOW's nominal path has
    ``role == "developer"`` (DEV A, DEV B, and a post-QA-FAIL fix are all
    plain development executions, by design — see
    ``WorkflowMode`` docstring) — asserted here directly, so any
    accidental estimator/review/qa_testing-role execution fails the test
    immediately rather than silently behaving oddly.

    ``dev_actions``: an ordered script, one entry per developer-role
    call — ``None`` means "run but change nothing" (e.g. DEV B agreeing
    the code is fine)."""

    def __init__(self, *, dev_actions: list | None = None) -> None:
        self._dev_actions = list(dev_actions) if dev_actions is not None else []
        self._dev_call_count = 0
        self.requests: list = []

    async def execute(self, request) -> ExecutionResult:
        self.requests.append(request)
        assert request.role == "developer", f"LEAN_FEATURE_FLOW must never execute role={request.role!r}"
        sha_before = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
        if self._dev_call_count < len(self._dev_actions):
            action = self._dev_actions[self._dev_call_count]
            if action is not None:
                action(request.workspace)
        self._dev_call_count += 1
        sha_after = _run_git(request.workspace, "rev-parse", "HEAD").stdout.strip()
        record = ExecutionRecord(
            execution_id=request.execution_id, task_id=request.task_id, worker_id=request.worker.worker_id,
            provider=request.worker.provider, backend=request.worker.backend, model=request.model,
            role=request.role, started_at=UTC_NOW, finished_at=UTC_NOW, status=ExecutionStatus.SUCCEEDED,
            git_sha_before=sha_before, git_sha_after=sha_after,
        )
        return ExecutionResult(record=record, exit_code=0)


def _qa_result(*, head_sha: str, regressions: tuple = ()) -> QAResult:
    return QAResult(
        engine_id="fake-qa", observed_head_sha=head_sha, started_at=UTC_NOW, finished_at=UTC_NOW,
        tests_selected=("tests/test_x.py",), tests_executed=("tests/test_x.py",),
        passed_count=0 if regressions else 1, failed_count=len(regressions),
        regressions=regressions, requires_coding_agent=bool(regressions),
        failure_classifications=(FailureClassification.REGRESSION,) if regressions else (),
    )


class DynamicQAEngine:
    """A scriptable ``QAEngine`` Protocol fake — no LLM/Ralph execution
    at all. ``script``: an ordered list of ``QAResult`` (or ``None`` to
    mean "PASS on the request's own head_sha") consumed one per call;
    once exhausted, always PASSes on the request's head_sha."""

    engine_id = "fake-qa"

    def __init__(self, script: list | None = None) -> None:
        self._script = list(script) if script is not None else []
        self.requests: list = []

    def run(self, request) -> QAResult:
        self.requests.append(request)
        if self._script:
            item = self._script.pop(0)
            if callable(item):
                return item(request)
            if item is not None:
                return item
        return _qa_result(head_sha=request.head_sha)


def _manager(
    project_store, handoff_store, selector, engine, *, git_governance_service, qa_engine, qa_run_store,
    wait_store=None, qa_policy=None,
) -> MVPManager:
    return MVPManager(
        project_store, handoff_store, selector, engine,
        git_governance_service=git_governance_service, qa_engine=qa_engine, qa_policy=qa_policy or QAPolicy(required_invariant_ids=("lean-qa-check",)),
        qa_run_store=qa_run_store, wait_store=wait_store,
        # workflow_mode deliberately OMITTED in most tests here — proving
        # LEAN_FEATURE_FLOW is genuinely the default, not something this
        # test file has to opt into.
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
    )


def _new_stack(
    tmp_path: Path, *, workers: list[Worker], dev_actions=None, qa_script=None, qa_policy=None,
    wait_store=None, unavailable_provider: str | None = None,
):
    repo = _git_repo(tmp_path)
    project_store, handoff_store = _stores(tmp_path, repo)
    project_store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="A")
    git_service, git_store = _git_service(tmp_path)
    qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
    engine = LeanFakeEngine(dev_actions=dev_actions)
    selector = (
        _real_worker_selector(workers) if unavailable_provider is None
        else _real_worker_selector_with_unavailable(workers, unavailable_provider=unavailable_provider)
    )
    qa_engine = DynamicQAEngine(qa_script)
    manager = _manager(
        project_store, handoff_store, selector, engine,
        git_governance_service=git_service, qa_engine=qa_engine, qa_run_store=qa_run_store,
        wait_store=wait_store, qa_policy=qa_policy,
    )
    return dict(
        repo=repo, project_store=project_store, handoff_store=handoff_store, git_service=git_service,
        git_store=git_store, qa_run_store=qa_run_store, engine=engine, selector=selector, qa_engine=qa_engine,
        manager=manager,
    )


# --- A/C/D/F/G/H/I/J/W: the nominal happy path ------------------------------


class TestLeanNominalFlow:
    def test_default_workflow_is_lean_and_completes_dev_a_dev_b_qa_merge_tag(self, tmp_path: Path) -> None:
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        stack = _new_stack(
            tmp_path, workers=[alice, victor],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A implementation"), None],
        )
        manager, engine, qa_engine = stack["manager"], stack["engine"], stack["qa_engine"]

        # A: no explicit workflow_mode was passed to MVPManager — this is
        # what "default" means.
        assert manager._workflow_mode is WorkflowMode.LEAN_FEATURE_FLOW

        result = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        dev_requests = [r for r in engine.requests if r.role == "developer"]
        # C/W: exactly DEV A + DEV B — no complexity-estimation execution,
        # no third/fourth hidden development execution.
        assert len(dev_requests) == 2
        # D: DEV B is a genuinely different worker from DEV A.
        assert dev_requests[0].worker.worker_id == "alice"
        assert dev_requests[1].worker.worker_id == "victor"
        # F/G/H: exactly one QA call, phase FINAL_VERIFICATION — no
        # separate QA Test Authoring, no separate second/Final QA call.
        assert len(qa_engine.requests) == 1
        assert qa_engine.requests[0].phase is QAPhase.FINAL_VERIFICATION
        runs = stack["qa_run_store"].list_for_work_item("wi-1")
        assert len(runs) == 1 and runs[0].phase is QAPhase.FINAL_VERIFICATION
        # I: QA PASS -> real merge.
        record = stack["git_store"].get("wi-1")
        assert record.status is GitWorkItemStatus.MERGED
        assert record.merged_sha == record.current_head_sha
        # J: tagged at exactly the merged SHA.
        tag_sha = _run_git(stack["repo"], "rev-parse", "feature/wi-1/done").stdout.strip()
        assert tag_sha == record.merged_sha
        assert (stack["repo"] / "feature.py").is_file()

    def test_dev_b_can_correct_code_and_the_correction_is_what_gets_merged(self, tmp_path: Path) -> None:
        """E: DEV B is not read-only — its own fix is what actually ships."""
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        stack = _new_stack(
            tmp_path, workers=[alice, victor],
            dev_actions=[
                _commit_action("feature.py", "x = 1  # buggy\n", "DEV A implementation"),
                _commit_action("feature.py", "x = 2  # fixed by DEV B\n", "DEV B correction"),
            ],
        )
        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert (stack["repo"] / "feature.py").read_text() == "x = 2  # fixed by DEV B\n"
        record = stack["git_store"].get("wi-1")
        assert record.status is GitWorkItemStatus.MERGED

    def test_dev_b_making_no_changes_still_proceeds_straight_to_qa(self, tmp_path: Path) -> None:
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        stack = _new_stack(
            tmp_path, workers=[alice, victor],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A implementation"), None],
        )
        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))
        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert len(stack["qa_engine"].requests) == 1


# --- K/L/M/N: bounded QA fix-and-retry, then HUMAN_REVIEW_REQUIRED ----------


class TestLeanQAFailBoundedRetry:
    def test_three_qa_fails_exhaust_attempts_then_human_review_required(self, tmp_path: Path) -> None:
        alice, victor, wendy = (
            _worker("alice"), _worker("victor", provider="openai", backend="codex"),
            _worker("wendy", provider="anthropic", backend="claude_code"),
        )
        fail = _qa_result(head_sha="placeholder", regressions=("tests/test_x.py::test_foo",))

        def _fail_for(request):
            return _qa_result(head_sha=request.head_sha, regressions=("tests/test_x.py::test_foo",))

        stack = _new_stack(
            tmp_path, workers=[alice, victor, wendy],
            dev_actions=[
                _commit_action("feature.py", "x = 1\n", "DEV A"),
                None,  # DEV B: no change
                _commit_action("feature.py", "x = 2\n", "fix attempt 1"),
                _commit_action("feature.py", "x = 3\n", "fix attempt 2"),
            ],
            qa_script=[_fail_for, _fail_for, _fail_for],
            qa_policy=QAPolicy(max_qa_cycles=3, required_invariant_ids=("lean-qa-check",)),
        )
        manager = stack["manager"]

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.NEEDS_REWORK

        second = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert second.work_item.status is WorkItemStatus.NEEDS_REWORK

        third = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert third.work_item.status is WorkItemStatus.BLOCKED
        assert "HUMAN_REVIEW_REQUIRED" in (third.work_item.blocked_reason or "")

        # N: exactly 3 QA attempts total, never a 4th.
        runs = stack["qa_run_store"].list_for_work_item("wi-1")
        assert len(runs) == 3
        assert all(r.phase is QAPhase.FINAL_VERIFICATION for r in runs)

        # O: no merge ever happened.
        record = stack["git_store"].get("wi-1")
        assert record.status is not GitWorkItemStatus.MERGED

        # P: no DONE tag was ever created.
        tags = _run_git(stack["repo"], "tag", "-l").stdout
        assert "feature/wi-1/done" not in tags

        # Q: exactly one ROADMAP.md TODO block was appended (not one per attempt).
        roadmap_text = (stack["repo"] / "ROADMAP.md").read_text()
        assert roadmap_text.count("HUMAN REVIEW REQUIRED — wi-1") == 1
        assert _run_git(stack["repo"], "log", "--oneline", "work/wi-1").stdout.count(
            "flag wi-1 for human review"
        ) == 1

    def test_independent_feature_still_progresses_after_another_is_human_review_required(self, tmp_path: Path) -> None:
        """R: an unrelated, dependency-free WorkItem is not blocked by a
        sibling stuck in HUMAN_REVIEW_REQUIRED."""
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")

        def _fail_for(request):
            return _qa_result(head_sha=request.head_sha, regressions=("tests/test_x.py::test_foo",))

        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="A")
        project_store.create_work_item(work_item_id="wi-2", mvp_id="mvp-1", title="B")
        git_service, git_store = _git_service(tmp_path)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
        engine = LeanFakeEngine(dev_actions=[
            _commit_action("wi1.py", "x = 1\n", "DEV A wi-1"), None,
            _commit_action("wi1.py", "x = 2\n", "fix 1"),
            _commit_action("wi1.py", "x = 3\n", "fix 2"),
            _commit_action("wi2.py", "y = 1\n", "DEV A wi-2"), None,
        ])
        selector = _real_worker_selector([alice, victor])
        qa_engine = DynamicQAEngine([_fail_for, _fail_for, _fail_for, None])
        manager = _manager(
            project_store, handoff_store, selector, engine,
            git_governance_service=git_service, qa_engine=qa_engine, qa_run_store=qa_run_store,
            qa_policy=QAPolicy(max_qa_cycles=3, required_invariant_ids=("lean-qa-check",)),
        )

        asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-1: fail 1
        asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-1: fail 2
        wi1_result = asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-1: fail 3 -> human review
        assert wi1_result.work_item.work_item_id == "wi-1"
        assert wi1_result.work_item.status is WorkItemStatus.BLOCKED

        wi2_result = asyncio.run(manager.run_next_work_item("mvp-1"))  # wi-2 proceeds normally
        assert wi2_result.work_item.work_item_id == "wi-2"
        assert wi2_result.work_item.status is WorkItemStatus.COMPLETED
        assert git_store.get("wi-2").status is GitWorkItemStatus.MERGED


# --- S/U: QA never PASSes on a workspace/SHA it cannot trust ----------------


class TestLeanQAIntegrityGuards:
    def test_dirty_workspace_before_qa_never_passes(self, tmp_path: Path) -> None:
        """S: a real, non-noise uncommitted file before QA blocks the
        WorkItem outright — QA is never even invoked on untrustworthy
        content."""
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        stack = _new_stack(
            tmp_path, workers=[alice, victor],
            dev_actions=[
                _commit_action("feature.py", "x = 1\n", "DEV A"),
                _dirty_untracked_action("leftover.txt", "oops\n"),
            ],
        )
        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert len(stack["qa_engine"].requests) == 0  # QA never even ran
        assert stack["git_store"].get("wi-1").status is not GitWorkItemStatus.MERGED

    def test_qa_reporting_a_mismatched_observed_sha_never_passes(self, tmp_path: Path) -> None:
        """U: even a QA engine bug (reporting evidence for the wrong SHA)
        can never produce a PASS — the exact-SHA check is never bypassed."""
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")

        def _wrong_sha(request):
            return _qa_result(head_sha="0" * 40)  # never the real requested head_sha

        stack = _new_stack(
            tmp_path, workers=[alice, victor],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A"), None],
            qa_script=[_wrong_sha],
        )
        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.NEEDS_REWORK  # not COMPLETED
        assert stack["git_store"].get("wi-1").status is not GitWorkItemStatus.MERGED


# --- V: DEV B quota wait / resume -------------------------------------------


class TestLeanDevBQuotaWait:
    def test_no_eligible_dev_b_waits_then_resumes_on_reset(self, tmp_path: Path) -> None:
        alice, victor = _worker("alice"), _worker("victor", provider="openai", backend="codex")
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        project_store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="A")
        git_service, git_store = _git_service(tmp_path)
        qa_run_store = QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)
        engine = LeanFakeEngine(dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A"), None])
        clock_box = {"now": UTC_NOW}
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: clock_box["now"])
        reset_at = UTC_NOW + timedelta(hours=2)
        quota_error = NoEligibleWorkerError(
            WorkerSelectionRequest(required_capabilities=frozenset({"developer"}), author_worker_id="alice"),
            diagnostics=(
                ProviderSelectionDiagnostic(
                    provider="openai", available=False, reason="quota_exhausted", reset_at=(reset_at,),
                ),
            ),
        )
        selector = ScriptedWorkerSelector([alice, quota_error, victor])
        qa_engine = DynamicQAEngine()
        manager = MVPManager(
            project_store, handoff_store, selector, engine,
            git_governance_service=git_service, qa_engine=qa_engine, qa_policy=QAPolicy(required_invariant_ids=("lean-qa-check",)),
            qa_run_store=qa_run_store, wait_store=wait_store,
            clock=lambda: clock_box["now"], id_factory=_counting_id_factory(),
        )

        first = asyncio.run(manager.run_next_work_item("mvp-1"))
        assert first.work_item.status is WorkItemStatus.WAITING
        assert first.wait is not None and first.wait.phase.value == "dev_b_review"

        clock_box["now"] = reset_at + timedelta(minutes=1)
        second = asyncio.run(manager.run_next_work_item("mvp-1"))

        assert second.work_item.status is WorkItemStatus.COMPLETED
        dev_requests = [r for r in engine.requests if r.role == "developer"]
        assert dev_requests[0].worker.worker_id == "alice"
        assert dev_requests[1].worker.worker_id == "victor"
        assert git_store.get("wi-1").status is GitWorkItemStatus.MERGED

    def test_structurally_no_second_developer_blocks_not_waits(self, tmp_path: Path) -> None:
        """Only one developer configured at all — a structural gap, never
        a silent same-worker fallback."""
        alice = _worker("alice")
        stack = _new_stack(
            tmp_path, workers=[alice],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A")],
        )
        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.BLOCKED
        assert "DEV B" in (result.work_item.blocked_reason or "")
        assert stack["git_store"].get("wi-1").status is not GitWorkItemStatus.MERGED


# --- Worker pool fallback (2026-09-17): >= 2 independent workers per -------
# --- participating provider, so provider exhaustion never forces a WAIT ----
# --- while another eligible worker exists.                                --
#
# CASE C (both providers available, cross-provider preferred for DEV B) is
# already fully covered by the nominal happy-path tests above (alice/victor,
# via ``_real_worker_selector`` where both providers are always available) —
# not duplicated here.


class TestLeanWorkerPoolSameProviderFallback:
    def test_dev_b_falls_back_to_same_provider_when_the_other_provider_is_exhausted_anthropic_only(
        self, tmp_path: Path,
    ) -> None:
        """CASE A: anthropic AVAILABLE, openai QUOTA_EXHAUSTED. DEV A picks
        the higher-priority anthropic worker (alice); DEV B must never wait
        for openai to reset — it falls back to the second anthropic worker
        (bob) instead, and the WorkItem completes straight through QA."""
        alice, bob = _worker("alice", priority=100), _worker("bob", priority=90)
        victor = _worker("victor", provider="openai", backend="codex", priority=100)
        oscar = _worker("oscar", provider="openai", backend="codex", priority=90)
        stack = _new_stack(
            tmp_path, workers=[alice, bob, victor, oscar],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A"), None],
            unavailable_provider="openai",
        )

        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.wait is None
        dev_requests = [r for r in stack["engine"].requests if r.role == "developer"]
        assert [r.worker.worker_id for r in dev_requests] == ["alice", "bob"]
        assert stack["git_store"].get("wi-1").status is GitWorkItemStatus.MERGED

    def test_dev_b_falls_back_to_same_provider_when_the_other_provider_is_exhausted_openai_only(
        self, tmp_path: Path,
    ) -> None:
        """CASE B: anthropic QUOTA_EXHAUSTED, openai AVAILABLE — the mirror
        of CASE A. DEV A/DEV B both come from the openai pool (victor/
        oscar); the WorkItem never waits for anthropic to reset."""
        alice, bob = _worker("alice", priority=100), _worker("bob", priority=90)
        victor = _worker("victor", provider="openai", backend="codex", priority=100)
        oscar = _worker("oscar", provider="openai", backend="codex", priority=90)
        stack = _new_stack(
            tmp_path, workers=[alice, bob, victor, oscar],
            dev_actions=[_commit_action("feature.py", "x = 1\n", "DEV A"), None],
            unavailable_provider="anthropic",
        )

        result = asyncio.run(stack["manager"].run_next_work_item("mvp-1"))

        assert result.work_item.status is WorkItemStatus.COMPLETED
        assert result.wait is None
        dev_requests = [r for r in stack["engine"].requests if r.role == "developer"]
        assert [r.worker.worker_id for r in dev_requests] == ["victor", "oscar"]
        assert stack["git_store"].get("wi-1").status is GitWorkItemStatus.MERGED


# --- B/X: GOVERNED_FULL stays available and green ---------------------------


class TestGovernedFullStillAvailable:
    def test_explicit_governed_full_still_selectable(self, tmp_path: Path) -> None:
        """B: passing workflow_mode explicitly still works — this file
        does not re-exercise GOVERNED_FULL's own pipeline (that is
        ``test_mvp_manager*.py``'s job, all still green — see X), it only
        proves the enum value round-trips through MVPManager."""
        from orchestrator.mvp_manager import WorkflowMode as WM

        alice = _worker("alice")
        repo = _git_repo(tmp_path)
        project_store, handoff_store = _stores(tmp_path, repo)
        git_service, _ = _git_service(tmp_path)
        manager = MVPManager(
            project_store, handoff_store, _real_worker_selector([alice]), LeanFakeEngine(),
            git_governance_service=git_service, workflow_mode=WM.GOVERNED_FULL,
            clock=lambda: UTC_NOW, id_factory=_counting_id_factory(),
        )
        assert manager._workflow_mode is WM.GOVERNED_FULL
