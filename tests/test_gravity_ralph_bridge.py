"""Tests for gravity_ralph_bridge.py's pure argv/command translation (P19).

Offline only: exercises ``build_gravity_command``/``permission_args``
directly — no real ``agy``/subprocess invocation anywhere in this file.
"""

from __future__ import annotations

import pytest

from orchestrator.gravity_ralph_bridge import (
    BridgeArgError,
    build_gravity_command,
    permission_args,
)


class TestPermissionArgs:
    def test_standard_adds_no_extra_flag(self) -> None:
        assert permission_args("standard") == []

    def test_unrestricted_uses_verified_bypass_flag(self) -> None:
        assert permission_args("unrestricted") == ["--dangerously-skip-permissions"]

    def test_unsupported_mode_raises(self) -> None:
        with pytest.raises(BridgeArgError):
            permission_args("bogus")


class TestBuildGravityCommand:
    def test_standard_never_contains_the_bypass_flag(self) -> None:
        command = build_gravity_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="hi")
        assert "--dangerously-skip-permissions" not in command

    def test_unrestricted_contains_only_verified_bypass_flag(self) -> None:
        command = build_gravity_command(["--permission-mode", "unrestricted", "/tmp/x"], prompt_text="hi")
        assert "--dangerously-skip-permissions" in command

    def test_mode_accept_edits_is_unconditional_regardless_of_permission_mode(self) -> None:
        for mode in ("standard", "unrestricted"):
            command = build_gravity_command(["--permission-mode", mode, "/tmp/x"], prompt_text="hi")
            assert "--mode=accept-edits" in command

    def test_omitted_mode_never_defaults_to_a_bypass(self) -> None:
        """No `--permission-mode` argv at all: unlike
        `vibe_ralph_bridge.py`'s own legacy pre-P12 default, this bridge
        has no such history — the safe, non-bypassing STANDARD invocation
        is used instead of ever defaulting to a bypass."""
        command = build_gravity_command(["/tmp/x"], prompt_text="hi")
        assert "--mode=accept-edits" in command
        assert "--dangerously-skip-permissions" not in command

    def test_unsupported_mode_raises_before_any_command_is_built(self) -> None:
        with pytest.raises(BridgeArgError):
            build_gravity_command(["--permission-mode", "bogus", "/tmp/x"], prompt_text="hi")

    def test_model_is_forwarded_verbatim_as_a_real_agy_flag(self) -> None:
        command = build_gravity_command(
            ["--model", "claude-sonnet-4-6", "--permission-mode", "standard", "/tmp/x"], prompt_text="hi",
        )
        assert "--model" in command
        assert command[command.index("--model") + 1] == "claude-sonnet-4-6"

    def test_no_model_flag_omitted_entirely_when_not_given(self) -> None:
        command = build_gravity_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="hi")
        assert "--model" not in command

    def test_prompt_text_is_passed_through_as_the_dash_p_value(self) -> None:
        command = build_gravity_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="do the thing")
        assert command[0:3] == ["agy", "-p", "do the thing"]

    def test_permission_mode_flag_with_no_value_raises(self) -> None:
        # "--permission-mode" is the last "rest" token, immediately
        # followed only by the (required) final prompt-path argument —
        # i.e. no actual mode value was ever given.
        with pytest.raises(BridgeArgError):
            build_gravity_command(["--permission-mode", "/tmp/x"], prompt_text="hi")

    def test_model_flag_with_no_value_raises(self) -> None:
        with pytest.raises(BridgeArgError):
            build_gravity_command(["--model", "/tmp/x"], prompt_text="hi")
