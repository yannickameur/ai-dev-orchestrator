"""Tests for WaitRecord / WaitStore / WaitCoordinator (Phase 1 / Slice 11).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, an
injectable clock, no network, no subprocess, no Claude/Codex/Ralph
invocation, no real sleep anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.wait import (
    DuplicateWaitError,
    InvalidWaitTransitionError,
    UnknownWaitError,
    WaitCoordinator,
    WaitPhase,
    WaitReason,
    WaitRecord,
    WaitStatus,
    WaitStore,
)
from orchestrator.worker_selector import ProviderSelectionDiagnostic

UTC_NOW = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path, *, clock=None) -> WaitStore:
    return WaitStore(tmp_path / "wait.sqlite3", clock=clock)


class TestWaitRecordValidation:
    def test_naive_created_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            WaitRecord(
                wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
                phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
                created_at=datetime(2026, 1, 1), eligible_at=UTC_NOW,
            )

    def test_naive_eligible_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            WaitRecord(
                wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
                phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
                created_at=UTC_NOW, eligible_at=datetime(2026, 1, 1),
            )


class TestWaitStoreCrud:
    def test_create_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3), providers=("anthropic",),
        )
        fetched = store.get("w1")
        assert fetched == record
        assert fetched.status is WaitStatus.PENDING
        assert fetched.providers == ("anthropic",)

    def test_unknown_wait_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownWaitError):
            store.get("nope")

    def test_duplicate_wait_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        with pytest.raises(DuplicateWaitError):
            store.create(
                wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
                phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
                eligible_at=UTC_NOW + timedelta(hours=1),
            )

    def test_historical_waits_are_never_deleted_after_resolution(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        store.resolve("w1", resolved_at=UTC_NOW + timedelta(hours=2), resolution="resumed")
        # still retrievable, in full, forever — this is the audit trail.
        resolved = store.get("w1")
        assert resolved.status is WaitStatus.RESOLVED
        assert resolved.resolution == "resumed"
        assert resolved.resolved_at == UTC_NOW + timedelta(hours=2)

    def test_cannot_resolve_twice(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        store.resolve("w1", resolved_at=UTC_NOW, resolution="resumed")
        with pytest.raises(InvalidWaitTransitionError):
            store.resolve("w1", resolved_at=UTC_NOW, resolution="resumed again")

    def test_state_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "wait.sqlite3"
        store = WaitStore(db_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.REVIEW, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1), providers=("openai",),
        )
        store.close()

        reopened = WaitStore(db_path)
        fetched = reopened.get("w1")
        assert fetched.phase is WaitPhase.REVIEW
        assert fetched.providers == ("openai",)


class TestListDueAndNextDueAt:
    def test_list_due_before_deadline_is_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3),
        )
        assert store.list_due(UTC_NOW) == []

    def test_list_due_after_deadline_reports_the_wait(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3),
        )
        due = store.list_due(UTC_NOW + timedelta(hours=3, minutes=5))
        assert [w.wait_id for w in due] == ["w1"]

    def test_next_due_at_picks_the_earliest_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=5),
        )
        store.create(
            wait_id="w2", project_id="p1", mvp_id="m1", work_item_id="wi2",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        assert store.next_due_at() == UTC_NOW + timedelta(hours=1)

    def test_next_due_at_is_none_when_nothing_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.next_due_at() is None

    def test_resolved_wait_never_reported_as_due(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi1",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        store.resolve("w1", resolved_at=UTC_NOW + timedelta(hours=2), resolution="resumed")
        assert store.list_due(UTC_NOW + timedelta(hours=5)) == []
        assert store.next_due_at() is None

    def test_restart_case_deadline_reached_while_process_was_stopped(self, tmp_path: Path) -> None:
        # 18:00 WorkItem A -> WAITING_RESET, eligible_at=21:05; the process
        # then stops and is only restarted at 22:00 — list_due at 22:00
        # must immediately report the wait as due, no polling/sleep needed.
        db_path = tmp_path / "wait.sqlite3"
        store = WaitStore(db_path)
        store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3, minutes=5), created_at=UTC_NOW,
        )
        store.close()  # simulates the orchestrator process stopping

        reopened = WaitStore(db_path)  # simulates the restart at 22:00
        due = reopened.list_due(UTC_NOW + timedelta(hours=4))
        assert [w.wait_id for w in due] == ["w1"]


def _diag(provider: str, *, available: bool, reason: str, reset_at=()) -> ProviderSelectionDiagnostic:
    return ProviderSelectionDiagnostic(provider=provider, available=available, reason=reason, reset_at=reset_at)


class TestWaitCoordinatorNextQuotaReset:
    def test_no_invented_deadline_when_no_reset_known(self, tmp_path: Path) -> None:
        coordinator = WaitCoordinator(_store(tmp_path))
        diagnostics = (_diag("anthropic", available=False, reason="quota_exhausted", reset_at=()),)
        assert coordinator.next_quota_reset(diagnostics) is None

    def test_probe_error_is_not_quota_exhausted(self, tmp_path: Path) -> None:
        coordinator = WaitCoordinator(_store(tmp_path))
        diagnostics = (_diag("anthropic", available=False, reason="probe_error"),)
        assert coordinator.next_quota_reset(diagnostics) is None

    def test_earliest_reset_among_multiple_candidates(self, tmp_path: Path) -> None:
        coordinator = WaitCoordinator(_store(tmp_path))
        later = UTC_NOW + timedelta(hours=5)
        earlier = UTC_NOW + timedelta(hours=2)
        diagnostics = (
            _diag("anthropic", available=False, reason="quota_exhausted", reset_at=(later,)),
            _diag("openai", available=False, reason="quota_exhausted", reset_at=(earlier,)),
        )
        assert coordinator.next_quota_reset(diagnostics) == earlier

    def test_unavailable_reason_without_reset_does_not_count(self, tmp_path: Path) -> None:
        coordinator = WaitCoordinator(_store(tmp_path))
        diagnostics = (_diag("anthropic", available=False, reason="unknown"),)
        assert coordinator.next_quota_reset(diagnostics) is None


class TestWaitCoordinatorRecordWait:
    def test_records_wait_when_quota_reset_known(self, tmp_path: Path) -> None:
        wait_store = _store(tmp_path, clock=lambda: UTC_NOW)
        coordinator = WaitCoordinator(wait_store, clock=lambda: UTC_NOW, id_factory=lambda: "w1")
        reset_at = UTC_NOW + timedelta(hours=3)
        diagnostics = (_diag("anthropic", available=False, reason="quota_exhausted", reset_at=(reset_at,)),)

        record = coordinator.record_wait(
            project_id="p1", mvp_id="m1", work_item_id="wi-a", phase=WaitPhase.DEVELOPMENT,
            diagnostics=diagnostics,
        )

        assert record is not None
        assert record.eligible_at == reset_at
        assert record.providers == ("anthropic",)
        assert record.reason is WaitReason.QUOTA_RESET
        assert wait_store.get("w1") == record

    def test_returns_none_when_no_reliable_reset(self, tmp_path: Path) -> None:
        coordinator = WaitCoordinator(_store(tmp_path))
        diagnostics = (_diag("anthropic", available=False, reason="probe_error"),)

        record = coordinator.record_wait(
            project_id="p1", mvp_id="m1", work_item_id="wi-a", phase=WaitPhase.DEVELOPMENT,
            diagnostics=diagnostics,
        )

        assert record is None

    def test_find_due_returns_none_before_deadline(self, tmp_path: Path) -> None:
        wait_store = _store(tmp_path)
        coordinator = WaitCoordinator(wait_store, clock=lambda: UTC_NOW)
        wait_store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3),
        )
        assert coordinator.find_due("m1") is None

    def test_find_due_returns_the_wait_once_due(self, tmp_path: Path) -> None:
        wait_store = _store(tmp_path)
        later_clock = {"now": UTC_NOW + timedelta(hours=4)}
        coordinator = WaitCoordinator(wait_store, clock=lambda: later_clock["now"])
        wait_store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=3),
        )
        found = coordinator.find_due("m1")
        assert found is not None
        assert found.wait_id == "w1"

    def test_find_due_scoped_to_mvp(self, tmp_path: Path) -> None:
        wait_store = _store(tmp_path)
        coordinator = WaitCoordinator(wait_store, clock=lambda: UTC_NOW + timedelta(hours=4))
        wait_store.create(
            wait_id="w1", project_id="p1", mvp_id="other-mvp", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        assert coordinator.find_due("m1") is None

    def test_resolve_marks_wait_resolved(self, tmp_path: Path) -> None:
        wait_store = _store(tmp_path)
        coordinator = WaitCoordinator(wait_store, clock=lambda: UTC_NOW + timedelta(hours=4))
        wait_store.create(
            wait_id="w1", project_id="p1", mvp_id="m1", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )
        resolved = coordinator.resolve("w1", resolution="resumed with a new worker")
        assert resolved.status is WaitStatus.RESOLVED
        assert resolved.resolution == "resumed with a new worker"
