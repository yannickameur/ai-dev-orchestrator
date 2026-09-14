"""Tests for the `.qa/` regression knowledge base (Slice 22).

Real temporary directories under ``tmp_path`` — no mocking of the
filesystem, no network, no LLM. Proves the module's central invariants:
a project with no ``.qa/`` stays valid, nothing is ever auto-created by
reading, invalid YAML fails closed, writes are atomic, and known-flaky
entries never imply PASS/skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.qa import TestImpactRequest as ImpactRequest
from orchestrator.qa_knowledge import (
    CriticalPath,
    DuplicateCriticalPathIdError,
    DuplicateInvariantIdError,
    DuplicateRegressionMapIdError,
    InvalidQAKnowledgeFileError,
    KnownFlakyEntry,
    QAInvariant,
    QAKnowledgeBase,
    RegressionMapEntry,
    analyze_test_impact_deterministic,
    load_qa_knowledge_base,
    write_critical_paths,
    write_invariants,
    write_known_flaky,
    write_regression_map,
)


# --- 21. Missing .qa => empty valid knowledge -------------------------------


class TestMissingDotQa:
    def test_no_qa_directory_yields_empty_knowledge_base(self, tmp_path: Path) -> None:
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.is_empty
        assert kb.invariants == ()
        assert kb.regression_map == ()
        assert kb.critical_paths == ()
        assert kb.known_flaky == ()

    def test_empty_qa_directory_also_yields_empty_knowledge_base(self, tmp_path: Path) -> None:
        (tmp_path / ".qa").mkdir()
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.is_empty


# --- 32. No auto-create on read ---------------------------------------------


class TestNoAutoCreateOnRead:
    def test_reading_never_creates_any_file(self, tmp_path: Path) -> None:
        load_qa_knowledge_base(tmp_path)
        assert not (tmp_path / ".qa").exists()

    def test_reading_partial_qa_dir_never_creates_missing_files(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="desc")])
        load_qa_knowledge_base(tmp_path)
        assert not (tmp_path / ".qa" / "regression-map.yaml").exists()
        assert not (tmp_path / ".qa" / "critical-paths.yaml").exists()
        assert not (tmp_path / ".qa" / "known-flaky.yaml").exists()


# --- 22. Invariants load -----------------------------------------------------


class TestInvariantsLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        write_invariants(
            tmp_path,
            [
                QAInvariant(
                    invariant_id="X-1", description="A mandatory reviewer failure prevents release.",
                    criticality="critical", related_tests=("tests/test_review.py",),
                )
            ],
        )
        kb = load_qa_knowledge_base(tmp_path)
        assert len(kb.invariants) == 1
        assert kb.invariants[0].invariant_id == "X-1"
        assert kb.invariants[0].related_tests == ("tests/test_review.py",)

    def test_active_defaults_true(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="d")])
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.invariants[0].active is True
        assert kb.active_invariants() == kb.invariants

    def test_inactive_invariant_excluded_from_active(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="d", active=False)])
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.active_invariants() == ()

    def test_lookup_by_id(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="d")])
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.invariant("X-1") is not None
        assert kb.invariant("does-not-exist") is None

    def test_no_hardcoded_id_prefix_required(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="anything-goes-123", description="d")])
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.invariants[0].invariant_id == "anything-goes-123"


# --- 23. Duplicate invariant rejected ----------------------------------------


class TestDuplicateInvariantRejected:
    def test_duplicate_id_on_write_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DuplicateInvariantIdError):
            write_invariants(
                tmp_path,
                [QAInvariant(invariant_id="X-1", description="a"), QAInvariant(invariant_id="X-1", description="b")],
            )

    def test_duplicate_id_on_read_rejected(self, tmp_path: Path) -> None:
        qa_dir = tmp_path / ".qa"
        qa_dir.mkdir()
        (qa_dir / "invariants.yaml").write_text(
            yaml.safe_dump(
                {
                    "invariants": [
                        {"invariant_id": "X-1", "description": "a"},
                        {"invariant_id": "X-1", "description": "b"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(DuplicateInvariantIdError):
            load_qa_knowledge_base(tmp_path)


# --- 24. Regression map load --------------------------------------------------


class TestRegressionMapLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        write_regression_map(
            tmp_path,
            [
                RegressionMapEntry(
                    entry_id="git-governance", paths=("src/orchestrator/git_governance.py",),
                    related_tests=("tests/test_git_governance.py",), invariant_ids=("X-1",),
                )
            ],
        )
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.regression_map[0].entry_id == "git-governance"
        assert kb.regression_map[0].related_tests == ("tests/test_git_governance.py",)

    def test_entry_requires_nonempty_paths(self) -> None:
        with pytest.raises(ValueError):
            RegressionMapEntry(entry_id="x", paths=())


# --- 25. Critical paths load ---------------------------------------------


class TestCriticalPathsLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        write_critical_paths(
            tmp_path,
            [CriticalPath(path_id="merge-governance", paths=("src/orchestrator/git_governance.py",), required_tests=("tests/test_git_governance.py",))],
        )
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.critical_paths[0].path_id == "merge-governance"
        assert kb.critical_paths[0].required_tests == ("tests/test_git_governance.py",)


# --- 26. Known-flaky load ------------------------------------------------


class TestKnownFlakyLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        write_known_flaky(
            tmp_path,
            [KnownFlakyEntry(test_id="tests/test_x.py::test_y", evidence="fails 1/20 runs", suspected_cause="timing", status="open")],
        )
        kb = load_qa_knowledge_base(tmp_path)
        assert kb.known_flaky[0].test_id == "tests/test_x.py::test_y"
        assert kb.known_flaky[0].status == "open"


# --- 31. Known-flaky never implies PASS/skip --------------------------------


class TestKnownFlakyNeverImpliesPassOrSkip:
    def test_known_flaky_entry_carries_no_pass_or_skip_semantics(self, tmp_path: Path) -> None:
        write_known_flaky(tmp_path, [KnownFlakyEntry(test_id="t", evidence="e", status="open")])
        kb = load_qa_knowledge_base(tmp_path)
        entry = kb.known_flaky[0]
        # The dataclass has no field that could be read as "skip this" or
        # "treat as passed" — only descriptive/evidence fields.
        field_names = set(type(entry).__dataclass_fields__)
        assert "skip" not in field_names
        assert "passed" not in field_names
        assert "ignore" not in field_names

    def test_critical_flaky_test_stays_present_not_hidden(self, tmp_path: Path) -> None:
        write_known_flaky(tmp_path, [KnownFlakyEntry(test_id="critical-test", evidence="flaky under load", status="open")])
        kb = load_qa_knowledge_base(tmp_path)
        assert any(e.test_id == "critical-test" for e in kb.known_flaky)


# --- 27. Invalid YAML fails closed -------------------------------------------


class TestInvalidYamlFailsClosed:
    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        qa_dir = tmp_path / ".qa"
        qa_dir.mkdir()
        (qa_dir / "invariants.yaml").write_text("invariants: [this is not: valid: yaml: at all", encoding="utf-8")
        with pytest.raises(InvalidQAKnowledgeFileError):
            load_qa_knowledge_base(tmp_path)

    def test_wrong_top_level_shape_raises(self, tmp_path: Path) -> None:
        qa_dir = tmp_path / ".qa"
        qa_dir.mkdir()
        (qa_dir / "invariants.yaml").write_text(yaml.safe_dump(["just", "a", "list"]), encoding="utf-8")
        with pytest.raises(InvalidQAKnowledgeFileError):
            load_qa_knowledge_base(tmp_path)

    def test_regression_map_duplicate_id_raises(self, tmp_path: Path) -> None:
        qa_dir = tmp_path / ".qa"
        qa_dir.mkdir()
        (qa_dir / "regression-map.yaml").write_text(
            yaml.safe_dump({"entries": [{"id": "a", "paths": ["x"]}, {"id": "a", "paths": ["y"]}]}), encoding="utf-8"
        )
        with pytest.raises(DuplicateRegressionMapIdError):
            load_qa_knowledge_base(tmp_path)

    def test_critical_path_duplicate_id_raises(self, tmp_path: Path) -> None:
        qa_dir = tmp_path / ".qa"
        qa_dir.mkdir()
        (qa_dir / "critical-paths.yaml").write_text(
            yaml.safe_dump({"critical_paths": [{"id": "a", "paths": ["x"]}, {"id": "a", "paths": ["y"]}]}), encoding="utf-8"
        )
        with pytest.raises(DuplicateCriticalPathIdError):
            load_qa_knowledge_base(tmp_path)


# --- 28/29. Atomic write, UTF-8 -----------------------------------------------


class TestAtomicWriteUtf8:
    def test_no_temp_file_left_behind(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="d")])
        leftover = list((tmp_path / ".qa").glob(".*.tmp"))
        assert leftover == []

    def test_unicode_content_round_trips(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="Contenu avec accents : é, à, ç, «test»")])
        kb = load_qa_knowledge_base(tmp_path)
        assert "é" in kb.invariants[0].description
        assert "«test»" in kb.invariants[0].description

    def test_written_file_is_valid_utf8_on_disk(self, tmp_path: Path) -> None:
        write_invariants(tmp_path, [QAInvariant(invariant_id="X-1", description="é à ç")])
        raw = (tmp_path / ".qa" / "invariants.yaml").read_bytes()
        raw.decode("utf-8")  # must not raise


# --- 30. Deterministic reload -------------------------------------------------


class TestDeterministicReload:
    def test_same_content_reloaded_twice_is_identical(self, tmp_path: Path) -> None:
        write_invariants(
            tmp_path,
            [QAInvariant(invariant_id="X-1", description="d1"), QAInvariant(invariant_id="X-2", description="d2")],
        )
        first = load_qa_knowledge_base(tmp_path)
        second = load_qa_knowledge_base(tmp_path)
        assert first == second

    def test_write_output_is_byte_stable_for_same_input(self, tmp_path: Path) -> None:
        invariants = [QAInvariant(invariant_id="X-1", description="d")]
        write_invariants(tmp_path, invariants)
        first_bytes = (tmp_path / ".qa" / "invariants.yaml").read_bytes()
        write_invariants(tmp_path, invariants)
        second_bytes = (tmp_path / ".qa" / "invariants.yaml").read_bytes()
        assert first_bytes == second_bytes


# --- 53-58. Deterministic Test Impact Analysis --------------------------------


class TestDeterministicTestImpactAnalysis:
    def _kb(self) -> QAKnowledgeBase:
        return QAKnowledgeBase(
            regression_map=(
                RegressionMapEntry(
                    entry_id="git-governance", paths=("src/orchestrator/git_governance.py",),
                    related_tests=("tests/test_git_governance.py",), invariant_ids=("INV-MERGE-SHA",),
                ),
                RegressionMapEntry(
                    entry_id="validation", paths=("src/orchestrator/validation.py",),
                    related_tests=("tests/test_validation.py",), invariant_ids=("INV-MANIFEST-SNAPSHOT",),
                ),
            ),
            critical_paths=(
                CriticalPath(
                    path_id="merge-governance", paths=("src/orchestrator/git_governance.py",),
                    required_tests=("tests/test_mvp_manager_git_governance.py",),
                ),
            ),
        )

    def test_changed_path_matches_regression_map_entry(self) -> None:
        result = analyze_test_impact_deterministic(
            ImpactRequest(base_sha="a" * 40, head_sha="b" * 40, changed_files=("src/orchestrator/git_governance.py",)),
            self._kb(),
        )
        assert "git-governance" in result.impacted_components

    def test_related_tests_returned(self) -> None:
        result = analyze_test_impact_deterministic(
            ImpactRequest(base_sha="a" * 40, head_sha="b" * 40, changed_files=("src/orchestrator/git_governance.py",)),
            self._kb(),
        )
        assert "tests/test_git_governance.py" in result.existing_tests
        assert "tests/test_mvp_manager_git_governance.py" in result.existing_tests  # via critical path too

    def test_invariant_ids_returned(self) -> None:
        result = analyze_test_impact_deterministic(
            ImpactRequest(base_sha="a" * 40, head_sha="b" * 40, changed_files=("src/orchestrator/git_governance.py",)),
            self._kb(),
        )
        assert "INV-MERGE-SHA" in result.required_invariants

    def test_unrelated_change_invents_nothing(self) -> None:
        result = analyze_test_impact_deterministic(
            ImpactRequest(base_sha="a" * 40, head_sha="b" * 40, changed_files=("README.md",)),
            self._kb(),
        )
        assert result.impacted_components == ()
        assert result.existing_tests == ()
        assert result.required_invariants == ()

    def test_multiple_changed_files_aggregate_without_duplicates(self) -> None:
        result = analyze_test_impact_deterministic(
            ImpactRequest(
                base_sha="a" * 40, head_sha="b" * 40,
                changed_files=("src/orchestrator/git_governance.py", "src/orchestrator/validation.py"),
            ),
            self._kb(),
        )
        assert set(result.impacted_components) == {"git-governance", "validation", "merge-governance"}
        assert len(result.existing_tests) == len(set(result.existing_tests))  # no duplicates

    def test_deterministic_results_same_input_same_output(self) -> None:
        request = ImpactRequest(base_sha="a" * 40, head_sha="b" * 40, changed_files=("src/orchestrator/git_governance.py",))
        kb = self._kb()
        first = analyze_test_impact_deterministic(request, kb)
        second = analyze_test_impact_deterministic(request, kb)
        assert first == second

    def test_no_ast_or_semantic_analysis_is_attempted(self) -> None:
        import inspect

        from orchestrator import qa_knowledge as module

        source = inspect.getsource(module.analyze_test_impact_deterministic)
        for forbidden in ("import ast", "networkx", "tree_sitter"):
            assert forbidden not in source


class TestShippedInvariantsFile:
    """This repository's own real .qa/invariants.yaml (Slice 22, Part N) —
    seeded minimally, only for invariants already demonstrated by
    existing tests. Loads through the real loader, no mocking."""

    def test_this_repos_own_qa_invariants_load(self) -> None:
        kb = load_qa_knowledge_base(Path(__file__).parent.parent)
        assert len(kb.invariants) >= 1
        for inv in kb.invariants:
            assert inv.description
            assert inv.criticality

    def test_this_repos_invariants_are_all_active(self) -> None:
        kb = load_qa_knowledge_base(Path(__file__).parent.parent)
        assert kb.active_invariants() == kb.invariants

    def test_this_repos_invariant_ids_are_unique(self) -> None:
        kb = load_qa_knowledge_base(Path(__file__).parent.parent)
        ids = [inv.invariant_id for inv in kb.invariants]
        assert len(ids) == len(set(ids))
