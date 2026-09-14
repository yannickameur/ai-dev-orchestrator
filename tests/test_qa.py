"""Tests for the QA governance domain (Slice 22): contracts, persistence,
and the deterministic engine-result-vs-governed-verdict separation.

All tests are offline: no real TestSprite/Momentic/BrowserStack/Diffblue
call, no real Claude/Codex session, no network. ``TestReadOnlyFinalVerification``
uses a real temporary git repository (same pattern as
``tests/test_validation.py``) to reuse Slice 21.5's
``QualityGateRunner``/``verify_repository_unchanged`` primitive for real.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.qa import (
    DuplicateQARunError,
    FailureClassification,
    InvalidQARunTransitionError,
    QAEngine,
    QAEngineCapabilities,
    QAEvidenceManifest,
    QAPhase,
    QAPolicy,
    QARequest,
    QARun,
    QARunStatus,
    QARunStore,
    QAResult,
    QAVerdict,
    QAVerdictStatus,
    ResultAlreadyRecordedError,
    UnknownQARunError,
    VerdictAlreadyRecordedError,
    evaluate_qa_verdict,
    new_qa_run,
    run_final_verification_gate,
)

UTC_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PY = sys.executable


def _request(**overrides) -> QARequest:
    fields = dict(
        project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", workspace="/tmp/ws",
        base_sha="a" * 40, head_sha="b" * 40, objective="Fix add()",
    )
    fields.update(overrides)
    return QARequest(**fields)


def _result(**overrides) -> QAResult:
    fields = dict(
        engine_id="internal", observed_head_sha="b" * 40,
        started_at=UTC_NOW, finished_at=UTC_NOW + timedelta(seconds=5),
    )
    fields.update(overrides)
    return QAResult(**fields)


def _manifest(**overrides) -> QAEvidenceManifest:
    fields = dict(required_test_ids=("tests/test_x.py::test_add",))
    fields.update(overrides)
    return QAEvidenceManifest(**fields)


def _policy(**overrides) -> QAPolicy:
    fields: dict = {}
    fields.update(overrides)
    return QAPolicy(**fields)


def _store(tmp_path: Path) -> QARunStore:
    return QARunStore(tmp_path / "qa_runs.sqlite3", clock=lambda: UTC_NOW)


def _run(**overrides) -> QARun:
    fields = dict(
        project_id="proj-1", mvp_id="mvp-1", work_item_id="wi-1", engine_id="internal",
        phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
        policy=_policy(), manifest=_manifest(), clock=lambda: UTC_NOW, id_factory=lambda: "run-1",
    )
    fields.update(overrides)
    return new_qa_run(**fields)


# --- 1. QARequest validation --------------------------------------------


class TestQARequestValidation:
    def test_valid_request_accepted(self) -> None:
        req = _request()
        assert req.project_id == "proj-1"
        assert req.acceptance_criteria == ()

    def test_empty_head_sha_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _request(head_sha="")

    def test_tuple_fields_normalized(self) -> None:
        req = _request(changed_files=["a.py", "b.py"])
        assert req.changed_files == ("a.py", "b.py")

    def test_no_vendor_field_in_schema(self) -> None:
        # No provider-specific attribute exists on the dataclass at all.
        field_names = {f for f in QARequest.__dataclass_fields__}
        for forbidden in ("testsprite_token", "browserstack_project", "momentic_workspace", "api_key"):
            assert forbidden not in field_names


# --- 2. QAResult normalization -------------------------------------------


class TestQAResultNormalization:
    def test_defaults(self) -> None:
        result = _result()
        assert result.passed_count == 0
        assert result.failure_classifications == ()

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(passed_count=-1)

    def test_failure_classifications_must_be_enum(self) -> None:
        with pytest.raises(TypeError):
            _result(failure_classifications=("regression",))  # raw string, not enum

    def test_failure_classifications_accepts_enum(self) -> None:
        result = _result(failure_classifications=(FailureClassification.FLAKY_TEST,))
        assert result.failure_classifications == (FailureClassification.FLAKY_TEST,)


# --- 3. FailureClassification enum ---------------------------------------


class TestFailureClassificationEnum:
    def test_canonical_members(self) -> None:
        assert {c.value for c in FailureClassification} == {
            "regression", "expected_change", "test_defect", "flaky_test", "environment_failure", "unknown",
        }

    def test_unknown_stays_unknown(self) -> None:
        assert FailureClassification.UNKNOWN.value == "unknown"


# --- 4/5. QAVerdictStatus vs QARunStatus ---------------------------------


class TestVerdictAndRunStatusAreDistinct:
    def test_pass_fail_inconclusive(self) -> None:
        assert {s.value for s in QAVerdictStatus} == {"pass", "fail", "inconclusive"}

    def test_run_status_distinct_enum(self) -> None:
        assert QARunStatus is not QAVerdictStatus
        assert {s.value for s in QARunStatus} == {"created", "running", "completed", "failed", "interrupted"}

    def test_failed_run_never_directly_a_verdict_value(self) -> None:
        # QARunStatus.FAILED has no counterpart named FAILED on QAVerdictStatus.
        assert not hasattr(QAVerdictStatus, "FAILED")


# --- 6. Engine result is NEVER authoritative for the governed verdict ---


class TestEngineResultNeverAuthoritative:
    def test_engine_claims_pass_but_governed_verdict_is_fail_on_sha_mismatch(self) -> None:
        run = QARun(
            run_id="run-1", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        result = _result(observed_head_sha="c" * 40, engine_reported_status="PASS")
        verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL
        assert "observed_head_sha" in verdict.reason

    def test_engine_claims_fail_but_governed_verdict_is_pass_when_evidence_is_clean(self) -> None:
        run = QARun(
            run_id="run-1", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        result = _result(engine_reported_status="FAIL")  # engine's own opinion, ignored
        verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.PASS

    def test_verdict_independent_of_engine_reported_status_value(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        clean_evidence = dict(observed_head_sha="b" * 40)
        pass_claim = evaluate_qa_verdict(run=run, result=_result(engine_reported_status="PASS", **clean_evidence), now=UTC_NOW)
        fail_claim = evaluate_qa_verdict(run=run, result=_result(engine_reported_status="FAIL", **clean_evidence), now=UTC_NOW)
        no_claim = evaluate_qa_verdict(run=run, result=_result(engine_reported_status=None, **clean_evidence), now=UTC_NOW)
        # Identical underlying evidence -> identical governed verdict,
        # regardless of what the engine itself claimed.
        assert pass_claim.status == fail_claim.status == no_claim.status == QAVerdictStatus.PASS


# --- 7. Wrong SHA ----------------------------------------------------------


class TestWrongSha:
    def test_wrong_sha_never_passes(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(observed_head_sha="z" * 40), now=UTC_NOW)
        assert verdict.status is not QAVerdictStatus.PASS
        assert verdict.status is QAVerdictStatus.FAIL

    def test_sha_check_can_be_disabled_by_policy(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(require_exact_sha=False), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(observed_head_sha="z" * 40), now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.PASS


# --- 8. Empty mandatory evidence -----------------------------------------


class TestEmptyMandatoryEvidence:
    def test_empty_manifest_cannot_pass_when_required(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(require_nonempty_evidence=True), manifest=QAEvidenceManifest(),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL
        assert "evidence" in verdict.reason

    def test_empty_manifest_allowed_when_policy_does_not_require_evidence(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(require_nonempty_evidence=False), manifest=QAEvidenceManifest(),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.PASS


# --- 9. Unresolved regression => FAIL -------------------------------------


class TestUnresolvedRegression:
    def test_regression_present_fails(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        result = _result(regressions=("tests/test_add.py::test_add_regressed",))
        verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL

    def test_mandatory_invariant_failure_fails(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(required_invariant_ids=("INV-1",)), manifest=_manifest(),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        result = _result(failed_invariant_ids=("INV-1",))
        verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL

    def test_missing_required_engine_fails(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(required_engines=("external-x",)), manifest=_manifest(),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL


# --- 10. Infrastructure failure => INCONCLUSIVE ---------------------------


class TestInfrastructureFailureInconclusive:
    @pytest.mark.parametrize("status", [QARunStatus.FAILED, QARunStatus.INTERRUPTED, QARunStatus.CREATED, QARunStatus.RUNNING])
    def test_unfinished_or_failed_run_is_inconclusive(self, status: QARunStatus) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=status, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=None, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.INCONCLUSIVE
        assert verdict.status is not QAVerdictStatus.PASS

    def test_completed_with_no_result_is_inconclusive_not_pass(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=None, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.INCONCLUSIVE


# --- 11/12. Policy/manifest snapshot stable after config change -----------


class TestPolicyManifestSnapshotStable:
    def test_manifest_snapshot_persisted_unaffected_by_new_run_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(manifest=_manifest(required_test_ids=("t1",)))
        store.create(run)
        reloaded = store.get(run.run_id)
        assert reloaded.manifest.required_test_ids == ("t1",)
        # A hypothetical "current policy" elsewhere in the system changing
        # never retroactively touches this run's own persisted snapshot.
        different_manifest = _manifest(required_test_ids=("t1", "t2"))
        assert reloaded.manifest.required_test_ids != different_manifest.required_test_ids

    def test_policy_snapshot_persisted_unaffected_by_new_run_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(policy=_policy(max_qa_cycles=2))
        store.create(run)
        reloaded = store.get(run.run_id)
        assert reloaded.policy.max_qa_cycles == 2


# --- 13/14. QARun persistence + restart/reload -----------------------------


class TestQARunPersistence:
    def test_create_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        assert store.get(run.run_id).run_id == run.run_id

    def test_duplicate_run_id_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(_run())
        with pytest.raises(DuplicateQARunError):
            store.create(_run())

    def test_unknown_run_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownQARunError):
            store.get("does-not-exist")

    def test_restart_reload(self, tmp_path: Path) -> None:
        db_path = tmp_path / "qa.sqlite3"
        store = QARunStore(db_path, clock=lambda: UTC_NOW)
        run = new_qa_run(
            project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal", phase=QAPhase.FINAL_VERIFICATION,
            expected_base_sha="a" * 40, expected_head_sha="b" * 40, policy=_policy(), manifest=_manifest(),
            clock=lambda: UTC_NOW, id_factory=lambda: "run-restart",
        )
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        store.update_status(run.run_id, QARunStatus.COMPLETED)
        store.record_result(run.run_id, _result())
        verdict = QAVerdict(status=QAVerdictStatus.PASS, reason="ok", evaluated_at=UTC_NOW, run_id=run.run_id, head_sha="b" * 40)
        store.record_verdict(run.run_id, verdict)
        store.close()

        reopened = QARunStore(db_path, clock=lambda: UTC_NOW)
        reloaded = reopened.get("run-restart")
        assert reloaded.status is QARunStatus.COMPLETED
        assert reloaded.verdict is not None
        assert reloaded.verdict.status is QAVerdictStatus.PASS
        assert reopened.get_result("run-restart") is not None


# --- 15/16. Valid/invalid transitions --------------------------------------


class TestTransitions:
    def test_valid_transition_sequence(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        updated = store.update_status(run.run_id, QARunStatus.COMPLETED)
        assert updated.status is QARunStatus.COMPLETED

    def test_invalid_transition_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        with pytest.raises(InvalidQARunTransitionError):
            store.update_status(run.run_id, QARunStatus.COMPLETED)  # CREATED -> COMPLETED directly, invalid

    def test_terminal_status_has_no_further_transitions(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        store.update_status(run.run_id, QARunStatus.COMPLETED)
        with pytest.raises(InvalidQARunTransitionError):
            store.update_status(run.run_id, QARunStatus.RUNNING)


# --- 17. Historical run immutable facts -------------------------------------


class TestHistoricalRunImmutableFacts:
    def test_verdict_recorded_once(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        store.update_status(run.run_id, QARunStatus.COMPLETED)
        verdict = QAVerdict(status=QAVerdictStatus.PASS, reason="ok", evaluated_at=UTC_NOW)
        store.record_verdict(run.run_id, verdict)
        with pytest.raises(VerdictAlreadyRecordedError):
            store.record_verdict(run.run_id, verdict)

    def test_result_recorded_once(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.record_result(run.run_id, _result())
        with pytest.raises(ResultAlreadyRecordedError):
            store.record_result(run.run_id, _result())


# --- 18/19. engine_run_id / artifacts persisted -----------------------------


class TestEngineRunIdAndArtifactsPersisted:
    def test_engine_run_id_persisted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(engine_run_id="ext-run-123")
        store.create(run)
        assert store.get(run.run_id).engine_run_id == "ext-run-123"

    def test_artifacts_refs_persisted_in_result(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.record_result(run.run_id, _result(artifacts=("https://example.test/report/123",)))
        result = store.get_result(run.run_id)
        assert result is not None
        assert result.artifacts == ("https://example.test/report/123",)


# --- 20. No provider-specific schema anywhere -------------------------------


class TestNoProviderSpecificSchema:
    def test_no_vendor_field_in_any_contract_dataclass(self) -> None:
        vendor_substrings = ("testsprite", "browserstack", "momentic", "diffblue")
        for dataclass_type in (QARequest, QAResult, QAPolicy, QAEvidenceManifest, QARun, QAEngineCapabilities):
            for field_name in dataclass_type.__dataclass_fields__:
                lowered = field_name.lower()
                for forbidden in vendor_substrings:
                    assert forbidden not in lowered, f"{dataclass_type.__name__}.{field_name} looks vendor-specific"

    def test_engine_id_is_a_plain_abstract_string_not_a_vendor_enum(self) -> None:
        # engine_id accepts any caller-assigned string ("internal",
        # "external-fake", ...) — there is no closed vendor enum anywhere
        # in this module a real adapter would need to extend.
        run = _run(engine_id="whatever-a-future-adapter-calls-itself")
        assert run.engine_id == "whatever-a-future-adapter-calls-itself"


# --- 42. Mandatory empty evidence cannot PASS (integration through the store) --


class TestMandatoryEmptyEvidenceIntegration:
    def test_empty_manifest_run_never_persists_a_pass_verdict(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(manifest=QAEvidenceManifest(), policy=_policy(require_nonempty_evidence=True))
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        store.update_status(run.run_id, QARunStatus.COMPLETED)
        store.record_result(run.run_id, _result())
        reloaded = store.get(run.run_id)
        verdict = evaluate_qa_verdict(run=reloaded, result=store.get_result(run.run_id), now=UTC_NOW)
        assert verdict.status is not QAVerdictStatus.PASS
        store.record_verdict(run.run_id, verdict)
        assert store.get(run.run_id).verdict.status is QAVerdictStatus.FAIL


# --- 43/44/45/46. QA phases + read-only Final Verification ------------------


class TestQAPhases:
    def test_authoring_phase_represents_tests_added(self) -> None:
        result = _result(tests_added=("tests/test_new.py::test_case",))
        assert "tests/test_new.py::test_case" in result.tests_added

    def test_final_verification_mutation_prevents_pass(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(final_verification_read_only=True), manifest=_manifest(),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW, read_only_violation=True)
        assert verdict.status is QAVerdictStatus.FAIL

    def test_inability_to_prove_read_only_is_inconclusive_not_pass(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW, read_only_unprovable=True)
        assert verdict.status is QAVerdictStatus.INCONCLUSIVE
        assert verdict.status is not QAVerdictStatus.PASS

    def test_read_only_violation_is_fail_not_inconclusive(self) -> None:
        run = QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=_result(), now=UTC_NOW, read_only_violation=True)
        assert verdict.status is QAVerdictStatus.FAIL


class TestReadOnlyFinalVerificationRealGitRepo:
    """Reuses Slice 21.5's QualityGateRunner/verify_repository_unchanged
    against a real temporary git repository — same pattern as
    tests/test_validation.py."""

    @staticmethod
    def _init_repo(tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)

    def test_clean_run_is_not_a_violation_and_provable(self, tmp_path: Path) -> None:
        from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore

        self._init_repo(tmp_path)
        store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
        store.set_project_commands("proj-1", [ValidationCommand(validation_id="a", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"), required=True)])
        runner = QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1")

        violation, unprovable = asyncio.run(
            run_final_verification_gate(gate_runner=runner, project_id="proj-1", cwd=tmp_path)
        )
        assert violation is False
        assert unprovable is False

    def test_mutating_command_is_detected_as_violation(self, tmp_path: Path) -> None:
        from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore

        self._init_repo(tmp_path)
        store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
        store.set_project_commands(
            "proj-1",
            [ValidationCommand(validation_id="a", kind=ValidationKind.UNIT_TEST, argv=("git", "commit", "--allow-empty", "-q", "-m", "sneaky"), required=True)],
        )
        runner = QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1")

        violation, unprovable = asyncio.run(
            run_final_verification_gate(gate_runner=runner, project_id="proj-1", cwd=tmp_path)
        )
        assert violation is True
        assert unprovable is False

    def test_non_git_workspace_is_unprovable(self, tmp_path: Path) -> None:
        from orchestrator.validation import QualityGateRunner, ValidationCommand, ValidationKind, ValidationStore

        store = ValidationStore(tmp_path / "validation.sqlite3", clock=lambda: UTC_NOW)
        store.set_project_commands("proj-1", [ValidationCommand(validation_id="a", kind=ValidationKind.UNIT_TEST, argv=(PY, "-c", "pass"), required=True)])
        runner = QualityGateRunner(store, clock=lambda: UTC_NOW, id_factory=lambda: "run-1")

        violation, unprovable = asyncio.run(
            run_final_verification_gate(gate_runner=runner, project_id="proj-1", cwd=tmp_path)
        )
        assert violation is False
        assert unprovable is True


# --- 47. Observed SHA mismatch (duplicate of §7 but via the run store path) --


class TestObservedShaMismatchIntegration:
    def test_sha_mismatch_run_produces_fail(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.create(run)
        store.update_status(run.run_id, QARunStatus.RUNNING)
        store.update_status(run.run_id, QARunStatus.COMPLETED)
        store.record_result(run.run_id, _result(observed_head_sha="c" * 40))
        reloaded = store.get(run.run_id)
        verdict = evaluate_qa_verdict(run=reloaded, result=store.get_result(run.run_id), now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.FAIL


# --- 48-52. Engine independence: two fake engines, same evaluator -----------


class _FakeInternalEngine:
    """A fake INTERNAL-style engine — LLM-driven test authoring/execution,
    reuses this project's own pytest suite in spirit."""

    def run(self, request: QARequest) -> QAResult:
        return QAResult(
            engine_id="internal", observed_head_sha=request.head_sha,
            started_at=UTC_NOW, finished_at=UTC_NOW + timedelta(seconds=1),
            tests_executed=("tests/test_add.py::test_add",), passed_count=1,
            engine_reported_status="PASS",
        )


