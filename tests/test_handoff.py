"""Tests for HandoffStore / HandoffRecord (Phase 1 / Slice 7).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.handoff import (
    CorruptHandoffRecordError,
    DuplicateHandoffError,
    HandoffStore,
    UnknownHandoffError,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
NAIVE_NOW = datetime(2026, 9, 12, 15, 0)


def _store(tmp_path: Path, *, clock=None) -> HandoffStore:
    return HandoffStore(tmp_path / "handoff.sqlite3", clock=clock or (lambda: UTC_NOW))


def _create(store: HandoffStore, **overrides):
    fields = dict(
        handoff_id="ho-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1",
        objective="Ship the thing",
    )
    fields.update(overrides)
    return store.create(**fields)


class TestCreateAndRead:
    def test_create_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create(store)

        fetched = store.get("ho-1")
        assert fetched == record
        assert fetched.created_at == UTC_NOW

    def test_created_at_must_be_timezone_aware(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="timezone-aware"):
            _create(store, created_at=NAIVE_NOW)

    def test_optional_fields_default_to_none_or_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create(store)

        assert record.execution_id is None
        assert record.worker_id is None
        assert record.completed_work is None
        assert record.decisions is None
        assert record.files_touched == ()
        assert record.tests_run == ()
        assert record.test_results is None
        assert record.open_issues is None
        assert record.risks is None
        assert record.next_action is None
        assert record.git_sha_after is None

    def test_full_fields_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create(
            store,
            execution_id="exec-1",
            worker_id="claude_dev_01",
            completed_work="Implemented X",
            decisions="Chose approach Y",
            files_touched=["a.py", "b.py"],
            tests_run=["tests/test_a.py"],
            test_results="all green",
            open_issues="none",
            risks="none",
            next_action="proceed",
            git_sha_after="abc123",
        )

        fetched = store.get("ho-1")
        assert fetched == record
        assert fetched.files_touched == ("a.py", "b.py")
        assert fetched.tests_run == ("tests/test_a.py",)

    def test_unknown_handoff_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownHandoffError):
            store.get("does-not-exist")

    def test_duplicate_handoff_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store)
        with pytest.raises(DuplicateHandoffError):
            _create(store)


class TestMultipleHandoffsPerWorkItem:
    def test_several_handoffs_for_same_work_item_are_all_listed(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = _store(tmp_path, clock=lambda: clock_value["now"])

        _create(store, handoff_id="ho-1")
        clock_value["now"] = UTC_NOW + timedelta(minutes=5)
        _create(store, handoff_id="ho-2")

        handoffs = store.list_for_work_item("wi-1")
        assert [h.handoff_id for h in handoffs] == ["ho-1", "ho-2"]

    def test_latest_for_work_item_returns_most_recent(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = _store(tmp_path, clock=lambda: clock_value["now"])

        _create(store, handoff_id="ho-1")
        clock_value["now"] = UTC_NOW + timedelta(minutes=5)
        _create(store, handoff_id="ho-2")

        latest = store.latest_for_work_item("wi-1")
        assert latest.handoff_id == "ho-2"

    def test_latest_for_work_item_with_no_handoffs_is_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.latest_for_work_item("does-not-exist") is None


class TestPersistenceAcrossRestart:
    def test_handoff_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "handoff.sqlite3"
        store = HandoffStore(db_path, clock=lambda: UTC_NOW)
        _create(store, next_action="proceed to next work item")
        store.close()

        reopened = HandoffStore(db_path, clock=lambda: UTC_NOW)
        fetched = reopened.get("ho-1")
        assert fetched.next_action == "proceed to next work item"
        assert reopened.latest_for_work_item("wi-1").handoff_id == "ho-1"


class TestCorruptData:
    def test_corrupt_files_touched_json_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store)
        store._conn.execute(  # noqa: SLF001 - deliberately corrupting data for the test
            "UPDATE handoffs SET files_touched = 'not-json' WHERE handoff_id = ?", ("ho-1",)
        )
        store._conn.commit()

        with pytest.raises(CorruptHandoffRecordError):
            store.get("ho-1")

    def test_corrupt_created_at_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create(store)
        store._conn.execute(
            "UPDATE handoffs SET created_at = 'not-a-timestamp' WHERE handoff_id = ?", ("ho-1",)
        )
        store._conn.commit()

        with pytest.raises(CorruptHandoffRecordError):
            store.get("ho-1")
