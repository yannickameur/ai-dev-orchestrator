"""Tests for applying a decided RoadmapProposal + starting the next MVP
(Phase 1 / Slice 14).

All tests are offline: real sqlite3 files under pytest's ``tmp_path``, a
real (temporary) ROADMAP.md file, injectable clock/id_factory, no
network, no subprocess, no Claude/Codex/Ralph/Ralph-execution invocation
anywhere in this file. Any use of ``MVPManager`` is a fake stub.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.approval import ApprovalStatus, ApprovalStore
from orchestrator.planning import (
    PlanningSession,
    PlanningSessionStatus,
    PlanningSnapshot,
    PlanningStore,
    ProposedWorkItem,
    RoadmapChange,
    RoadmapChangeType,
    RoadmapProposal,
)
from orchestrator.project_state import ProjectStateStore
from orchestrator.roadmap_application import (
    DecisionNotAuthorizedError,
    RoadmapApplication,
    RoadmapApplicationFailedError,
    RoadmapApplicationInProgressError,
    RoadmapApplicationService,
    RoadmapApplicationStatus,
    RoadmapApplicationStore,
    RoadmapConflictError,
    render_roadmap_content,
)

UTC_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

ROADMAP_TEXT = (
    "# ROADMAP — AI Dev Orchestrator\n\n"
    "## Slice 12 — DONE\n\n"
    "Some human-authored history that must never be touched.\n"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _counting_id_factory(prefix: str = "id"):
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return id_factory


def _work_items() -> tuple[ProposedWorkItem, ...]:
    return (
        ProposedWorkItem(
            title="Design store", objective="Design the schema",
            acceptance_criteria=("schema documented",), required_capabilities=("developer",),
        ),
        ProposedWorkItem(
            title="Wire service", objective="Wire it up", dependencies=("Design store",),
            acceptance_criteria=("service wired",), required_capabilities=("developer",),
        ),
    )


def _proposal(
    *, roadmap_proposal_id: str = "rp-1", next_mvp_work_items: tuple[ProposedWorkItem, ...] | None = None,
) -> RoadmapProposal:
    return RoadmapProposal(
        roadmap_proposal_id=roadmap_proposal_id, planning_session_id="ps-1", snapshot_id="snap-1",
        synthesizer_worker_id="worker-1", synthesizer_execution_id="exec-1", created_at=UTC_NOW,
        source_proposal_ids=("pp-1", "pp-2"), next_mvp_objective="Ship the next slice",
        next_mvp_rationale="closes the loop", next_mvp_work_items=_work_items() if next_mvp_work_items is None else next_mvp_work_items,
        next_mvp_acceptance_criteria=("MVP N+1 works end to end",), next_mvp_deferred_items=("Nice to have",),
        roadmap_changes=(
            RoadmapChange(change_type=RoadmapChangeType.KEEP, item_reference="Slice 12", reason="still valid"),
            RoadmapChange(change_type=RoadmapChangeType.ADD, item_reference="Slice 14", reason="close the loop", target_mvp="mvp-2"),
            RoadmapChange(change_type=RoadmapChangeType.MOVE, item_reference="Slice 15", reason="reordered", target_mvp="mvp-3"),
            RoadmapChange(change_type=RoadmapChangeType.DROP, item_reference="Old idea", reason="obsolete"),
        ),
        risks=("scope risk",), agreements=("agree on scope",), disagreements=(), rationale="synthesis rationale",
    )


class FakeMVPManager:
    def __init__(self, result: object = "ran-next-work-item") -> None:
        self.calls: list[str] = []
        self._result = result

    async def run_next_work_item(self, mvp_id: str):
        self.calls.append(mvp_id)
        return self._result


def _seed(
    tmp_path: Path, *, roadmap_text: str = ROADMAP_TEXT, approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
    proposal: RoadmapProposal | None = None,
) -> tuple[ProjectStateStore, PlanningStore, ApprovalStore, RoadmapApplicationStore, RoadmapProposal]:
    (tmp_path / "ROADMAP.md").write_text(roadmap_text)

    project_store = ProjectStateStore(tmp_path / "project.sqlite3", clock=lambda: UTC_NOW)
    project_store.create_project(project_id="proj-1", name="Demo", workspace=tmp_path)
    project_store.create_mvp(mvp_id="mvp-1", project_id="proj-1", objective="Ship it")

    planning_store = PlanningStore(tmp_path / "planning.sqlite3", clock=lambda: UTC_NOW)
    planning_store.record_snapshot(
        PlanningSnapshot(
            snapshot_id="snap-1", project_id="proj-1", source_mvp_id="mvp-1", source_release_id="release-1",
            created_at=UTC_NOW, roadmap_content=roadmap_text, roadmap_hash=_sha256(roadmap_text),
            activity_report_id="report-1",
        )
    )
    planning_store.create_session(
        PlanningSession(
            planning_session_id="ps-1", project_id="proj-1", mvp_id="mvp-1", release_id="release-1",
            snapshot_id="snap-1", created_at=UTC_NOW, status=PlanningSessionStatus.PROPOSAL_READY,
            policy_planner_count=2,
        )
    )
    proposal = proposal or _proposal()
    planning_store.record_roadmap_proposal(proposal)

    approval_store = ApprovalStore(tmp_path / "approval.sqlite3", clock=lambda: UTC_NOW)
    if approval_status is not None:
        approval_store.create(
            approval_id="appr-1", project_id="proj-1", mvp_id="mvp-1", planning_session_id="ps-1",
            roadmap_proposal_id=proposal.roadmap_proposal_id, deadline_at=UTC_NOW,
            auto_approval_enabled=True, created_at=UTC_NOW,
        )
        if approval_status is not ApprovalStatus.AWAITING_APPROVAL:
            approval_store.decide("appr-1", status=approval_status, reason="test", decided_at=UTC_NOW)

    application_store = RoadmapApplicationStore(tmp_path / "application.sqlite3", clock=lambda: UTC_NOW)
    return project_store, planning_store, approval_store, application_store, proposal


def _service(
    project_store, planning_store, approval_store, application_store, *, id_prefix: str = "gen"
) -> RoadmapApplicationService:
    return RoadmapApplicationService(
        application_store, planning_store, approval_store, project_store,
        clock=lambda: UTC_NOW, id_factory=_counting_id_factory(id_prefix),
    )


def _run(coro):
    return asyncio.run(coro)


class TestAuthorizedDecisions:
    def test_approved_is_applicable(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=ApprovalStatus.APPROVED
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.application.status is RoadmapApplicationStatus.APPLIED
        assert outcome.mvp.objective == "Ship the next slice"

    def test_auto_approved_is_applicable(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=ApprovalStatus.AUTO_APPROVED
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.application.status is RoadmapApplicationStatus.APPLIED
        assert outcome.application.decision_status == "auto_approved"


class TestUnauthorizedDecisions:
    def test_rejected_causes_no_mutation(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=ApprovalStatus.REJECTED
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(DecisionNotAuthorizedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT
        assert application_store.list_for_proposal(proposal.roadmap_proposal_id) == []
        with pytest.raises(Exception):
            project_store.get_mvp("gen-1")

    def test_modify_requested_causes_no_mutation(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=ApprovalStatus.MODIFY_REQUESTED
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(DecisionNotAuthorizedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT
        assert application_store.list_for_proposal(proposal.roadmap_proposal_id) == []

    def test_awaiting_approval_causes_no_mutation(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=ApprovalStatus.AWAITING_APPROVAL
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(DecisionNotAuthorizedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT
        assert application_store.list_for_proposal(proposal.roadmap_proposal_id) == []

    def test_no_approval_window_at_all_causes_no_mutation(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, approval_status=None
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(DecisionNotAuthorizedError) as excinfo:
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert excinfo.value.status is None
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT


class TestRoadmapHashGuard:
    def test_matching_hash_continues(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.application.status is RoadmapApplicationStatus.APPLIED

    def test_stale_hash_yields_conflict_and_creates_no_mvp(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        (tmp_path / "ROADMAP.md").write_text(ROADMAP_TEXT + "\nSomeone edited this by hand.\n")
        service = _service(project_store, planning_store, approval_store, application_store)

        with pytest.raises(RoadmapConflictError) as excinfo:
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))

        application = excinfo.value.application
        assert application.status is RoadmapApplicationStatus.CONFLICT
        assert "STALE_PROPOSAL" in application.error
        assert application.target_mvp_id is None
        with pytest.raises(Exception):
            project_store.get_mvp("gen-1")


class TestDependencyValidation:
    def test_unknown_dependency_fails_closed(self, tmp_path: Path) -> None:
        bad_items = (
            ProposedWorkItem(title="A", objective="do a", dependencies=("Nonexistent",)),
        )
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, proposal=_proposal(next_mvp_work_items=bad_items)
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(RoadmapApplicationFailedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT

    def test_dependency_cycle_fails_closed(self, tmp_path: Path) -> None:
        cyclic_items = (
            ProposedWorkItem(title="A", objective="do a", dependencies=("B",)),
            ProposedWorkItem(title="B", objective="do b", dependencies=("A",)),
        )
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, proposal=_proposal(next_mvp_work_items=cyclic_items)
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(RoadmapApplicationFailedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert (tmp_path / "ROADMAP.md").read_text() == ROADMAP_TEXT

    def test_duplicate_titles_fail_closed(self, tmp_path: Path) -> None:
        dupe_items = (
            ProposedWorkItem(title="A", objective="do a"),
            ProposedWorkItem(title="A", objective="do a again"),
        )
        project_store, planning_store, approval_store, application_store, proposal = _seed(
            tmp_path, proposal=_proposal(next_mvp_work_items=dupe_items)
        )
        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(RoadmapApplicationFailedError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))


class TestMvpAndWorkItemCreation:
    def test_real_mvp_created_from_proposed_mvp(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.mvp.mvp_id not in (proposal.roadmap_proposal_id, "mvp-1")
        assert outcome.mvp.objective == proposal.next_mvp_objective
        assert tuple(outcome.mvp.acceptance_criteria) == proposal.next_mvp_acceptance_criteria

    def test_real_work_items_created(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        titles = sorted(wi.title for wi in outcome.work_items)
        assert titles == ["Design store", "Wire service"]

    def test_dependency_mapping_is_correct(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        by_title = {wi.title: wi for wi in outcome.work_items}
        design = by_title["Design store"]
        wire = by_title["Wire service"]
        assert wire.dependencies == frozenset({design.work_item_id})


class TestIdempotence:
    def test_second_call_for_same_proposal_is_not_duplicated(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        first = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        second = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))

        assert first.application.application_id == second.application.application_id
        assert first.mvp.mvp_id == second.mvp.mvp_id
        attempts = application_store.list_for_proposal(proposal.roadmap_proposal_id)
        assert len(attempts) == 1
        assert len(project_store.list_work_items(first.mvp.mvp_id)) == 2

    def test_restart_with_applied_row_is_not_reapplied(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        first = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))

        # simulate a fresh process: brand-new service instance over the same stores
        restarted_service = _service(project_store, planning_store, approval_store, application_store, id_prefix="restart")
        second = _run(restarted_service.apply_decided_proposal(proposal.roadmap_proposal_id))

        assert second.application.application_id == first.application.application_id
        assert len(application_store.list_for_proposal(proposal.roadmap_proposal_id)) == 1

    def test_applying_in_progress_is_never_blindly_replayed(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        approval = approval_store.get_for_proposal(proposal.roadmap_proposal_id)
        orphaned = RoadmapApplication(
            application_id="orphan-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
            source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=(("Design store", "wi-1"), ("Wire service", "wi-2")),
            created_at=UTC_NOW, status=RoadmapApplicationStatus.APPLYING,
            roadmap_hash_before=_sha256(ROADMAP_TEXT),
        )
        application_store.create(orphaned)

        service = _service(project_store, planning_store, approval_store, application_store)
        with pytest.raises(RoadmapApplicationInProgressError):
            _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))


class TestReconciliationAfterCrash:
    def test_reconcile_after_crash_following_roadmap_write(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        approval = approval_store.get_for_proposal(proposal.roadmap_proposal_id)
        work_item_map = (("Design store", "wi-1"), ("Wire service", "wi-2"))
        new_content = render_roadmap_content(
            ROADMAP_TEXT,
            [(
                RoadmapApplication(
                    application_id="app-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
                    approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
                    source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
                    decision_status=approval.status.value, decided_at=approval.decided_at,
                    work_item_map=work_item_map, created_at=UTC_NOW, status=RoadmapApplicationStatus.APPLYING,
                    roadmap_hash_before=_sha256(ROADMAP_TEXT),
                ),
                proposal,
            )],
        )
        applying = RoadmapApplication(
            application_id="app-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
            source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=work_item_map, created_at=UTC_NOW, status=RoadmapApplicationStatus.APPLYING,
            roadmap_hash_before=_sha256(ROADMAP_TEXT), roadmap_hash_after=_sha256(new_content),
        )
        application_store.create(applying)
        # simulate: the file write happened, then the process crashed
        # before creating the MVP/WorkItems.
        (tmp_path / "ROADMAP.md").write_text(new_content)

        service = _service(project_store, planning_store, approval_store, application_store)
        reconciled = service.reconcile("app-1")

        assert reconciled.status is RoadmapApplicationStatus.APPLIED
        assert reconciled.finished_at is not None
        mvp = project_store.get_mvp("mvp-2")
        assert mvp.objective == proposal.next_mvp_objective
        work_items = project_store.list_work_items("mvp-2")
        assert {wi.title for wi in work_items} == {"Design store", "Wire service"}

    def test_reconcile_is_idempotent(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))

        reconciled_again = service.reconcile(outcome.application.application_id)
        assert reconciled_again == outcome.application  # already terminal, untouched

    def test_reconcile_when_mvp_already_created_before_crash(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        approval = approval_store.get_for_proposal(proposal.roadmap_proposal_id)
        work_item_map = (("Design store", "wi-1"), ("Wire service", "wi-2"))
        applying = RoadmapApplication(
            application_id="app-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
            source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=work_item_map, created_at=UTC_NOW, status=RoadmapApplicationStatus.APPLYING,
            roadmap_hash_before=_sha256(ROADMAP_TEXT),
        )
        new_content = render_roadmap_content(ROADMAP_TEXT, [(applying, proposal)])
        applying = replace(applying, roadmap_hash_after=_sha256(new_content))
        application_store.create(applying)
        (tmp_path / "ROADMAP.md").write_text(new_content)
        # simulate: roadmap written AND MVP/work items already created,
        # crash happened right before the row was marked APPLIED.
        project_store.create_mvp(mvp_id="mvp-2", project_id="proj-1", objective=proposal.next_mvp_objective)
        project_store.create_work_item(work_item_id="wi-1", mvp_id="mvp-2", title="Design store")
        project_store.create_work_item(work_item_id="wi-2", mvp_id="mvp-2", title="Wire service", dependencies=("wi-1",))

        service = _service(project_store, planning_store, approval_store, application_store)
        reconciled = service.reconcile("app-1")

        assert reconciled.status is RoadmapApplicationStatus.APPLIED
        assert len(project_store.list_work_items("mvp-2")) == 2  # no duplicates

    def test_reconcile_conflict_when_roadmap_changed_unexpectedly(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        approval = approval_store.get_for_proposal(proposal.roadmap_proposal_id)
        applying = RoadmapApplication(
            application_id="app-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
            source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=(("Design store", "wi-1"),), created_at=UTC_NOW,
            status=RoadmapApplicationStatus.APPLYING, roadmap_hash_before=_sha256(ROADMAP_TEXT),
            roadmap_hash_after=_sha256(ROADMAP_TEXT + "\nexpected new content\n"),
        )
        application_store.create(applying)
        (tmp_path / "ROADMAP.md").write_text(ROADMAP_TEXT + "\nsomething totally different happened\n")

        service = _service(project_store, planning_store, approval_store, application_store)
        reconciled = service.reconcile("app-1")

        assert reconciled.status is RoadmapApplicationStatus.CONFLICT
        with pytest.raises(Exception):
            project_store.get_mvp("mvp-2")


class TestAuditTrail:
    def test_roadmap_hash_after_is_persisted(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.application.roadmap_hash_after == _sha256((tmp_path / "ROADMAP.md").read_text())

    def test_application_is_fully_traceable(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        app = outcome.application
        assert app.roadmap_proposal_id == proposal.roadmap_proposal_id
        assert app.approval_id == "appr-1"
        assert app.source_release_id == "release-1"
        assert app.source_mvp_id == "mvp-1"
        assert app.decision_status == "approved"


class TestMvpManagerHandoff:
    def test_next_mvp_can_be_handed_to_mvp_manager(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        fake_manager = FakeMVPManager(result="first-work-item-started")

        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id, mvp_manager=fake_manager))

        assert fake_manager.calls == [outcome.mvp.mvp_id]
        assert outcome.run_result == "first-work-item-started"

    def test_no_mvp_manager_means_no_run_result(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        outcome = _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        assert outcome.run_result is None


class TestAtomicWriteAndDeterminism:
    def test_no_leftover_temp_files_after_apply(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        leftovers = glob.glob(str(tmp_path / ".ROADMAP.md.*.tmp"))
        assert leftovers == []

    def test_previous_human_content_preserved_verbatim(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        service = _service(project_store, planning_store, approval_store, application_store)
        _run(service.apply_decided_proposal(proposal.roadmap_proposal_id))
        new_text = (tmp_path / "ROADMAP.md").read_text()
        assert ROADMAP_TEXT in new_text
        assert new_text.index(ROADMAP_TEXT) == 0

    def test_render_roadmap_content_is_deterministic(self, tmp_path: Path) -> None:
        project_store, planning_store, approval_store, application_store, proposal = _seed(tmp_path)
        approval = approval_store.get_for_proposal(proposal.roadmap_proposal_id)
        application = RoadmapApplication(
            application_id="app-1", roadmap_proposal_id=proposal.roadmap_proposal_id,
            approval_id=approval.approval_id, planning_session_id="ps-1", project_id="proj-1",
            source_mvp_id="mvp-1", source_release_id="release-1", target_mvp_id="mvp-2",
            decision_status=approval.status.value, decided_at=approval.decided_at,
            work_item_map=(("Design store", "wi-1"), ("Wire service", "wi-2")), created_at=UTC_NOW,
            status=RoadmapApplicationStatus.APPLYING, roadmap_hash_before=_sha256(ROADMAP_TEXT),
        )
        first = render_roadmap_content(ROADMAP_TEXT, [(application, proposal)])
        second = render_roadmap_content(ROADMAP_TEXT, [(application, proposal)])
        assert first == second
        assert "## KEEP" in first and "## ADD" in first and "## MOVE" in first and "## DROP" in first
        assert "Slice 12" in first and "Slice 14" in first and "Slice 15" in first and "Old idea" in first


class TestNoGitNoLLM:
    def test_module_never_touches_git_or_llm_execution(self) -> None:
        module_path = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "roadmap_application.py"
        source = module_path.read_text()
        assert "subprocess" not in source
        assert "RalphExecutionEngine" not in source
        assert "WorkerSelector" not in source
