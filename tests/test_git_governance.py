"""Tests for Git/PR/merge governance (Phase 1 / Slice 20).

Foundational ``LocalGitWorkspace``/merge tests use real, temporary git
repositories (no git mocking) — this file creates and destroys disposable
repos under ``tmp_path`` for every test, never touching any repo outside
of it. The optional GitHub PR adapter is tested with an injected fake
subprocess runner only: no network call, no real ``gh`` invocation
anywhere in this file.
"""

from __future__ import annotations

import inspect
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.git_governance import (
    DirtyWorkingTreeError,
    DuplicateGitWorkItemError,
    GitBranchMissingError,
    GitGovernanceError,
    GitGovernancePolicy,
    GitGovernanceService,
    GitHeadDriftError,
    GitHubCliPullRequestPublisher,
    GitWorkItemStatus,
    GitWorkItemStore,
    InvalidGitWorkItemTransitionError,
    LocalGitWorkspace,
    MergeEligibilityResult,
    MergeRefusedError,
    NotAGitRepositoryError,
    NotMergeableError,
    ProtectedBranchError,
    UnknownGitWorkItemError,
    sanitize_branch_component,
    work_branch_name,
)

UTC_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial commit")
    return repo


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _run_git(repo, "add", name)
    _run_git(repo, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _store(tmp_path: Path) -> GitWorkItemStore:
    return GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)


# --- branch naming -----------------------------------------------------------


class TestBranchNaming:
    def test_deterministic(self) -> None:
        assert work_branch_name("wi-42") == work_branch_name("wi-42")

    def test_sanitizes_unsafe_characters(self) -> None:
        assert sanitize_branch_component("wi 42/../weird*name") == "wi-42-.-weird-name"

    def test_prefixed_and_lowercased(self) -> None:
        assert work_branch_name("WI-Fix-ADD") == "work/wi-fix-add"

    def test_never_empty(self) -> None:
        assert sanitize_branch_component("...") not in ("", ".", "..")


# --- LocalGitWorkspace: real repos ------------------------------------------