class _FakeExternalEngine:
    """A fake EXTERNAL-style engine — e.g. what a future TestSpriteQAEngine/
    MomenticQAEngine adapter would look like. Note: nothing in this class
    or the test below hardcodes any real vendor's behavior; it is purely
    illustrative of "some other engine implementing the same Protocol"."""

    def run(self, request: QARequest) -> QAResult:
        return QAResult(
            engine_id="external-fake", observed_head_sha=request.head_sha,
            started_at=UTC_NOW, finished_at=UTC_NOW + timedelta(seconds=3),
            tests_executed=("e2e::checkout_flow",), passed_count=1,
            artifacts=("https://example.test/fake-report",), engine_reported_status="success",
        )


class TestEngineIndependence:
    def test_fake_internal_and_external_engines_satisfy_the_same_protocol(self) -> None:
        internal: QAEngine = _FakeInternalEngine()
        external: QAEngine = _FakeExternalEngine()
        req = _request()
        assert internal.run(req).engine_id == "internal"
        assert external.run(req).engine_id == "external-fake"

    def test_same_governance_evaluator_normalizes_both_engines_identically(self) -> None:
        req = _request()
        for engine, engine_id in ((_FakeInternalEngine(), "internal"), (_FakeExternalEngine(), "external-fake")):
            result = engine.run(req)
            run = QARun(
                run_id=f"r-{engine_id}", project_id="p", mvp_id="m", work_item_id="wi", engine_id=engine_id,
                phase=QAPhase.FINAL_VERIFICATION, expected_base_sha=req.base_sha, expected_head_sha=req.head_sha,
                policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
                created_at=UTC_NOW, updated_at=UTC_NOW,
            )
            verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
            assert verdict.status is QAVerdictStatus.PASS  # same evaluator, same conclusion for equivalent evidence

    def test_qarun_qaverdict_qapolicy_never_changed_to_add_a_new_engine(self) -> None:
        # A third, brand-new fake engine plugs in without any change to
        # QARun/QAVerdict/QAPolicy/evaluate_qa_verdict's signature.
        class _FakeThirdEngine:
            def run(self, request: QARequest) -> QAResult:
                return QAResult(
                    engine_id="third-fake", observed_head_sha=request.head_sha,
                    started_at=UTC_NOW, finished_at=UTC_NOW,
                )

        engine: QAEngine = _FakeThirdEngine()
        req = _request()
        result = engine.run(req)
        run = QARun(
            run_id="r-third", project_id="p", mvp_id="m", work_item_id="wi", engine_id="third-fake",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha=req.base_sha, expected_head_sha=req.head_sha,
            policy=_policy(), manifest=_manifest(), status=QARunStatus.COMPLETED,
            created_at=UTC_NOW, updated_at=UTC_NOW,
        )
        verdict = evaluate_qa_verdict(run=run, result=result, now=UTC_NOW)
        assert verdict.status is QAVerdictStatus.PASS

    def test_capabilities_hold_no_vendor_name(self) -> None:
        caps = QAEngineCapabilities(engine_id="internal", supported_test_levels=("unit",), can_execute_existing_tests=True)
        assert caps.engine_id == "internal"


# --- No MVPManager / MergeEligibility / ReleaseManager coupling ------------


class TestNoWorkflowIntegrationThisSlice:
    def test_qa_module_never_imports_mvp_manager(self) -> None:
        import re

        from orchestrator import qa as module

        with open(module.__file__, encoding="utf-8") as handle:
            import_lines = [line for line in handle if re.match(r"^\s*(import|from)\s", line)]
        joined = "".join(import_lines)
        assert "mvp_manager" not in joined

    def test_qa_module_never_imports_git_governance_or_release_manager(self) -> None:
        import re

        from orchestrator import qa as module

        with open(module.__file__, encoding="utf-8") as handle:
            import_lines = [line for line in handle if re.match(r"^\s*(import|from)\s", line)]
        joined = "".join(import_lines)
        assert "git_governance" not in joined
        assert "release_manager" not in joined
