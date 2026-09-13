"""Tests for notification + optimistic approval window (Phase 1 / Slice 13).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, an
injectable clock/id_factory, no network, no subprocess, no real sleep
anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.approval import (
    ApprovalCoordinator,
    ApprovalNotYetDueError,
    ApprovalPolicy,
    ApprovalStatus,
    ApprovalStore,
    ApprovalWindow,
    DEFAULT_WINDOW_SECONDS,
    DuplicateApprovalWindowError,
    InvalidApprovalTransitionError,
    UnknownApprovalWindowError,
)

UTC_NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path, *, clock=None) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approval.sqlite3", clock=clock)


def _create(
    store: ApprovalStore,
    *,
    approval_id: str = "a1",
    roadmap_proposal_id: str = "rp-1",
    deadline_at: datetime,
    auto_approval_enabled: bool = True,
    created_at: datetime | None = None,
) -> ApprovalWindow:
    return store.create(
        approval_id=approval_id, project_id="p1", mvp_id="m1", planning_session_id="ps-1",
        roadmap_proposal_id=roadmap_proposal_id, deadline_at=deadline_at,
        auto_approval_enabled=auto_approval_enabled, created_at=created_at,
    )


class TestApprovalWindowValidation:
    def test_naive_created_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            ApprovalWindow(
                approval_id="a1", project_id="p1", mvp_id="m1", planning_session_id="ps-1",
                roadmap_proposal_id="rp-1", created_at=datetime(2026, 1, 1),
                deadline_at=UTC_NOW, auto_approval_enabled=True,
            )

    def test_naive_deadline_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            ApprovalWindow(
                approval_id="a1", project_id="p1", mvp_id="m1", planning_session_id="ps-1",
                roadmap_proposal_id="rp-1", created_at=UTC_NOW,
                deadline_at=datetime(2026, 1, 1), auto_approval_enabled=True,
            )


class TestApprovalPolicy:
    def test_rejects_non_positive_window(self) -> None:
        with pytest.raises(ValueError):
            ApprovalPolicy(window_seconds=0)

    def test_default_window_is_twenty_minutes(self) -> None:
        assert ApprovalPolicy().window_seconds == DEFAULT_WINDOW_SECONDS == 1200.0


class TestApprovalStoreCrud:
    def test_create_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        window = _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        fetched = store.get("a1")
        assert fetched == window
        assert fetched.status is ApprovalStatus.AWAITING_APPROVAL

    def test_unknown_window_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownApprovalWindowError):
            store.get("nope")

    def test_duplicate_approval_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        with pytest.raises(DuplicateApprovalWindowError):
            _create(store, roadmap_proposal_id="rp-2", deadline_at=UTC_NOW + timedelta(minutes=20))

    def test_duplicate_roadmap_proposal_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, approval_id="a1", roadmap_proposal_id="rp-1", deadline_at=UTC_NOW + timedelta(minutes=20))
        with pytest.raises(DuplicateApprovalWindowError):
            _create(store, approval_id="a2", roadmap_proposal_id="rp-1", deadline_at=UTC_NOW + timedelta(minutes=20))

    def test_get_for_proposal_returns_none_when_absent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_for_proposal("nope") is None

    def test_get_for_proposal_finds_the_window(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        window = _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        assert store.get_for_proposal("rp-1") == window

    def test_state_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "approval.sqlite3"
        store = ApprovalStore(db_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        store.close()

        reopened = ApprovalStore(db_path)
        fetched = reopened.get("a1")
        assert fetched.roadmap_proposal_id == "rp-1"


class TestApprovalStoreDecide:
    def test_approve_marks_terminal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        decided = store.decide(
            "a1", status=ApprovalStatus.APPROVED, reason="looks good", decided_at=UTC_NOW + timedelta(minutes=5)
        )
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.decision_reason == "looks good"
        assert decided.decided_at == UTC_NOW + timedelta(minutes=5)

    def test_cannot_decide_twice(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        store.decide("a1", status=ApprovalStatus.REJECTED, reason="no")
        with pytest.raises(InvalidApprovalTransitionError):
            store.decide("a1", status=ApprovalStatus.APPROVED, reason="actually yes")

    def test_decide_rejects_non_terminal_status(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        with pytest.raises(ValueError):
            store.decide("a1", status=ApprovalStatus.AWAITING_APPROVAL, reason="nope")

    def test_require_due_rejects_early_auto_approval(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        with pytest.raises(ApprovalNotYetDueError):
            store.decide(
                "a1", status=ApprovalStatus.AUTO_APPROVED, reason="elapsed",
                decided_at=UTC_NOW + timedelta(minutes=5), require_due=True,
            )

    def test_require_due_allows_decision_exactly_at_deadline(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        deadline = UTC_NOW + timedelta(minutes=20)
        _create(store, deadline_at=deadline)
        decided = store.decide(
            "a1", status=ApprovalStatus.AUTO_APPROVED, reason="elapsed", decided_at=deadline, require_due=True
        )
        assert decided.status is ApprovalStatus.AUTO_APPROVED

    def test_explicit_decisions_never_require_due(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        decided = store.decide(
            "a1", status=ApprovalStatus.APPROVED, reason="early approval",
            decided_at=UTC_NOW + timedelta(minutes=1),
        )
        assert decided.status is ApprovalStatus.APPROVED

    def test_resolved_window_kept_forever(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        store.decide("a1", status=ApprovalStatus.REJECTED, reason="pass", decided_at=UTC_NOW + timedelta(minutes=1))
        fetched = store.get("a1")
        assert fetched.status is ApprovalStatus.REJECTED
        assert fetched.decision_reason == "pass"


class TestApprovalStoreListDueAndNextDueAt:
    def test_list_due_before_deadline_is_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        assert store.list_due(UTC_NOW) == []

    def test_list_due_after_deadline_reports_the_window(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        due = store.list_due(UTC_NOW + timedelta(minutes=25))
        assert [w.approval_id for w in due] == ["a1"]

    def test_next_due_at_picks_earliest_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, approval_id="a1", roadmap_proposal_id="rp-1", deadline_at=UTC_NOW + timedelta(minutes=30))
        _create(store, approval_id="a2", roadmap_proposal_id="rp-2", deadline_at=UTC_NOW + timedelta(minutes=10))
        assert store.next_due_at() == UTC_NOW + timedelta(minutes=10)

    def test_next_due_at_none_when_nothing_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.next_due_at() is None

    def test_decided_window_never_reported_as_due(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        store.decide("a1", status=ApprovalStatus.APPROVED, reason="ok", decided_at=UTC_NOW + timedelta(minutes=1))
        assert store.list_due(UTC_NOW + timedelta(hours=1)) == []
        assert store.next_due_at() is None

    def test_restart_case_deadline_reached_while_process_was_stopped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "approval.sqlite3"
        store = ApprovalStore(db_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        store.close()  # simulates the orchestrator process stopping

        reopened = ApprovalStore(db_path)  # simulates the restart, well past the deadline
        due = reopened.list_due(UTC_NOW + timedelta(hours=1))
        assert [w.approval_id for w in due] == ["a1"]


class TestApprovalStoreMarkNotified:
    def test_mark_notified_records_timestamp(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        updated = store.mark_notified("a1", notified_at=UTC_NOW + timedelta(seconds=1))
        assert updated.notified_at == UTC_NOW + timedelta(seconds=1)
        assert updated.notification_error is None

    def test_mark_notified_records_error_without_raising(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store, deadline_at=UTC_NOW + timedelta(minutes=20))
        updated = store.mark_notified("a1", notified_at=UTC_NOW, error="smtp down")
        assert updated.notification_error == "smtp down"
        # still pending/awaiting — a failed notification never blocks the deadline
        assert store.get("a1").status is ApprovalStatus.AWAITING_APPROVAL


def _recording_notifier(calls: list):
    def notifier(window: ApprovalWindow, body: str) -> None:
        calls.append((window.approval_id, body))

    return notifier


def _raising_notifier(message: str):
    def notifier(window: ApprovalWindow, body: str) -> None:
        raise RuntimeError(message)

    return notifier


class TestApprovalCoordinatorOpenWindow:
    def test_opens_a_window_with_deadline_from_policy(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(window_seconds=1200.0), clock=lambda: UTC_NOW, id_factory=lambda: "a1"
        )
        window = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        assert window.deadline_at == UTC_NOW + timedelta(minutes=20)
        assert window.auto_approval_enabled is True
        assert window.status is ApprovalStatus.AWAITING_APPROVAL

    def test_disabled_policy_freezes_auto_approval_enabled_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=False), clock=lambda: UTC_NOW, id_factory=lambda: "a1"
        )
        window = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        assert window.auto_approval_enabled is False

    def test_open_window_is_idempotent_for_same_proposal(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        ids = iter(["a1", "a2"])
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: next(ids))
        first = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        second = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        assert first == second
        assert first.approval_id == "a1"

    def test_notifier_called_and_notified_at_recorded(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        calls: list = []
        coordinator = ApprovalCoordinator(
            store, clock=lambda: UTC_NOW, id_factory=lambda: "a1", notifier=_recording_notifier(calls)
        )
        window = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1",
            notification_body="a new roadmap proposal is ready",
        )
        assert calls == [("a1", "a new roadmap proposal is ready")]
        assert window.notified_at == UTC_NOW
        assert window.notification_error is None

    def test_notifier_failure_does_not_block_window_creation(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(
            store, clock=lambda: UTC_NOW, id_factory=lambda: "a1", notifier=_raising_notifier("webhook down")
        )
        window = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        assert window.status is ApprovalStatus.AWAITING_APPROVAL
        assert window.notification_error == "webhook down"
        assert store.get("a1").deadline_at == window.deadline_at

    def test_no_notifier_configured_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        window = coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        assert window.notified_at is None
        assert window.notification_error is None


class TestApprovalCoordinatorDecisions:
    def test_approve_is_immediate_even_before_deadline(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        approved = coordinator.approve("a1", reason="ship it")
        assert approved.status is ApprovalStatus.APPROVED
        assert approved.decision_reason == "ship it"

    def test_reject_records_reason(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        rejected = coordinator.reject("a1", reason="too risky")
        assert rejected.status is ApprovalStatus.REJECTED
        assert rejected.decision_reason == "too risky"

    def test_request_modification_records_distinct_status(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        modified = coordinator.request_modification("a1", reason="split the MVP in two")
        assert modified.status is ApprovalStatus.MODIFY_REQUESTED
        assert modified.decision_reason == "split the MVP in two"

    def test_cannot_approve_after_already_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        coordinator.reject("a1", reason="no")
        with pytest.raises(InvalidApprovalTransitionError):
            coordinator.approve("a1")


class TestApprovalCoordinatorResolveDue:
    def test_resolve_due_is_empty_before_deadline(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        assert coordinator.resolve_due() == []
        assert store.get("a1").status is ApprovalStatus.AWAITING_APPROVAL

    def test_resolve_due_auto_approves_past_deadline(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")

        later_coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW + timedelta(minutes=21))
        resolved = later_coordinator.resolve_due()

        assert [w.approval_id for w in resolved] == ["a1"]
        assert resolved[0].status is ApprovalStatus.AUTO_APPROVED
        assert store.get("a1").status is ApprovalStatus.AUTO_APPROVED

    def test_resolve_due_survives_restart(self, tmp_path: Path) -> None:
        # 10:00 proposal ready -> window opens, deadline 10:20; process
        # stops and only restarts at 11:00 — resolve_due must immediately
        # auto-approve, no polling/sleep needed in between.
        db_path = tmp_path / "approval.sqlite3"
        store = ApprovalStore(db_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        store.close()  # simulates the orchestrator process stopping

        reopened_store = ApprovalStore(db_path)
        restarted_coordinator = ApprovalCoordinator(reopened_store, clock=lambda: UTC_NOW + timedelta(hours=1))
        resolved = restarted_coordinator.resolve_due()
        assert [w.approval_id for w in resolved] == ["a1"]
        assert resolved[0].status is ApprovalStatus.AUTO_APPROVED

    def test_resolve_due_never_auto_approves_when_policy_disabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=False), clock=lambda: UTC_NOW, id_factory=lambda: "a1"
        )
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")

        later_coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=False), clock=lambda: UTC_NOW + timedelta(hours=1)
        )
        assert later_coordinator.resolve_due() == []
        assert store.get("a1").status is ApprovalStatus.AWAITING_APPROVAL

    def test_policy_change_after_creation_never_retroactively_enables_a_window(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        disabled_coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=False), clock=lambda: UTC_NOW, id_factory=lambda: "a1"
        )
        disabled_coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )

        # a later coordinator instance with auto-approval turned back on
        # must still respect what was frozen at creation time.
        now_enabled_coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=True), clock=lambda: UTC_NOW + timedelta(hours=1)
        )
        assert now_enabled_coordinator.resolve_due() == []

    def test_already_decided_window_is_never_resolved_again(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: "a1")
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        coordinator.approve("a1")

        later_coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW + timedelta(hours=1))
        assert later_coordinator.resolve_due() == []
        assert store.get("a1").status is ApprovalStatus.APPROVED

    def test_resolve_due_orders_by_deadline(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        ids = iter(["a1", "a2"])
        coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW, id_factory=lambda: next(ids))
        coordinator.open_window(project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1")
        later_open_coordinator = ApprovalCoordinator(
            store, clock=lambda: UTC_NOW + timedelta(minutes=1), id_factory=lambda: next(ids)
        )
        later_open_coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-2"
        )

        far_future_coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW + timedelta(hours=1))
        resolved = far_future_coordinator.resolve_due()
        assert [w.approval_id for w in resolved] == ["a1", "a2"]

    def test_list_due_excludes_disabled_windows_but_next_due_at_does_not(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: UTC_NOW)
        disabled_coordinator = ApprovalCoordinator(
            store, policy=ApprovalPolicy(enabled=False), clock=lambda: UTC_NOW, id_factory=lambda: "a1"
        )
        disabled_coordinator.open_window(
            project_id="p1", mvp_id="m1", planning_session_id="ps-1", roadmap_proposal_id="rp-1"
        )
        later_coordinator = ApprovalCoordinator(store, clock=lambda: UTC_NOW + timedelta(hours=1))
        assert later_coordinator.list_due() == []
        assert later_coordinator.next_due_at() == UTC_NOW + timedelta(minutes=20)
