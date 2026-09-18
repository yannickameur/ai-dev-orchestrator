"""Tests for ExecutionPermissionMode — provider-neutral execution policy (P12)."""

from __future__ import annotations

import pytest

from orchestrator.execution_policy import ExecutionPermissionMode


class TestExecutionPermissionMode:
    def test_values_are_lowercase_and_match_config_spelling(self) -> None:
        assert ExecutionPermissionMode.STANDARD.value == "standard"
        assert ExecutionPermissionMode.UNRESTRICTED.value == "unrestricted"

    def test_parse_accepts_exact_lowercase_string(self) -> None:
        assert ExecutionPermissionMode.parse("standard") is ExecutionPermissionMode.STANDARD
        assert ExecutionPermissionMode.parse("unrestricted") is ExecutionPermissionMode.UNRESTRICTED

    def test_parse_is_case_and_whitespace_tolerant(self) -> None:
        assert ExecutionPermissionMode.parse("  STANDARD  ") is ExecutionPermissionMode.STANDARD

    def test_parse_accepts_an_already_parsed_enum_member(self) -> None:
        assert ExecutionPermissionMode.parse(ExecutionPermissionMode.UNRESTRICTED) is ExecutionPermissionMode.UNRESTRICTED

    def test_parse_rejects_unknown_value(self) -> None:
        with pytest.raises(ValueError):
            ExecutionPermissionMode.parse("yolo")

    def test_parse_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            ExecutionPermissionMode.parse(123)

    def test_exactly_two_modes_exist(self) -> None:
        assert {m.value for m in ExecutionPermissionMode} == {"standard", "unrestricted"}
