"""Tests for realization reports (Phase 1 / Slice 18).

All tests are offline: real sqlite3-backed stores under pytest's
``tmp_path``, no network, no subprocess, no Claude/Codex/Ralph/LLM
invocation anywhere in this file — ``render_html`` is a pure, structural
projection, never LLM-authored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.activity_report import ActivityReport, ActivityReportStore, ActivitySummary
from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.handoff import HandoffStore
from orchestrator.project_state import ProjectStateStore
from orchestrator.realization_report import (
    DuplicateRealizationReportError,
    RealizationReportService,
    RealizationReportStatus,
    RealizationReportStore,
    UnknownRealizationReportError,
    compute_html_hash,
    write_html,
)
from orchestrator.review import ReviewFinding, ReviewRecord, ReviewStatus, ReviewStore
from orchestrator.validation import ValidationCommand, ValidationKind, ValidationStatus, ValidationStore
from orchestrator.wait import WaitPhase, WaitReason, WaitStore

UTC_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


def _stores(tmp_path: Path):
    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3", clock=lambda: UTC_NOW)
    handoff_store = HandoffStore(tmp_path / "handoff.sqlite3", clock=lambda: UTC_NOW)
    report_store = RealizationReportStore(tmp_path / "reports.sqlite3", clock=lambda: UTC_NOW)
    return project_store, execution_store, handoff_store, report_store


def _seed_work_item(project_store: ProjectStateStore, tmp_path: Path, **overrides) -> None:
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")
    fields = dict(
        work_item_id="wi-a", mvp_id="mvp-1", title="Fix add()",
        acceptance_criteria=("add(2,3) == 5",), required_capabilities=("development",),
    )
    fields.update(overrides)
    project_store.create_work_item(**fields)


def _service(project_store, execution_store, handoff_store, report_store, **kwargs) -> RealizationReportService:
    return RealizationReportService(
        project_store, execution_store, handoff_store, report_store, clock=lambda: UTC_NOW,
        id_factory=_counting_id_factory(), **kwargs,
    )


def _counting_id_factory(prefix: str = "id"):
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return id_factory


class TestBasicAggregation:
    def test_report_built_from_stores(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.work_item_id == "wi-a"
        assert report.objective == "Fix add()"
        assert report.acceptance_criteria == ("add(2,3) == 5",)

    def test_executions_are_aggregated(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="alice", provider="anthropic",
            backend="claude_code", model="sonnet", role="developer", started_at=UTC_NOW,
        )
        execution_store.mark_succeeded("exec-1", git_sha_after="deadbeef")

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.executions) == 1
        assert report.executions[0].execution_id == "exec-1"
        assert report.executions[0].status == ExecutionStatus.SUCCEEDED.value
        assert report.git_sha_final == "deadbeef"

    def test_recommendations_are_aggregated(self, tmp_path: Path) -> None:
        from orchestrator.complexity_estimation import ExecutionRecommendation, ExecutionRecommendationStore
        from orchestrator.worker_selector import QualityTier

        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        recommendation_store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        recommendation_store.record(ExecutionRecommendation(
            recommendation_id="rec-1", project_id="proj-1", role="developer", estimator_worker_id="alice",
            estimator_execution_id="exec-est", estimator_profile_id="economy", task_fingerprint="fp-1",
            minimum_quality_tier=QualityTier.STANDARD, reasons=("small change",), created_at=UTC_NOW,
            work_item_id="wi-a",
        ))

        service = _service(project_store, execution_store, handoff_store, report_store, recommendation_store=recommendation_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.recommendations) == 1
        assert report.recommendations[0].minimum_quality_tier == "STANDARD"

    def test_adaptive_decisions_are_aggregated(self, tmp_path: Path) -> None:
        from orchestrator.adaptive_execution import AdaptiveExecutionDecision, AdaptiveExecutionDecisionStore
        from orchestrator.worker_selector import QualityTier

        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        decision_store = AdaptiveExecutionDecisionStore(tmp_path / "dec.sqlite3", clock=lambda: UTC_NOW)
        decision_store.record(AdaptiveExecutionDecision(
            decision_id="dec-1", recommendation_id="rec-1", project_id="proj-1", role="developer",
            worker_id="alice", provider="anthropic", backend="claude_code", profile_id="standard",
            quality_tier=QualityTier.STANDARD, model="sonnet", created_at=UTC_NOW, work_item_id="wi-a",
        ))

        service = _service(project_store, execution_store, handoff_store, report_store, decision_store=decision_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.decisions) == 1
        assert report.decisions[0].worker_id == "alice"

    def test_handoffs_are_aggregated(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Fix add()", next_action="continue", created_at=UTC_NOW,
        )

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.handoffs) == 1
        assert report.handoffs[0].handoff_id == "h1"

    def test_validations_are_aggregated(self, tmp_path: Path) -> None:
        from orchestrator.validation import ValidationResult

        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        validation_store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
        validation_store.record_result(
            "run-1", "proj-1",
            ValidationResult(
                validation_run_id="run-1", validation_id="unit-tests", kind=ValidationKind.UNIT_TEST,
                required=True, argv=("true",), status=ValidationStatus.PASSED,
                started_at=UTC_NOW, finished_at=UTC_NOW, exit_code=0,
            ),
            work_item_id="wi-a",
        )

        service = _service(project_store, execution_store, handoff_store, report_store, validation_store=validation_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.validations) == 1
        assert report.validations[0].status == "passed"

    def test_reviews_are_optionally_aggregated(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        review_store = ReviewStore(tmp_path / "review.sqlite3", clock=lambda: UTC_NOW)
        review_store.record(ReviewRecord(
            review_id="rev-1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            author_execution_id="exec-1", author_worker_id="alice", started_at=UTC_NOW, finished_at=UTC_NOW,
            status=ReviewStatus.APPROVED, reviewer_worker_id="victor",
        ))

        service = _service(project_store, execution_store, handoff_store, report_store, review_store=review_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.reviews) == 1
        assert report.reviews[0].status == "approved"

    def test_reviews_absent_when_no_review_store_configured(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")
        assert report.reviews == ()

    def test_waits_are_optionally_aggregated(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        wait_store = WaitStore(tmp_path / "wait.sqlite3", clock=lambda: UTC_NOW)
        wait_store.create(
            wait_id="w1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            phase=WaitPhase.DEVELOPMENT, reason=WaitReason.QUOTA_RESET,
            eligible_at=UTC_NOW + timedelta(hours=1),
        )

        service = _service(project_store, execution_store, handoff_store, report_store, wait_store=wait_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert len(report.waits) == 1
        assert report.waits[0].wait_id == "w1"


class TestTimeline:
    def test_timeline_is_sorted_chronologically(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        execution_store.create(
            execution_id="exec-1", task_id="wi-a", worker_id="alice", provider="anthropic",
            backend="claude_code", model="sonnet", role="developer", started_at=UTC_NOW,
        )
        execution_store.mark_succeeded("exec-1", finished_at=UTC_NOW + timedelta(minutes=5))
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Fix add()", created_at=UTC_NOW + timedelta(minutes=10),
        )

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        timestamps = [t.timestamp for t in report.timeline]
        assert timestamps == sorted(timestamps)
        assert [t.entry_type for t in report.timeline] == ["execution_started", "execution_finished", "handoff"]


class TestReportStatusAndLifecycle:
    def test_intermediate_report_is_handoff_ready(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Fix add()", next_action="continue from here", created_at=UTC_NOW,
        )

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.report_status is RealizationReportStatus.HANDOFF_READY

    def test_final_report_is_completed(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        project_store.mark_work_item_completed("wi-a")

        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.report_status is RealizationReportStatus.COMPLETED
        assert report.final_work_item_status == "completed"

    def test_reports_are_insert_only(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        with pytest.raises(DuplicateRealizationReportError):
            report_store.record(report)

    def test_readable_after_restart(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")
        report_store.close()

        reopened = RealizationReportStore(tmp_path / "reports.sqlite3")
        assert reopened.get(report.report_id) == report

    def test_unknown_report_raises(self, tmp_path: Path) -> None:
        _, _, _, report_store = _stores(tmp_path)
        with pytest.raises(UnknownRealizationReportError):
            report_store.get("nope")

    def test_multiple_reports_for_same_work_item_are_all_kept(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)

        first = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")
        second = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        reports = report_store.list_for_work_item("wi-a")
        assert {r.report_id for r in reports} == {first.report_id, second.report_id}

    def test_intermediate_report_never_overwritten_by_final(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        project_store.refresh_readiness("mvp-1")
        project_store.mark_work_item_running("wi-a")
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Fix add()", created_at=UTC_NOW,
        )
        service = _service(project_store, execution_store, handoff_store, report_store)
        intermediate = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        project_store.mark_work_item_completed("wi-a")
        final = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report_store.get(intermediate.report_id).report_status is RealizationReportStatus.HANDOFF_READY
        assert report_store.get(final.report_id).report_status is RealizationReportStatus.COMPLETED
        assert intermediate.report_id != final.report_id


class TestHtmlRendering:
    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        html_1 = report.render_html()
        html_2 = report.render_html()
        assert html_1 == html_2
        assert compute_html_hash(html_1) == compute_html_hash(html_2)

    def test_html_escaping(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path, title="<script>alert(1)</script>")
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        html = report.render_html()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_no_external_dependencies(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        html = report.render_html()
        for forbidden in ("http://", "https://", "<script src", "cdn."):
            assert forbidden not in html

    def test_write_html_is_atomic_and_readable(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        output_path = tmp_path / "reports" / "realizations" / "wi-a" / f"{report.report_id}.html"
        written = write_html(report, output_path)

        assert written == output_path
        assert output_path.read_text(encoding="utf-8") == report.render_html()
        leftovers = list((tmp_path / "reports" / "realizations" / "wi-a").glob(".*.tmp"))
        assert leftovers == []

    def test_status_visible_immediately(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        html = report.render_html()
        # the status badge appears near the very top of the document.
        assert html.index("status-badge") < html.index("<h2>A. Objective</h2>")


class TestNoSecretsIntroduced:
    def test_render_never_adds_data_beyond_the_snapshot(self, tmp_path: Path) -> None:
        # A report built from facts containing no secret-looking strings
        # must never introduce one — the renderer is a pure projection.
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        html = report.render_html()
        for forbidden in ("api_key", "Authorization:", "Bearer ", "secret", "password"):
            assert forbidden.lower() not in html.lower()

    def test_no_llm_invocation_in_render_path(self) -> None:
        import inspect

        from orchestrator import realization_report as module

        source = inspect.getsource(module._render_html) + inspect.getsource(module._build_summary_text)
        for forbidden in ("RalphExecutionEngine", "WorkerSelector", "subprocess"):
            assert forbidden not in source


class TestActivityReportUnaffected:
    def test_activity_report_store_still_works(self, tmp_path: Path) -> None:
        store = ActivityReportStore(tmp_path / "activity.sqlite3", clock=lambda: UTC_NOW)
        report = ActivityReport(
            report_id="ar-1", project_id="proj-1", mvp_id="mvp-1", release_id="rel-1",
            generated_at=UTC_NOW, mvp_objective="Ship it", mvp_final_status="released",
            summary=ActivitySummary(
                work_item_count=1, execution_count=1, validation_count=0, review_count=0,
                rework_count=0, failure_count=0, interruption_count=0,
            ),
        )
        store.record(report)
        assert store.get("ar-1") == report


class TestHandoffRemainsCanonical:
    def test_handoff_store_is_the_source_the_report_reads_never_writes(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        handoff_store.create(
            handoff_id="h1", project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a",
            objective="Fix add()", created_at=UTC_NOW,
        )
        service = _service(project_store, execution_store, handoff_store, report_store)
        service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        # Exactly the one handoff the test itself created — generating a
        # report never writes a new handoff.
        assert len(handoff_store.list_for_work_item("wi-a")) == 1
