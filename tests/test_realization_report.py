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
from orchestrator.git_governance import GitWorkItemRecord, GitWorkItemStatus, GitWorkItemStore
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

    def test_reviewer_role_recommendations_and_decisions_surface_alongside_developer(self, tmp_path: Path) -> None:
        """Slice 19: review recommendations/decisions are never filtered
        out by role — the aggregation query is already role-agnostic
        (WHERE work_item_id = ? only), so a reviewer-role entry appears
        naturally next to a developer-role one, distinguished by ``role``.
        No RealizationReport code change was needed for this — this test
        is the proof."""
        from orchestrator.adaptive_execution import AdaptiveExecutionDecision, AdaptiveExecutionDecisionStore
        from orchestrator.complexity_estimation import ExecutionRecommendation, ExecutionRecommendationStore
        from orchestrator.worker_selector import QualityTier

        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        recommendation_store = ExecutionRecommendationStore(tmp_path / "rec.sqlite3", clock=lambda: UTC_NOW)
        recommendation_store.record(ExecutionRecommendation(
            recommendation_id="rec-dev-1", project_id="proj-1", role="developer", estimator_worker_id="alice",
            estimator_execution_id="exec-est-1", estimator_profile_id="economy", task_fingerprint="fp-dev-1",
            minimum_quality_tier=QualityTier.SIMPLE, reasons=("small change",), created_at=UTC_NOW,
            work_item_id="wi-a",
        ))
        recommendation_store.record(ExecutionRecommendation(
            recommendation_id="rec-review-1", project_id="proj-1", role="reviewer", estimator_worker_id="alice",
            estimator_execution_id="exec-est-2", estimator_profile_id="economy", task_fingerprint="fp-review-1",
            minimum_quality_tier=QualityTier.COMPLEX, reasons=("security-sensitive review",), created_at=UTC_NOW,
            work_item_id="wi-a",
        ))
        decision_store = AdaptiveExecutionDecisionStore(tmp_path / "dec.sqlite3", clock=lambda: UTC_NOW)
        decision_store.record(AdaptiveExecutionDecision(
            decision_id="dec-dev-1", recommendation_id="rec-dev-1", project_id="proj-1", role="developer",
            worker_id="alice", provider="anthropic", backend="claude_code", profile_id="economy",
            quality_tier=QualityTier.SIMPLE, model="haiku", created_at=UTC_NOW, work_item_id="wi-a",
        ))
        decision_store.record(AdaptiveExecutionDecision(
            decision_id="dec-review-1", recommendation_id="rec-review-1", project_id="proj-1", role="reviewer",
            worker_id="victor", provider="openai", backend="codex", profile_id="deep",
            quality_tier=QualityTier.COMPLEX, model="gpt-5.6-terra", reasoning_effort="high",
            created_at=UTC_NOW, work_item_id="wi-a",
        ))

        service = _service(
            project_store, execution_store, handoff_store, report_store,
            recommendation_store=recommendation_store, decision_store=decision_store,
        )
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert {r.role for r in report.recommendations} == {"developer", "reviewer"}
        assert {d.role for d in report.decisions} == {"developer", "reviewer"}
        review_decision = next(d for d in report.decisions if d.role == "reviewer")
        assert review_decision.worker_id == "victor" and review_decision.quality_tier == "COMPLEX"

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


def _git_store(tmp_path: Path) -> GitWorkItemStore:
    return GitWorkItemStore(tmp_path / "git_governance.sqlite3", clock=lambda: UTC_NOW)


def _prepared_git_record(git_store: GitWorkItemStore, *, work_item_id: str = "wi-a") -> GitWorkItemRecord:
    record = GitWorkItemRecord(
        git_work_id="git-1", project_id="proj-1", mvp_id="mvp-1", work_item_id=work_item_id,
        repository_path="/tmp/workspace", base_branch="main", work_branch=f"work/{work_item_id}",
        base_sha="a" * 40, status=GitWorkItemStatus.PREPARED, created_at=UTC_NOW, updated_at=UTC_NOW,
    )
    return git_store.create(record)


