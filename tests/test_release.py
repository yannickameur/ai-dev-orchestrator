"""Tests for ReleaseStore / ReleaseRecord / ReleaseCheck / ReleaseGateResult (Slice 10).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.release import (
    CorruptReleaseRecordError,
    DuplicateReleaseError,
    ReleaseCheck,
    ReleaseGateResult,
    ReleaseGateStatus,
    ReleaseRecord,
    ReleaseStore,
    UnknownReleaseError,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
NAIVE_NOW = datetime(2026, 9, 12, 15, 0)


def _store(tmp_path: Path) -> ReleaseStore:
    return ReleaseStore(tmp_path / "release.sqlite3", clock=lambda: UTC_NOW)


def _record(**overrides) -> ReleaseRecord:
    fields = dict(
        release_id="rel-1", project_id="proj-1", mvp_id="mvp-1",
        started_at=UTC_NOW, finished_at=UTC_NOW, status=ReleaseGateStatus.PASSED,
        checks=(ReleaseCheck(check_id="all-work-items-completed", passed=True, summary="3/3 completed"),),
    )
    fields.update(overrides)
    return ReleaseRecord(**fields)


class TestReleaseCheckAndGateResult:
    def test_release_gate_result_passed_property(self) -> None:
        result = ReleaseGateResult(
            status=ReleaseGateStatus.PASSED,
            checks=(ReleaseCheck(check_id="x", passed=True, summary="ok"),),
        )
        assert result.passed is True

    def test_release_gate_result_failed_property(self) -> None:
        result = ReleaseGateResult(
            status=ReleaseGateStatus.FAILED,
            checks=(ReleaseCheck(check_id="x", passed=False, summary="nope"),),
        )
        assert result.passed is False

    def test_error_status_is_never_passed(self) -> None:
        result = ReleaseGateResult(status=ReleaseGateStatus.ERROR, checks=())
        assert result.passed is False


class TestReleaseRecordValidation:
    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _record(started_at=NAIVE_NOW)

    def test_empty_checks_allowed(self) -> None:
        record = _record(checks=())
        assert record.checks == ()


class TestReleaseStoreCreateAndRead:
    def test_record_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        release = _record()
        store.record(release)

        fetched = store.get("rel-1")
        assert fetched == release

    def test_unknown_release_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownReleaseError):
            store.get("does-not-exist")

    def test_duplicate_release_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record())
        with pytest.raises(DuplicateReleaseError):
            store.record(_record())

    def test_checks_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        checks = (
            ReleaseCheck(check_id="all-work-items-completed", passed=True, summary="3/3", related_ids=()),
            ReleaseCheck(check_id="quality-gates-satisfied", passed=False, summary="1 failing", related_ids=("wi-2",)),
        )
        store.record(_record(status=ReleaseGateStatus.FAILED, checks=checks))

        fetched = store.get("rel-1")
        assert fetched.checks == checks


class TestMultipleAttempts:
    def test_several_attempts_for_same_mvp_are_all_kept(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = ReleaseStore(tmp_path / "release.sqlite3", clock=lambda: clock_value["now"])
        store.record(_record(release_id="rel-1", status=ReleaseGateStatus.FAILED))
        clock_value["now"] = UTC_NOW + timedelta(hours=1)
        store.record(_record(release_id="rel-2", status=ReleaseGateStatus.PASSED))

        releases = store.list_for_mvp("mvp-1")
        assert [r.release_id for r in releases] == ["rel-1", "rel-2"]

    def test_latest_for_mvp(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = ReleaseStore(tmp_path / "release.sqlite3", clock=lambda: clock_value["now"])
        store.record(_record(release_id="rel-1", status=ReleaseGateStatus.FAILED))
        clock_value["now"] = UTC_NOW + timedelta(hours=1)
        store.record(_record(release_id="rel-2", status=ReleaseGateStatus.PASSED))

        latest = store.latest_for_mvp("mvp-1")
        assert latest.release_id == "rel-2"

    def test_latest_for_mvp_absent_is_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.latest_for_mvp("does-not-exist") is None

    def test_failed_attempt_is_never_overwritten(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record(release_id="rel-1", status=ReleaseGateStatus.FAILED))
        store.record(_record(release_id="rel-2", status=ReleaseGateStatus.PASSED))

        assert store.get("rel-1").status is ReleaseGateStatus.FAILED
        assert len(store.list_for_mvp("mvp-1")) == 2


class TestPersistenceAcrossRestart:
    def test_release_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "release.sqlite3"
        store = ReleaseStore(db_path, clock=lambda: UTC_NOW)
        store.record(_record())
        store.close()

        reopened = ReleaseStore(db_path, clock=lambda: UTC_NOW)
        assert reopened.get("rel-1").status is ReleaseGateStatus.PASSED


class TestCorruptData:
    def test_corrupt_status_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record())
        store._conn.execute(  # noqa: SLF001 - deliberately corrupting data for the test
            "UPDATE releases SET status = 'not_a_status' WHERE release_id = ?", ("rel-1",)
        )
        store._conn.commit()

        with pytest.raises(CorruptReleaseRecordError):
            store.get("rel-1")
