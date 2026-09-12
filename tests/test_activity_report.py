"""Tests for ActivityReport / ActivityReportStore / render_markdown (Slice 10).

All tests are offline: a real sqlite3 file under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere here.
No LLM is ever involved in producing the Markdown rendering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.activity_report import (
    ActivityReport,
    ActivityReportStore,
    ActivitySummary,
    CorruptActivityReportError,
    ExecutionSummary,
    HandoffSummary,
    Incident,
    ReleaseCheckSummary,
    ReviewSummary,
    UnknownActivityReportError,
    ValidationSummary,
    WorkItemSummary,
    render_markdown,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _summary(**overrides) -> ActivitySummary:
    fields = dict(
        work_item_count=1, execution_count=1, validation_count=1, review_count=1,
        rework_count=0, failure_count=0, interruption_count=0,
        workers_used=("claude_dev_01",), providers_used=("anthropic",), duration_seconds=42.0,
    )
    fields.update(overrides)
    return ActivitySummary(**fields)


def _report(**overrides) -> ActivityReport:
    fields = dict(
        report_id="rep-1", project_id="proj-1", mvp_id="mvp-1", release_id="rel-1",
        generated_at=UTC_NOW, mvp_objective="Ship it", mvp_final_status="released",
        summary=_summary(),
    )
    fields.update(overrides)
    return ActivityReport(**fields)


def _store(tmp_path: Path) -> ActivityReportStore:
    return ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)


class TestActivityReportValidation:
    def test_summary_required(self) -> None:
        with pytest.raises(TypeError, match="ActivitySummary"):
            ActivityReport(
                report_id="r", project_id="p", mvp_id="m", release_id="rel",
                generated_at=UTC_NOW, mvp_objective="X", mvp_final_status="released",
                summary="not-a-summary",  # type: ignore[arg-type]
            )

    def test_naive_generated_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _report(generated_at=datetime(2026, 9, 12, 15, 0))


class TestFullReportRoundTrip:
    def test_full_report_survives_store_and_reload(self, tmp_path: Path) -> None:
        report = _report(
            work_items=(
                WorkItemSummary(work_item_id="wi-1", title="Do X", status="completed", dependencies=(), acceptance_criteria=("must work",)),
            ),
            executions=(
                ExecutionSummary(
                    execution_id="exec-1", work_item_id="wi-1", worker_id="claude_dev_01",
                    provider="anthropic", backend="claude_code", model="sonnet", role="developer",
                    started_at=UTC_NOW, finished_at=UTC_NOW + timedelta(minutes=5), status="succeeded",
                    reasoning_effort=None, exit_code=0, git_sha_before="aaa", git_sha_after="bbb",
                    provider_session_id="sess-1", ralph_loop_id="loop-1",
                ),
            ),
            validations=(
                ValidationSummary(
                    validation_run_id="run-1", work_item_id="wi-1", validation_id="unit-tests",
                    kind="unit_test", required=True, status="passed", duration_ms=1234, exit_code=0,
                    git_sha="bbb",
                ),
            ),
            reviews=(
                ReviewSummary(
                    review_id="rev-1", work_item_id="wi-1", author_worker_id="claude_dev_01",
                    status="approved", cycle_number=1, reviewer_worker_id="codex_dev_01",
                    reviewer_provider="openai", reviewer_model="gpt-5.6-terra",
                    findings_summary=(), git_sha_reviewed="bbb",
                ),
            ),
            handoffs=(HandoffSummary(handoff_id="ho-1", work_item_id="wi-1", next_action="proceed"),),
            incidents=(Incident(work_item_id="wi-1", kind="none", summary="n/a"),),
            gate_checks=(ReleaseCheckSummary(check_id="all-work-items-completed", passed=True, summary="1/1"),),
        )
        store = _store(tmp_path)
        store.record(report)

        fetched = store.get("rep-1")
        assert fetched == report

    def test_unknown_report_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownActivityReportError):
            store.get("does-not-exist")


class TestLatestForMVP:
    def test_latest_for_mvp(self, tmp_path: Path) -> None:
        clock_value = {"now": UTC_NOW}
        store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: clock_value["now"])
        store.record(_report(report_id="rep-1", generated_at=UTC_NOW))
        clock_value["now"] = UTC_NOW + timedelta(hours=1)
        store.record(_report(report_id="rep-2", generated_at=UTC_NOW + timedelta(hours=1)))

        latest = store.latest_for_mvp("mvp-1")
        assert latest.report_id == "rep-2"

    def test_latest_for_mvp_absent_is_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.latest_for_mvp("does-not-exist") is None


class TestPersistenceAcrossRestart:
    def test_report_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "activity.sqlite3"
        store = ActivityReportStore(db_path, clock=lambda: UTC_NOW)
        store.record(_report())
        store.close()

        reopened = ActivityReportStore(db_path, clock=lambda: UTC_NOW)
        assert reopened.get("rep-1").mvp_objective == "Ship it"


class TestCorruptData:
    def test_corrupt_payload_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_report())
        store._conn.execute(  # noqa: SLF001 - deliberately corrupting data for the test
            "UPDATE activity_reports SET payload = 'not-json' WHERE report_id = ?", ("rep-1",)
        )
        store._conn.commit()

        with pytest.raises(CorruptActivityReportError):
            store.get("rep-1")


class TestRenderMarkdown:
    def test_markdown_is_deterministic(self) -> None:
        report = _report(
            work_items=(WorkItemSummary(work_item_id="wi-1", title="Do X", status="completed"),),
            gate_checks=(ReleaseCheckSummary(check_id="all-work-items-completed", passed=True, summary="1/1"),),
        )
        first = render_markdown(report)
        second = render_markdown(report)
        assert first == second

    def test_markdown_contains_key_sections(self) -> None:
        report = _report(
            work_items=(WorkItemSummary(work_item_id="wi-1", title="Do X", status="completed"),),
        )
        markdown = render_markdown(report)

        assert "# MVP mvp-1 — Release Report" in markdown
        assert "## Summary" in markdown
        assert "## WorkItems" in markdown
        assert "## Executions" in markdown
        assert "## Quality Gates" in markdown
        assert "## Reviews" in markdown
        assert "## Handoffs" in markdown
        assert "## Incidents" in markdown
        assert "## Release Gate" in markdown
        assert "wi-1" in markdown

    def test_no_incidents_renders_none_marker(self) -> None:
        report = _report(incidents=())
        markdown = render_markdown(report)
        assert "(none)" in markdown

    def test_markdown_never_calls_an_llm(self) -> None:
        import inspect

        from orchestrator import activity_report as module

        source = inspect.getsource(module.render_markdown)
        for forbidden in ("claude", "codex", "ralph", "Anthropic", "openai", "await "):
            assert forbidden not in source
