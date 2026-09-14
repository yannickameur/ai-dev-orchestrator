"""Tests for the protected-test baseline (Slice 22).

Real files under ``tmp_path`` — deterministic SHA-256 hashing, no
mocking. Proves the central invariant: an unauthorized change to a
protected test can never be waved through, and a brand-new,
never-protected file is never treated as a violation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.qa import QAPhase, QARunStatus, QAVerdictStatus, evaluate_qa_verdict
from orchestrator.qa_protection import (
    ExpectedChangeSource,
    ProtectedTestBaseline,
    TestChangeAuthorization as ChangeAuthorization,
    capture_protected_test_baseline,
    compare_protected_test_baseline,
    has_unauthorized_change,
    hash_file,
)

UTC_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _write(repo: Path, rel_path: str, content: str) -> None:
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


# --- 33/40. Protected baseline snapshot, SHA persisted ----------------------


class TestProtectedBaselineSnapshot:
    def test_capture_records_base_sha(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2\n")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        assert baseline.base_sha == "a" * 40

    def test_capture_records_digest_for_existing_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "content")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        assert baseline.files[0].digest is not None
        assert len(baseline.files[0].digest) == 64  # sha256 hex digest

    def test_capture_records_none_digest_for_missing_file(self, tmp_path: Path) -> None:
        baseline = capture_protected_test_baseline(tmp_path, ["tests/does_not_exist.py"], base_sha="a" * 40)
        assert baseline.files[0].digest is None


# --- 41. Hashes deterministic -------------------------------------------


class TestHashesDeterministic:
    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "identical content")
        _write(tmp_path, "b.py", "identical content")
        assert hash_file(tmp_path / "a.py") == hash_file(tmp_path / "b.py")

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "content one")
        _write(tmp_path, "b.py", "content two")
        assert hash_file(tmp_path / "a.py") != hash_file(tmp_path / "b.py")

    def test_missing_file_hash_is_none(self, tmp_path: Path) -> None:
        assert hash_file(tmp_path / "missing.py") is None

    def test_hash_stable_across_calls(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "content")
        assert hash_file(tmp_path / "a.py") == hash_file(tmp_path / "a.py")


# --- 34. Unchanged test accepted ---------------------------------------------


class TestUnchangedTestAccepted:
    def test_no_change_no_violation(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2\n")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        changes = compare_protected_test_baseline(baseline, tmp_path)
        assert changes[0].change_detected is False
        assert has_unauthorized_change(changes) is False


# --- 35. Modified protected test detected ------------------------------------


class TestModifiedProtectedTestDetected:
    def test_content_change_detected(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2\n")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 80  # weakened!\n")
        changes = compare_protected_test_baseline(baseline, tmp_path)
        assert changes[0].change_detected is True
        assert changes[0].deleted is False
        assert has_unauthorized_change(changes) is True  # no authorization supplied


# --- 36. Deleted protected test detected -------------------------------------


class TestDeletedProtectedTestDetected:
    def test_deletion_detected_explicitly(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "content")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        (tmp_path / "tests" / "test_add.py").unlink()
        changes = compare_protected_test_baseline(baseline, tmp_path)
        assert changes[0].deleted is True
        assert changes[0].change_detected is True
        assert changes[0].digest_after is None
        assert has_unauthorized_change(changes) is True


# --- 37. New unprotected test not automatically a violation -----------------


class TestNewUnprotectedTestNotViolation:
    def test_new_file_outside_baseline_never_inspected(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "content")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        _write(tmp_path, "tests/test_brand_new.py", "def test_new(): pass\n")
        changes = compare_protected_test_baseline(baseline, tmp_path)
        assert len(changes) == 1  # only the originally-protected file is ever compared
        assert has_unauthorized_change(changes) is False

    def test_a_file_added_to_baseline_that_did_not_exist_at_capture_is_observable_not_a_violation(
        self, tmp_path: Path
    ) -> None:
        # Capture a baseline that already declares a not-yet-existing path
        # (e.g. a test the WorkItem is expected to add) — its appearance
        # is observable (digest_before None -> digest_after set) but is
        # not, by itself, flagged as a violation (change_detected reflects
        # a genuine digest difference, but there's no prior content to
        # have been weakened).
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_future.py"], base_sha="a" * 40)
        assert baseline.files[0].digest is None
        _write(tmp_path, "tests/test_future.py", "def test_future(): pass\n")
        changes = compare_protected_test_baseline(baseline, tmp_path)
        assert changes[0].digest_before is None
        assert changes[0].digest_after is not None
        assert changes[0].deleted is False


# --- 38. Unauthorized protected test change prevents PASS -------------------


class TestUnauthorizedChangeBlocksPass:
    def _run(self):
        from orchestrator.qa import QAEvidenceManifest, QAPolicy, QARun

        return QARun(
            run_id="r", project_id="p", mvp_id="m", work_item_id="wi", engine_id="internal",
            phase=QAPhase.FINAL_VERIFICATION, expected_base_sha="a" * 40, expected_head_sha="b" * 40,
            policy=QAPolicy(), manifest=QAEvidenceManifest(required_test_ids=("t1",)),
            status=QARunStatus.COMPLETED, created_at=UTC_NOW, updated_at=UTC_NOW,
        )

    def test_unauthorized_protected_change_produces_fail(self, tmp_path: Path) -> None:
        from orchestrator.qa import QAResult

        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2\n")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert True  # weakened\n")
        changes = compare_protected_test_baseline(baseline, tmp_path)

        result = QAResult(engine_id="internal", observed_head_sha="b" * 40, started_at=UTC_NOW, finished_at=UTC_NOW)
        verdict = evaluate_qa_verdict(
            run=self._run(), result=result, now=UTC_NOW, unauthorized_protected_change=has_unauthorized_change(changes)
        )
        assert verdict.status is QAVerdictStatus.FAIL
        assert "protected" in verdict.reason


# --- 39. Authorized expected change representable ----------------------------


class TestAuthorizedExpectedChangeRepresentable:
    def test_authorization_suppresses_unauthorized_flag(self, tmp_path: Path) -> None:
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2\n")
        baseline = capture_protected_test_baseline(tmp_path, ["tests/test_add.py"], base_sha="a" * 40)
        _write(tmp_path, "tests/test_add.py", "def test_add(): assert 1 + 1 == 2  # comment added, spec updated\n")

        authorization = ChangeAuthorization(
            path="tests/test_add.py", source=ExpectedChangeSource.ROADMAP_DECISION,
            justification="Roadmap decision to clarify the assertion comment.",
            authorized_at=UTC_NOW, reference="ROADMAP.md#slice-22",
        )
        changes = compare_protected_test_baseline(baseline, tmp_path, authorizations={"tests/test_add.py": authorization})
        assert changes[0].change_detected is True
        assert changes[0].is_unauthorized is False
        assert has_unauthorized_change(changes) is False

    def test_authorization_sources_are_the_canonical_set(self) -> None:
        assert {s.value for s in ExpectedChangeSource} == {
            "acceptance_criteria", "specification", "roadmap_decision", "architecture_decision", "human_approval",
        }

    def test_authorization_requires_justification(self) -> None:
        with pytest.raises(ValueError):
            ChangeAuthorization(
                path="tests/test_add.py", source=ExpectedChangeSource.HUMAN_APPROVAL,
                justification="", authorized_at=UTC_NOW,
            )


class TestBaselineValidation:
    def test_empty_base_sha_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProtectedTestBaseline(base_sha="", files=())

    def test_no_hardcoded_tests_directory_convention(self) -> None:
        # protected_paths is fully caller-supplied — no "tests/" default
        # anywhere in the capture function's signature or behavior.
        import inspect

        sig = inspect.signature(capture_protected_test_baseline)
        assert "protected_paths" in sig.parameters
        assert sig.parameters["protected_paths"].default is inspect.Parameter.empty