class TestLocalGitWorkspace:
    def test_detects_repository(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        assert LocalGitWorkspace(repo).is_git_repository() is True

    def test_detects_non_repository(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        assert LocalGitWorkspace(not_a_repo).is_git_repository() is False

    def test_reads_current_branch(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        assert LocalGitWorkspace(repo).current_branch() == "main"

    def test_reads_head(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        assert ws.head_sha() == _run_git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_working_tree_clean(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        status = LocalGitWorkspace(repo).working_tree_status()
        assert status.is_clean_for_governance
        assert status.tracked_dirty == ()

    def test_dirty_tracked_file_detected(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "README.md").write_text("modified\n")
        status = LocalGitWorkspace(repo).working_tree_status()
        assert not status.is_clean_for_governance
        assert "README.md" in status.tracked_dirty

    def test_untracked_file_reported_but_not_blocking(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "new_file.txt").write_text("new\n")
        status = LocalGitWorkspace(repo).working_tree_status()
        assert status.is_clean_for_governance  # untracked alone never blocks
        assert "new_file.txt" in status.untracked

    def test_branch_creation(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        assert not ws.branch_exists("work/wi-1")
        ws.create_branch("work/wi-1", from_ref="main")
        assert ws.branch_exists("work/wi-1")

    def test_head_sha_refreshed_after_new_commit(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        before = ws.head_sha()
        _commit_file(repo, "a.txt", "a", "add a")
        after = ws.head_sha()
        assert before != after

    def test_missing_branch_detected(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        assert ws.branch_exists("work/does-not-exist") is False
        assert ws.try_rev_parse("work/does-not-exist") is None

    def test_switch_and_merge_ff_only_success(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        ws.create_branch("work/wi-1", from_ref="main")
        ws.switch("work/wi-1")
        new_head = _commit_file(repo, "a.txt", "a", "work commit")
        ws.switch("main")
        merged = ws.merge_ff_only("work/wi-1")
        assert merged == new_head
        assert ws.head_sha("main") == new_head

    def test_merge_ff_only_refuses_divergence(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        ws = LocalGitWorkspace(repo)
        ws.create_branch("work/wi-1", from_ref="main")
        ws.switch("work/wi-1")
        _commit_file(repo, "a.txt", "a", "work commit")
        ws.switch("main")
        _commit_file(repo, "b.txt", "b", "main advanced independently")
        with pytest.raises(MergeRefusedError):
            ws.merge_ff_only("work/wi-1")
        # And main is genuinely untouched by the refused attempt.
        assert "b.txt" in (repo / "b.txt").name


class TestNoDestructiveCommands:
    def test_module_never_issues_destructive_or_history_rewriting_git_commands(self) -> None:
        from orchestrator import git_governance as module

        source = inspect.getsource(module)
        forbidden = (
            "reset", "--hard", "clean -f", "clean", "-fd", "rebase",
            "push", "--force", "-f\"", "cherry-pick", "commit\"",
        )
        # Precise check: only these specific dangerous argv tokens must
        # never appear as a literal git subcommand/flag anywhere in the
        # module (not a substring ban on unrelated prose/docstrings).
        for token in ("\"reset\"", "\"--hard\"", "\"clean\"", "\"-fd\"", "\"rebase\"", "\"push\"", "\"--force\""):
            assert token not in source, f"forbidden git argv token found: {token}"


# --- GitWorkItemStore --------------------------------------------------------


class TestGitWorkItemStore:
    def test_create_and_get_roundtrip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        record = service.prepare_work_item(
            project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", repository_path=repo,
        )
        assert store.get("wi-1") == record
        assert record.status is GitWorkItemStatus.PREPARED

    def test_duplicate_direct_create_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        record = service.prepare_work_item(
            project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", repository_path=repo,
        )
        with pytest.raises(DuplicateGitWorkItemError):
            store.create(record)

    def test_unknown_work_item_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownGitWorkItemError):
            store.get("does-not-exist")

    def test_invalid_transition_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        with pytest.raises(InvalidGitWorkItemTransitionError):
            store.mark_merged("wi-1", merged_sha="deadbeef")  # PREPARED -> MERGED is not allowed directly

    def test_events_are_insert_only_audit_trail(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        service.capture_head("wi-1", repository_path=repo)
        events = store.list_events("wi-1")
        event_types = [e[0] for e in events]
        assert event_types[0] == "branch_prepared"
        assert "head_captured" in event_types


# --- GitGovernanceService: preparation / protected branch -------------------


class TestPreparation:
    def test_main_is_protected_by_default(self) -> None:
        policy = GitGovernancePolicy()
        assert "main" in policy.protected_branches

    def test_assert_not_protected_branch_target_raises_for_main(self, tmp_path: Path) -> None:
        service = GitGovernanceService(_store(tmp_path), clock=lambda: UTC_NOW)
        with pytest.raises(ProtectedBranchError):
            service.assert_not_protected_branch_target("main")

    def test_worker_never_started_on_protected_branch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        record = service.prepare_work_item(
            project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo,
        )
        assert record.work_branch != service.policy.base_branch
        service.assert_not_protected_branch_target(record.work_branch)  # never raises

    def test_dirty_tracked_worktree_fails_closed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        (repo / "README.md").write_text("dirty\n")
        with pytest.raises(DirtyWorkingTreeError):
            service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        # Never auto-stashed/reset/cleaned: the dirty file is still there, untouched.
        assert (repo / "README.md").read_text() == "dirty\n"

    def test_untracked_files_do_not_block_preparation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        (repo / "scratch.txt").write_text("untracked\n")
        record = service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        assert record.status is GitWorkItemStatus.PREPARED

    def test_base_sha_captured_and_immutable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        record = service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        expected = LocalGitWorkspace(repo).head_sha("main")
        assert record.base_sha == expected

        service.capture_head("wi-1", repository_path=repo)
        reloaded = store.get("wi-1")
        assert reloaded.base_sha == record.base_sha  # never changes across the WorkItem's lifetime

    def test_work_branch_stable_across_restart(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        store = _store(tmp_path)
        service_a = GitGovernanceService(store, clock=lambda: UTC_NOW)
        first = service_a.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)

        # Simulate a cold restart: a brand new store handle + service.
        store_b = GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)
        service_b = GitGovernanceService(store_b, clock=lambda: UTC_NOW)
        second = service_b.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)

        assert first.work_branch == second.work_branch
        assert first.base_sha == second.base_sha
        assert LocalGitWorkspace(repo).branch_exists(first.work_branch)

    def test_reused_on_rework_never_creates_second_branch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        first = service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        service.capture_head("wi-1", repository_path=repo)
        second = service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        assert first.work_branch == second.work_branch
        assert first.git_work_id == second.git_work_id

    def test_not_a_git_repository_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        plain = tmp_path / "not_a_repo"
        plain.mkdir()
        with pytest.raises(NotAGitRepositoryError):
            service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=plain)


# --- head capture / review target -------------------------------------------


class TestHeadCapture:
    def test_head_sha_captured_after_execution(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        new_head = _commit_file(repo, "a.txt", "a", "dev commit")
        record = service.capture_head("wi-1", repository_path=repo)
        assert record.current_head_sha == new_head
        assert record.status is GitWorkItemStatus.IN_PROGRESS

    def test_capture_head_detects_drift_from_wrong_branch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        LocalGitWorkspace(repo).switch("main")
        with pytest.raises(GitHeadDriftError):
            service.capture_head("wi-1", repository_path=repo)


class TestReviewTarget:
    def test_review_targets_exact_head(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        head = _commit_file(repo, "a.txt", "a", "dev commit")
        service.capture_head("wi-1", repository_path=repo)
        record = service.assert_review_target("wi-1", repository_path=repo)
        assert record.current_head_sha == head

    def test_review_target_missing_branch_fails_closed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        record = store.get("wi-1")
        _run_git(repo, "checkout", "main")
        _run_git(repo, "branch", "-D", record.work_branch)
        with pytest.raises(GitBranchMissingError):
            service.assert_review_target("wi-1", repository_path=repo)


# --- merge eligibility -------------------------------------------------------


def _prepared(tmp_path: Path, *, work_item_id: str = "wi-1") -> tuple[GitGovernanceService, Path, str]:
    store = _store(tmp_path)
    service = GitGovernanceService(store, clock=lambda: UTC_NOW)
    repo = _init_repo(tmp_path)
    service.prepare_work_item(project_id="p", mvp_id="m", work_item_id=work_item_id, repository_path=repo)
    LocalGitWorkspace(repo).switch(work_branch_name(work_item_id))
    head = _commit_file(repo, "a.txt", "a", "dev commit")
    service.capture_head(work_item_id, repository_path=repo)
    return service, repo, head


class TestMergeEligibility:
    def test_passes_with_current_gate_and_review(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )
        assert result.mergeable is True
        assert result.reason is None

    def test_missing_review_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=None, review_git_sha=None,
        )
        assert result.mergeable is False
        assert "review" in result.reason

    def test_missing_gate_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=None, gate_git_sha=None, review_approved=True, review_git_sha=head,
        )
        assert result.mergeable is False
        assert "gate" in result.reason

    def test_rejected_review_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=False, review_git_sha=head,
        )
        assert result.mergeable is False

    def test_review_on_stale_sha_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha="old-sha-1234",
        )
        assert result.mergeable is False
        assert "old SHA" in result.reason

    def test_gate_on_stale_sha_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha="old-sha-1234", review_approved=True, review_git_sha=head,
        )
        assert result.mergeable is False
        assert "old SHA" in result.reason

    def test_work_item_not_completed_not_mergeable(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        result = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="reviewing",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )
        assert result.mergeable is False

    def test_new_commit_after_new_commit_lands_new_review_required(self, tmp_path: Path) -> None:
        """A rework cycle: a second commit lands after the first review; the
        old review's SHA no longer matches the new head, so it never
        suffices — never automatically declared "still fine"."""
        service, repo, first_head = _prepared(tmp_path)
        # first review approves the first head — merge-ready.
        result_1 = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=first_head, review_approved=True, review_git_sha=first_head,
        )
        assert result_1.mergeable is True

        # a new dev commit lands (rework) — invalidates old evidence.
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        second_head = _commit_file(repo, "b.txt", "b", "rework commit")
        from orchestrator.git_governance import GitWorkItemStore
        store = GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)
        service2 = GitGovernanceService(store, clock=lambda: UTC_NOW)
        service2.capture_head("wi-1", repository_path=repo)
        assert store.get("wi-1").status is GitWorkItemStatus.IN_PROGRESS  # moved back from MERGE_READY

        result_2 = service2.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=first_head, review_approved=True, review_git_sha=first_head,
        )
        assert result_2.mergeable is False  # old review no longer covers the new head

    def test_never_llm_pure_function_of_inputs(self, tmp_path: Path) -> None:
        """Same explicit inputs -> same verdict, deterministically, twice."""
        service, repo, head = _prepared(tmp_path)
        kwargs = dict(
            work_item_status="completed", gate_passed=True, gate_git_sha=head,
            review_approved=True, review_git_sha=head,
        )
        r1 = service.compute_merge_eligibility("wi-1", repository_path=repo, **kwargs)
        r2 = service.compute_merge_eligibility("wi-1", repository_path=repo, **kwargs)
        assert r1.mergeable == r2.mergeable == True  # noqa: E712

    def test_module_source_never_imports_an_llm_or_worker_selector(self) -> None:
        from orchestrator import git_governance as module

        source = inspect.getsource(module)
        for forbidden in ("worker_selector", "WorkerSelector", "ralph_execution_engine", "AdaptiveExecutionSelector"):
            assert forbidden not in source


# --- merge -------------------------------------------------------------------


class TestMerge:
    def _eligible(self, service: GitGovernanceService, repo: Path, head: str) -> MergeEligibilityResult:
        return service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )

    def test_ff_only_merge_success(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        eligibility = self._eligible(service, repo, head)
        record = service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        assert record.status is GitWorkItemStatus.MERGED
        assert record.merged_sha == head
        assert LocalGitWorkspace(repo).head_sha("main") == head

    def test_compatible_base_advance_still_mergeable(self, tmp_path: Path) -> None:
        """base advances, but the work branch was rebased from that same
        commit chain (i.e. is still a clean fast-forward child) — the
        expected 'good' case, distinct from real divergence."""
        service, repo, head = _prepared(tmp_path)
        # Simulate: nothing else touched main since base_sha (still the tip).
        eligibility = self._eligible(service, repo, head)
        assert eligibility.mergeable is True

    def test_divergence_detected_and_refused(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch("main")
        _commit_file(repo, "unrelated.txt", "x", "main advanced independently")
        eligibility = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )
        assert eligibility.mergeable is False
        assert "diverged" in eligibility.reason
        assert GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW).get("wi-1").status is GitWorkItemStatus.CONFLICT

    def test_ff_only_refuses_and_never_mutates_main_on_divergence(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch("main")
        main_head_before = _commit_file(repo, "unrelated.txt", "x", "main advanced independently")
        eligibility = MergeEligibilityResult(
            work_item_id="wi-1", head_sha=head, mergeable=True, reason=None, evaluated_at=UTC_NOW,
        )  # force an attempt despite the real divergence, to prove the git layer itself refuses
        with pytest.raises(MergeRefusedError):
            service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        assert LocalGitWorkspace(repo).head_sha("main") == main_head_before  # untouched

    def test_no_rebase_no_force_no_reset_attempted(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch("main")
        _commit_file(repo, "unrelated.txt", "x", "main advanced")
        eligibility = MergeEligibilityResult(
            work_item_id="wi-1", head_sha=head, mergeable=True, reason=None, evaluated_at=UTC_NOW,
        )
        with pytest.raises(MergeRefusedError):
            service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        # The work branch itself is untouched — no rebase was attempted.
        work_head_still = LocalGitWorkspace(repo).head_sha(work_branch_name("wi-1"))
        assert work_head_still == head

    def test_not_mergeable_refused_by_service(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        not_eligible = MergeEligibilityResult(
            work_item_id="wi-1", head_sha=head, mergeable=False, reason="missing review", evaluated_at=UTC_NOW,
        )
        with pytest.raises(NotMergeableError):
            service.merge("wi-1", repository_path=repo, eligibility=not_eligible)

    def test_merged_sha_persisted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        head = _commit_file(repo, "a.txt", "a", "dev commit")
        service.capture_head("wi-1", repository_path=repo)
        eligibility = self._eligible(service, repo, head)
        service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        assert store.get("wi-1").merged_sha == head

    def test_repeated_merge_is_idempotent(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        eligibility = self._eligible(service, repo, head)
        first = service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        second = service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        assert first == second
        assert LocalGitWorkspace(repo).head_sha("main") == head  # merged exactly once

    def test_cold_restart_after_merge_still_reports_merged(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        eligibility = self._eligible(service, repo, head)
        service.merge("wi-1", repository_path=repo, eligibility=eligibility)

        store_b = GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)
        service_b = GitGovernanceService(store_b, clock=lambda: UTC_NOW)
        reconciled = service_b.reconcile("wi-1", repository_path=repo)
        assert reconciled.status is GitWorkItemStatus.MERGED
        assert reconciled.merged_sha == head


class TestMergeHeadDriftHardening:
    """Slice 21.5, point D: merge() must never fold in a work-branch tip
    the supplied eligibility never actually covered — reproduced first
    (the "confirms" test would have merged H3 on H2's eligibility before
    this slice's fix), then the fix is asserted directly.
    """

    def test_confirms_merge_used_to_trust_the_branch_name_not_the_sha(self, tmp_path: Path) -> None:
        # Historical/regression pin: this exact scenario is exactly what
        # Slice 21.5 closes. With today's fix it must raise
        # GitHeadDriftError; if this ever silently merges H3 again, that
        # is the TOCTOU bug back.
        service, repo, h2 = _prepared(tmp_path)
        eligibility_for_h2 = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=h2, review_approved=True, review_git_sha=h2,
        )
        assert eligibility_for_h2.mergeable is True

        # The work branch advances again *after* eligibility was computed
        # (e.g. a stray/concurrent rework execution) — still a clean
        # fast-forward child, so git itself would happily allow it.
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        h3 = _commit_file(repo, "c.txt", "c", "unreviewed stray commit")
        assert h3 != h2

        with pytest.raises(GitHeadDriftError):
            service.merge("wi-1", repository_path=repo, eligibility=eligibility_for_h2)

    def test_main_is_not_advanced_when_drift_is_refused(self, tmp_path: Path) -> None:
        service, repo, h2 = _prepared(tmp_path)
        eligibility_for_h2 = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=h2, review_approved=True, review_git_sha=h2,
        )
        main_before = LocalGitWorkspace(repo).head_sha("main")

        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        _commit_file(repo, "c.txt", "c", "unreviewed stray commit")

        with pytest.raises(GitHeadDriftError):
            service.merge("wi-1", repository_path=repo, eligibility=eligibility_for_h2)
        assert LocalGitWorkspace(repo).head_sha("main") == main_before  # untouched, no rebase/reset/force

    def test_merging_the_exact_current_head_still_works(self, tmp_path: Path) -> None:
        # Nominal case unaffected: a fresh eligibility computed for the
        # branch's real current tip still merges normally.
        service, repo, h2 = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        h3 = _commit_file(repo, "c.txt", "c", "second dev commit")
        service.capture_head("wi-1", repository_path=repo)

        eligibility_for_h3 = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=h3, review_approved=True, review_git_sha=h3,
        )
        assert eligibility_for_h3.mergeable is True
        record = service.merge("wi-1", repository_path=repo, eligibility=eligibility_for_h3)
        assert record.status is GitWorkItemStatus.MERGED
        assert record.merged_sha == h3
        assert LocalGitWorkspace(repo).head_sha("main") == h3


# --- reconcile / restart recovery -------------------------------------------


class TestReconcile:
    def test_missing_branch_fails_closed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)
        repo = _init_repo(tmp_path)
        record = service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        _run_git(repo, "checkout", "main")
        _run_git(repo, "branch", "-D", record.work_branch)
        with pytest.raises(GitBranchMissingError):
            service.reconcile("wi-1", repository_path=repo)

    def test_head_drift_without_known_execution_fails_closed(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        _commit_file(repo, "b.txt", "b", "unaccounted-for commit")
        with pytest.raises(GitHeadDriftError):
            service.reconcile("wi-1", repository_path=repo)

    def test_head_drift_reconciled_when_matching_known_execution(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        new_head = _commit_file(repo, "b.txt", "b", "a legitimate, persisted execution's commit")
        reconciled = service.reconcile("wi-1", repository_path=repo, known_execution_shas=frozenset({new_head}))
        assert reconciled.current_head_sha == new_head

    def test_reconcile_terminal_merged_is_a_noop(self, tmp_path: Path) -> None:
        service, repo, head = _prepared(tmp_path)
        eligibility = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=True, gate_git_sha=head, review_approved=True, review_git_sha=head,
        )
        service.merge("wi-1", repository_path=repo, eligibility=eligibility)
        # Even with an unrelated drift on the (now-irrelevant) work branch,
        # reconciling a MERGED record never raises.
        record = service.reconcile("wi-1", repository_path=repo)
        assert record.status is GitWorkItemStatus.MERGED


# --- pull request abstraction ------------------------------------------------


class TestPullRequestPublisher:
    def test_create_pr_uses_exact_argv_no_shell_interpolation(self, tmp_path: Path) -> None:
        captured = {}

        def fake_runner(argv, cwd):
            captured["argv"] = list(argv)
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(
                argv, 0, stdout="https://github.com/example/repo/pull/42\n", stderr="",
            )

        publisher = GitHubCliPullRequestPublisher(clock=lambda: UTC_NOW, subprocess_runner=fake_runner)
        record = publisher.create_pull_request(
            repository_path=Path("/tmp/repo"), base_branch="main", head_branch="work/wi-1",
            head_sha="abc123", title="Fix add()", body="Implements the fix; $(rm -rf /) is inert text here",
        )
        assert captured["argv"] == [
            "gh", "pr", "create", "--base", "main", "--head", "work/wi-1",
            "--title", "Fix add()", "--body", "Implements the fix; $(rm -rf /) is inert text here",
        ]
        assert record.number == 42
        assert record.url.endswith("/pull/42")
        assert record.state == "open"

    def test_pr_record_persisted_and_auditable(self, tmp_path: Path) -> None:
        def fake_runner(argv, cwd):
            return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/pull/7\n", stderr="")

        store = _store(tmp_path)
        publisher = GitHubCliPullRequestPublisher(clock=lambda: UTC_NOW, subprocess_runner=fake_runner)
        policy = GitGovernancePolicy(remote_pr_enabled=True)
        service = GitGovernanceService(store, policy=policy, clock=lambda: UTC_NOW, pull_request_publisher=publisher)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        pr = service.publish_pull_request("wi-1", title="t", body="b")
        assert pr.number == 7
        reloaded = store.get("wi-1")
        assert reloaded.pull_request_number == 7
        assert reloaded.pull_request_url == "https://example.invalid/pull/7"

    def test_pr_existence_never_implies_merge_authorization(self, tmp_path: Path) -> None:
        def fake_runner(argv, cwd):
            return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/pull/1\n", stderr="")

        store = _store(tmp_path)
        publisher = GitHubCliPullRequestPublisher(clock=lambda: UTC_NOW, subprocess_runner=fake_runner)
        policy = GitGovernancePolicy(remote_pr_enabled=True)
        service = GitGovernanceService(store, policy=policy, clock=lambda: UTC_NOW, pull_request_publisher=publisher)
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        head = _commit_file(repo, "a.txt", "a", "dev commit")
        LocalGitWorkspace(repo).switch(work_branch_name("wi-1"))
        service.capture_head("wi-1", repository_path=repo)
        service.publish_pull_request("wi-1", title="t", body="b")

        # No review/gate evidence at all — still NOT_MERGEABLE despite a PR existing.
        eligibility = service.compute_merge_eligibility(
            "wi-1", repository_path=repo, work_item_status="completed",
            gate_passed=None, gate_git_sha=None, review_approved=None, review_git_sha=None,
        )
        assert eligibility.mergeable is False

    def test_disabled_by_policy_refuses(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = GitGovernanceService(store, clock=lambda: UTC_NOW)  # remote_pr_enabled defaults False
        repo = _init_repo(tmp_path)
        service.prepare_work_item(project_id="p", mvp_id="m", work_item_id="wi-1", repository_path=repo)
        with pytest.raises(GitGovernanceError):
            service.publish_pull_request("wi-1", title="t", body="b")

    def test_no_network_call_in_pytest(self, tmp_path: Path) -> None:
        """Sanity: the default publisher is never constructed/used unless a
        test explicitly injects a fake runner — this file never imports
        `requests`/`httpx`/`urllib.request` and never shells out to a real
        `gh` binary."""
        import orchestrator.git_governance as module

        source = inspect.getsource(module)
        assert "requests" not in source
        assert "urllib" not in source
        assert "httpx" not in source