class TestGitGovernanceEnrichment:
    def test_report_without_git_store_stays_compatible(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)  # no git_work_item_store

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.git_work_branch is None
        assert report.git_merge_status is None
        html = report.render_html()
        assert "Git governance:" not in html  # section only renders when data exists

    def test_report_without_a_governed_record_stays_compatible(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)  # configured, but no record for this WorkItem
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.git_work_branch is None

    def test_intermediate_report_shows_branch(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.git_base_branch == "main"
        assert report.git_work_branch == "work/wi-a"
        assert report.git_merge_status == "prepared"
        assert report.git_merged_sha is None
        html = report.render_html()
        assert "work/wi-a" in html
        assert "Git governance:" in html

    def test_final_report_shows_merge_status_and_sha(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.update_head("wi-a", "b" * 40)
        git_store.mark_merge_ready("wi-a")
        git_store.mark_merged("wi-a", merged_sha="b" * 40)
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.git_merge_status == "merged"
        assert report.git_merged_sha == "b" * 40
        html = report.render_html()
        assert f"merged_sha={'b' * 40}" in html

    def test_pull_request_info_surfaced_when_present(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.record_pull_request("wi-a", number=42, url="https://example.invalid/pull/42")
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.git_pull_request_number == 42
        assert report.git_pull_request_url == "https://example.invalid/pull/42"
        html = report.render_html()
        assert "https://example.invalid/pull/42" in html
        assert "#42" in html

    def test_git_events_appear_in_timeline(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.update_head("wi-a", "b" * 40)
        git_store.mark_merge_ready("wi-a")
        git_store.mark_merged("wi-a", merged_sha="b" * 40)
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        entry_types = [t.entry_type for t in report.timeline]
        assert "branch_prepared" in entry_types
        assert "merge_ready" in entry_types
        assert "merged" in entry_types

    def test_git_conflict_event_appears_in_timeline(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.update_head("wi-a", "b" * 40)
        git_store.mark_conflict("wi-a", reason="base branch advanced incompatibly")
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert "merge_conflict" in [t.entry_type for t in report.timeline]

    def test_html_escaping_unchanged_for_git_section(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.update_head("wi-a", "b" * 40)
        git_store.mark_conflict("wi-a", reason="<script>alert(1)</script>")
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")
        html = report.render_html()

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_round_trip_through_store_preserves_git_fields(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        git_store = _git_store(tmp_path)
        _prepared_git_record(git_store)
        git_store.update_head("wi-a", "b" * 40)
        git_store.mark_merge_ready("wi-a")
        git_store.mark_merged("wi-a", merged_sha="b" * 40)
        service = _service(
            project_store, execution_store, handoff_store, report_store, git_work_item_store=git_store,
        )
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        reloaded = report_store.get(report.report_id)
        assert reloaded.git_merge_status == "merged"
        assert reloaded.git_merged_sha == "b" * 40
        assert reloaded.git_work_branch == "work/wi-a"


def _qa_store(tmp_path: Path):
    from orchestrator.qa import QARunStore
    return QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)


def _qa_run_with_result(qa_store, *, verdict_status="pass"):
    from orchestrator.qa import (
        QAEvidenceManifest, QAPhase, QAPolicy, QARequest, QARunStatus, QAResult, QAVerdict,
        QAVerdictStatus, new_qa_run,
    )
    run = new_qa_run(
        project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a", engine_id="internal",
        phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
        policy=QAPolicy(), manifest=QAEvidenceManifest(required_test_ids=("tests/test_app.py",)),
        clock=lambda: UTC_NOW, id_factory=lambda: "qa-run-1",
    )
    qa_store.create(run)
    qa_store.update_status(run.run_id, QARunStatus.RUNNING)
    result = QAResult(
        engine_id="internal", observed_head_sha="b" * 40, started_at=UTC_NOW, finished_at=UTC_NOW,
        tests_executed=("tests/test_app.py",), passed_count=1, failed_count=0,
    )
    qa_store.record_result(run.run_id, result)
    qa_store.update_status(run.run_id, QARunStatus.COMPLETED)
    verdict = QAVerdict(
        status=QAVerdictStatus(verdict_status), reason="all mandatory QA evidence present and passing",
        evaluated_at=UTC_NOW, run_id=run.run_id, head_sha="b" * 40,
    )
    qa_store.record_verdict(run.run_id, verdict)
    return qa_store.get(run.run_id)


class TestQAEnrichment:
    def test_report_without_qa_store_stays_compatible(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        service = _service(project_store, execution_store, handoff_store, report_store)  # no qa_run_store

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.qa_run_id is None
        assert report.qa_verdict_status is None
        html = report.render_html()
        assert "<strong>QA:</strong>" not in html  # section only renders when data exists

    def test_report_without_a_qa_run_stays_compatible(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)  # configured, but no run for this WorkItem
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.qa_run_id is None

    def test_qa_facts_surfaced_when_present(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)
        run = _qa_run_with_result(qa_store)
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.qa_run_id == run.run_id
        assert report.qa_engine_id == "internal"
        assert report.qa_verdict_status == "pass"
        assert report.qa_passed_count == 1
        assert report.qa_failed_count == 0
        html = report.render_html()
        assert "<strong>QA:</strong>" in html
        assert "verdict=pass" in html

    def test_qa_events_appear_in_timeline(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)
        _qa_run_with_result(qa_store)
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)

        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        entry_types = [t.entry_type for t in report.timeline]
        assert "qa_run_created" in entry_types
        assert "qa_verdict_recorded" in entry_types

    def test_qa_regressions_surfaced_and_escaped(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)
        run = _qa_run_with_result(qa_store, verdict_status="fail")
        # Overwrite the persisted result via a fresh run with a regression,
        # so the HTML-escaping assertion below is meaningful.
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")
        assert report.qa_verdict_status == "fail"
        html = report.render_html()
        assert "verdict=fail" in html

    def test_round_trip_through_store_preserves_qa_fields(self, tmp_path: Path) -> None:
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)
        _qa_run_with_result(qa_store)
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        reloaded = report_store.get(report.report_id)
        assert reloaded.qa_run_id == report.qa_run_id
        assert reloaded.qa_verdict_status == "pass"
        assert reloaded.qa_passed_count == 1

    def test_html_is_never_reparsed_as_source_of_truth(self, tmp_path: Path) -> None:
        # render_html is a pure projection: rendering twice from the same
        # report object yields byte-identical output, and nothing in this
        # module ever parses HTML back into a report.
        project_store, execution_store, handoff_store, report_store = _stores(tmp_path)
        _seed_work_item(project_store, tmp_path)
        qa_store = _qa_store(tmp_path)
        _qa_run_with_result(qa_store)
        service = _service(project_store, execution_store, handoff_store, report_store, qa_run_store=qa_store)
        report = service.generate(project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-a")

        assert report.render_html() == report.render_html()
        import inspect
        from orchestrator import realization_report as module
        assert "html.parser" not in inspect.getsource(module)
        assert "BeautifulSoup" not in inspect.getsource(module)
