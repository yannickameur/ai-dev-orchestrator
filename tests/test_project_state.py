"""Tests for ProjectStateStore / Project / MVP / WorkItem (Phase 1 / Slice 7).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.project_state import (
    DuplicateMVPError,
    DuplicateProjectError,
    DuplicateWorkItemError,
    InvalidWorkItemTransitionError,
    MVPStatus,
    ProjectStateStore,
    UnknownMVPError,
    UnknownProjectError,
    UnknownWorkItemError,
    WorkItemStatus,
)


def _store(tmp_path: Path) -> ProjectStateStore:
    return ProjectStateStore(tmp_path / "project_state.sqlite3")


def _seed_project_and_mvp(store: ProjectStateStore, tmp_path: Path) -> None:
    store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship the thing")


class TestProjectAndMVPCreation:
    def test_create_and_get_project(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        project = store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)

        assert project.current_mvp_id is None
        fetched = store.get_project("proj-1")
        assert fetched == project

    def test_duplicate_project_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)

        with pytest.raises(DuplicateProjectError):
            store.create_project(project_id="proj-1", name="Demo2", workspace=tmp_path)

    def test_unknown_project_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownProjectError):
            store.get_project("does-not-exist")

    def test_mvp_requires_existing_project(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownProjectError):
            store.create_mvp(mvp_id="mvp-1", project_id="does-not-exist", objective="X")

    def test_duplicate_mvp_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        with pytest.raises(DuplicateMVPError):
            store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="X")

    def test_unknown_mvp_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownMVPError):
            store.get_mvp("does-not-exist")

    def test_set_current_mvp(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        updated = store.set_current_mvp("proj-1", "mvp-1")
        assert updated.current_mvp_id == "mvp-1"


class TestPersistenceAcrossRestart:
    def test_state_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "project_state.sqlite3"
        store = ProjectStateStore(db_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="Do X")
        store.close()

        reopened = ProjectStateStore(db_path)
        assert reopened.get_project("proj-1").name == "Demo"
        assert reopened.get_mvp("mvp-1").objective == "Ship the thing"
        assert reopened.get_work_item("wi-1").title == "Do X"


class TestWorkItemCreation:
    def test_work_item_requires_existing_mvp(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownMVPError):
            store.create_work_item(work_item_id="wi-1", mvp_id="does-not-exist", title="X")

    def test_duplicate_work_item_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="Do X")
        with pytest.raises(DuplicateWorkItemError):
            store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="Do X again")

    def test_unknown_work_item_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownWorkItemError):
            store.get_work_item("does-not-exist")

    def test_new_work_item_starts_planned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        wi = store.create_work_item(work_item_id="wi-1", mvp_id="mvp-1", title="Do X")
        assert wi.status is WorkItemStatus.PLANNED

    def test_list_work_items_is_deterministically_ordered(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-c", mvp_id="mvp-1", title="C")
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(work_item_id="wi-b", mvp_id="mvp-1", title="B")

        ids = [wi.work_item_id for wi in store.list_work_items("mvp-1")]
        assert ids == ["wi-a", "wi-b", "wi-c"]


class TestDependencyResolution:
    def test_work_item_without_dependency_becomes_ready(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-a").status is WorkItemStatus.READY

    def test_unsatisfied_dependency_is_not_eligible(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(
            work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=["wi-a"]
        )

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-b").status is WorkItemStatus.PLANNED

    def test_satisfied_dependency_yields_ready(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(
            work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=["wi-a"]
        )
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_completed("wi-a")

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-b").status is WorkItemStatus.READY

    def test_chain_a_b_c_b_waits_for_a(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=["wi-a"])
        store.create_work_item(work_item_id="wi-c", mvp_id="mvp-1", title="C", dependencies=["wi-b"])

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-a").status is WorkItemStatus.READY
        assert store.get_work_item("wi-b").status is WorkItemStatus.PLANNED
        assert store.get_work_item("wi-c").status is WorkItemStatus.PLANNED

    def test_unknown_dependency_is_blocked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(
            work_item_id="wi-a", mvp_id="mvp-1", title="A", dependencies=["ghost"]
        )

        store.refresh_readiness("mvp-1")

        item = store.get_work_item("wi-a")
        assert item.status is WorkItemStatus.BLOCKED
        assert "ghost" in item.blocked_reason

    def test_dependency_on_failed_item_is_blocked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(
            work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=["wi-a"]
        )
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_failed("wi-a")

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-b").status is WorkItemStatus.BLOCKED

    def test_simple_circular_dependency_is_blocked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A", dependencies=["wi-b"])
        store.create_work_item(work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=["wi-a"])

        store.refresh_readiness("mvp-1")

        assert store.get_work_item("wi-a").status is WorkItemStatus.BLOCKED
        assert store.get_work_item("wi-b").status is WorkItemStatus.BLOCKED
        assert "circular" in store.get_work_item("wi-a").blocked_reason


class TestWorkItemTransitions:
    def test_ready_to_running_to_completed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        completed = store.mark_work_item_completed("wi-a")
        assert completed.status is WorkItemStatus.COMPLETED

    def test_terminal_work_item_cannot_transition_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_completed("wi-a")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_failed("wi-a")

    def test_cannot_run_a_planned_work_item_directly(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_running("wi-a")


class TestMVPTransitions:
    def test_mark_mvp_running_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        first = store.mark_mvp_running("mvp-1")
        second = store.mark_mvp_running("mvp-1")
        assert first.status is MVPStatus.RUNNING
        assert second.status is MVPStatus.RUNNING


class TestReviewWorkflowTransitions:
    def test_running_to_reviewing_to_completed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        reviewing = store.mark_work_item_reviewing("wi-a")
        assert reviewing.status is WorkItemStatus.REVIEWING

        completed = store.mark_work_item_completed("wi-a")
        assert completed.status is WorkItemStatus.COMPLETED

    def test_reviewing_to_needs_rework_to_running_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")

        rework = store.mark_work_item_needs_rework("wi-a")
        assert rework.status is WorkItemStatus.NEEDS_REWORK

        running_again = store.mark_work_item_running("wi-a")
        assert running_again.status is WorkItemStatus.RUNNING

    def test_reviewing_to_blocked_with_reason(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")

        blocked = store.mark_work_item_blocked("wi-a", reason="max review cycles reached")
        assert blocked.status is WorkItemStatus.BLOCKED
        assert blocked.blocked_reason == "max review cycles reached"

    def test_needs_rework_cannot_go_directly_to_completed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")
        store.mark_work_item_needs_rework("wi-a")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_completed("wi-a")

    def test_running_cannot_go_directly_to_reviewing_skip_not_allowed_from_ready(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_reviewing("wi-a")  # still READY, not RUNNING


class TestWaitingTransitions:
    """Slice 11: WAITING is an orchestration decision, always resumed into
    the exact state it was waiting to re-attempt — never "in place"."""

    def test_ready_to_waiting_to_ready_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")

        waiting = store.mark_work_item_waiting("wi-a")
        assert waiting.status is WorkItemStatus.WAITING

        resumed = store.mark_work_item_ready("wi-a")
        assert resumed.status is WorkItemStatus.READY

    def test_needs_rework_to_waiting_to_needs_rework_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")
        store.mark_work_item_needs_rework("wi-a")

        waiting = store.mark_work_item_waiting("wi-a")
        assert waiting.status is WorkItemStatus.WAITING

        resumed = store.mark_work_item_needs_rework("wi-a")
        assert resumed.status is WorkItemStatus.NEEDS_REWORK

    def test_reviewing_to_waiting_to_reviewing_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")

        waiting = store.mark_work_item_waiting("wi-a")
        assert waiting.status is WorkItemStatus.WAITING

        resumed = store.mark_work_item_reviewing("wi-a")
        assert resumed.status is WorkItemStatus.REVIEWING

    def test_waiting_can_give_up_to_blocked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_waiting("wi-a")

        blocked = store.mark_work_item_blocked("wi-a", reason="no reliable reset known anymore")
        assert blocked.status is WorkItemStatus.BLOCKED

    def test_waiting_dependent_never_becomes_ready(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(
            work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=frozenset({"wi-a"})
        )
        store.refresh_readiness("mvp-1")
        store.mark_work_item_waiting("wi-a")

        # wi-b's dependency (wi-a) is neither COMPLETED nor unresolvable —
        # it must stay PLANNED, never become READY nor BLOCKED.
        updated = store.refresh_readiness("mvp-1")
        assert updated == []
        assert store.get_work_item("wi-b").status is WorkItemStatus.PLANNED

    def test_running_to_waiting_to_running_again(self, tmp_path: Path) -> None:
        # Slice 24: a QA Test Authoring worker-selection failure diagnosable
        # as quota happens while the WorkItem is still RUNNING (development
        # already succeeded) — resuming re-enters RUNNING directly (never
        # READY, since re-running development itself would be wrong; only
        # QA authoring onward is retried).
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")

        waiting = store.mark_work_item_waiting("wi-a")
        assert waiting.status is WorkItemStatus.WAITING

        resumed = store.mark_work_item_running("wi-a")
        assert resumed.status is WorkItemStatus.RUNNING

    def test_terminal_work_item_cannot_move_to_waiting(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_completed("wi-a")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_waiting("wi-a")


class TestRecoveryRequiredTransitions:
    """Slice 11b: RECOVERY_REQUIRED is distinct from WAITING — no deadline,
    immediately re-orchestrable, always resumed via a new execution."""

    def test_running_to_recovery_required_to_running_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")

        recovery = store.mark_work_item_recovery_required("wi-a")
        assert recovery.status is WorkItemStatus.RECOVERY_REQUIRED

        resumed = store.mark_work_item_running("wi-a")
        assert resumed.status is WorkItemStatus.RUNNING

    def test_reviewing_to_recovery_required_to_reviewing_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_reviewing("wi-a")

        recovery = store.mark_work_item_recovery_required("wi-a")
        assert recovery.status is WorkItemStatus.RECOVERY_REQUIRED

        resumed = store.mark_work_item_reviewing("wi-a")
        assert resumed.status is WorkItemStatus.REVIEWING

    def test_recovery_required_can_escape_to_blocked_defensively(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_recovery_required("wi-a")

        blocked = store.mark_work_item_blocked("wi-a", reason="no recovery handoff could be built")
        assert blocked.status is WorkItemStatus.BLOCKED

    def test_recovery_required_dependent_never_becomes_ready(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.create_work_item(
            work_item_id="wi-b", mvp_id="mvp-1", title="B", dependencies=frozenset({"wi-a"})
        )
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_recovery_required("wi-a")

        updated = store.refresh_readiness("mvp-1")
        assert updated == []
        assert store.get_work_item("wi-b").status is WorkItemStatus.PLANNED

    def test_ready_cannot_go_directly_to_recovery_required(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_recovery_required("wi-a")  # still READY, never ran

    def test_terminal_work_item_cannot_move_to_recovery_required(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")
        store.mark_work_item_completed("wi-a")

        with pytest.raises(InvalidWorkItemTransitionError):
            store.mark_work_item_recovery_required("wi-a")

    def test_recovery_required_and_waiting_are_distinct_states(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_project_and_mvp(store, tmp_path)
        store.create_work_item(work_item_id="wi-a", mvp_id="mvp-1", title="A")
        store.refresh_readiness("mvp-1")
        store.mark_work_item_running("wi-a")

        recovery = store.mark_work_item_recovery_required("wi-a")
        assert recovery.status is not WorkItemStatus.WAITING
        assert WorkItemStatus.RECOVERY_REQUIRED != WorkItemStatus.WAITING
