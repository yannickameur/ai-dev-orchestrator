"""Tests for ReviewStore / ReviewRecord / ReviewPolicy / parse_findings (Slice 9).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.review import (
    CorruptReviewRecordError,
    DuplicateReviewError,
    ReviewFinding,
    ReviewPolicy,
    ReviewRecord,
    ReviewStatus,
    ReviewStore,
    UnknownReviewError,
    parse_findings,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
NAIVE_NOW = datetime(2026, 9, 12, 15, 0)


def _store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)


def _record(**overrides) -> ReviewRecord:
    fields = dict(
        review_id="rev-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1",
        author_execution_id="exec-a", author_worker_id="claude_dev_01",
        started_at=UTC_NOW, finished_at=UTC_NOW, status=ReviewStatus.APPROVED,
        reviewer_execution_id="exec-r", reviewer_worker_id="codex_dev_01",
        reviewer_provider="openai", reviewer_model="gpt-5.6-terra",
    )
    fields.update(overrides)
    return ReviewRecord(**fields)


class TestReviewRecordInvariants:
    def test_reviewer_equal_to_author_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="never review its own work"):
            _record(reviewer_worker_id="claude_dev_01")  # same as author_worker_id

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _record(started_at=NAIVE_NOW)

    def test_reviewer_fields_optional(self) -> None:
        record = _record(
            reviewer_execution_id=None, reviewer_worker_id=None,
            reviewer_provider=None, reviewer_model=None, status=ReviewStatus.ERROR,
        )
        assert record.reviewer_worker_id is None


class TestParseFindings:
    def test_plain_string_payload_yields_one_finding(self) -> None:
        findings = parse_findings("add(a,b) subtracts instead of adding", lambda: "f-1")
        assert len(findings) == 1
        assert findings[0].summary == "add(a,b) subtracts instead of adding"
        assert findings[0].severity == "unknown"

    def test_none_payload_yields_no_findings(self) -> None:
        assert parse_findings(None, lambda: "f-1") == ()

    def test_empty_string_payload_yields_no_findings(self) -> None:
        assert parse_findings("", lambda: "f-1") == ()

    def test_structured_json_array_yields_multiple_findings(self) -> None:
        payload = (
            '[{"summary": "off by one", "severity": "major", "file_path": "a.py", "line": 12}, '
            '{"summary": "missing docstring", "severity": "minor"}]'
        )
        counter = iter(["f-1", "f-2"])
        findings = parse_findings(payload, lambda: next(counter))

        assert len(findings) == 2
        assert findings[0].summary == "off by one"
        assert findings[0].severity == "major"
        assert findings[0].file_path == "a.py"
        assert findings[0].line == 12
        assert findings[1].summary == "missing docstring"

    def test_invalid_json_falls_back_to_plain_string(self) -> None:
        findings = parse_findings("{not valid json", lambda: "f-1")
        assert len(findings) == 1
        assert findings[0].summary == "{not valid json"

    def test_json_object_not_array_falls_back_to_plain_string(self) -> None:
        findings = parse_findings('{"summary": "x"}', lambda: "f-1")
        assert len(findings) == 1
        assert findings[0].summary == '{"summary": "x"}'


class TestReviewPolicy:
    def test_default_max_review_cycles(self) -> None:
        assert ReviewPolicy().max_review_cycles == 3

    @pytest.mark.parametrize("bad_value", [0, -1, "3", 3.5])
    def test_invalid_max_review_cycles_rejected(self, bad_value) -> None:
        with pytest.raises((ValueError, TypeError)):
            ReviewPolicy(max_review_cycles=bad_value)


class TestReviewStoreCreateAndRead:
    def test_record_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        review = _record()
        store.record(review)

        fetched = store.get("rev-1")
        assert fetched == review

    def test_unknown_review_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownReviewError):
            store.get("does-not-exist")

    def test_duplicate_review_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record())
        with pytest.raises(DuplicateReviewError):
            store.record(_record())

    def test_findings_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        findings = (
            ReviewFinding(finding_id="f-1", summary="issue one", severity="major"),
            ReviewFinding(finding_id="f-2", summary="issue two", severity="minor", file_path="x.py", line=5),
        )
        review = _record(status=ReviewStatus.REJECTED, findings=findings)
        store.record(review)

        fetched = store.get("rev-1")
        assert fetched.findings == findings


class TestReviewStoreHistory:
    def test_several_reviews_for_same_work_item_are_listed_oldest_first(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: clock_value["now"])
        store.record(_record(review_id="rev-1", status=ReviewStatus.REJECTED))
        clock_value["now"] = UTC_NOW + timedelta(minutes=5)
        store.record(_record(review_id="rev-2", status=ReviewStatus.APPROVED))

        reviews = store.list_for_work_item("wi-1")
        assert [r.review_id for r in reviews] == ["rev-1", "rev-2"]

    def test_latest_for_work_item(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: clock_value["now"])
        store.record(_record(review_id="rev-1", status=ReviewStatus.REJECTED))
        clock_value["now"] = UTC_NOW + timedelta(minutes=5)
        store.record(_record(review_id="rev-2", status=ReviewStatus.APPROVED))

        latest = store.latest_for_work_item("wi-1")
        assert latest.review_id == "rev-2"

    def test_latest_for_work_item_absent_is_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.latest_for_work_item("does-not-exist") is None

    def test_count_for_work_item(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record(review_id="rev-1"))
        store.record(_record(review_id="rev-2"))
        assert store.count_for_work_item("wi-1") == 2
        assert store.count_for_work_item("does-not-exist") == 0


class TestPersistenceAcrossRestart:
    def test_reviews_and_findings_survive_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "review.sqlite3"
        store = ReviewStore(db_path, clock=lambda: UTC_NOW)
        findings = (ReviewFinding(finding_id="f-1", summary="fix this"),)
        store.record(_record(status=ReviewStatus.REJECTED, findings=findings))
        store.close()

        reopened = ReviewStore(db_path, clock=lambda: UTC_NOW)
        fetched = reopened.get("rev-1")
        assert fetched.status is ReviewStatus.REJECTED
        assert fetched.findings == findings
        assert reopened.count_for_work_item("wi-1") == 1


class TestCorruptData:
    def test_corrupt_status_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_record())
        store._conn.execute(  # noqa: SLF001 - deliberately corrupting data for the test
            "UPDATE reviews SET status = 'not_a_status' WHERE review_id = ?", ("rev-1",)
        )
        store._conn.commit()

        with pytest.raises(CorruptReviewRecordError):
            store.get("rev-1")
