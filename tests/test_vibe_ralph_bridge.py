"""Tests for vibe_ralph_bridge.py's pure argv/command translation (P12).

Offline only: exercises ``build_vibe_command``/``permission_args`` directly
— no real ``vibe``/subprocess invocation anywhere in this file.
"""

from __future__ import annotations

import pytest

from orchestrator.vibe_ralph_bridge import (
    BridgeArgError,
    build_vibe_command,
    permission_args,
)


class TestPermissionArgs:
    def test_standard_uses_verified_ask_agent(self) -> None:
        assert permission_args("standard") == ["--agent", "ask"]

    def test_unrestricted_uses_verified_auto_approve(self) -> None:
        assert permission_args("unrestricted") == ["--auto-approve"]

    def test_unsupported_mode_raises(self) -> None:
        with pytest.raises(BridgeArgError):
            permission_args("bogus")


class TestBuildVibeCommand:
    def test_standard_never_contains_unrestricted_flag(self) -> None:
        command, _ = build_vibe_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="hi")
        assert "--auto-approve" not in command
        assert "--agent" in command and "ask" in command

    def test_unrestricted_contains_only_verified_bypass_mechanism(self) -> None:
        command, _ = build_vibe_command(["--permission-mode", "unrestricted", "/tmp/x"], prompt_text="hi")
        assert "--auto-approve" in command
        assert "--agent" not in command

    def test_trust_is_unconditional_regardless_of_mode(self) -> None:
        for mode in ("standard", "unrestricted"):
            command, _ = build_vibe_command(["--permission-mode", mode, "/tmp/x"], prompt_text="hi")
            assert "--trust" in command

    def test_legacy_omitted_mode_preserves_pre_p12_unconditional_auto_approve(self) -> None:
        """No `--permission-mode` argv at all == an ExecutionPermissionMode
        -unconfigured RalphExecutionEngine — the bridge's original,
        pre-P12 behavior (unconditional --trust --auto-approve) is
        preserved unchanged for this legacy case only."""
        command, _ = build_vibe_command(["/tmp/x"], prompt_text="hi")
        assert "--trust" in command
        assert "--auto-approve" in command

    def test_unsupported_mode_raises_before_any_command_is_built(self) -> None:
        with pytest.raises(BridgeArgError):
            build_vibe_command(["--permission-mode", "bogus", "/tmp/x"], prompt_text="hi")

    def test_model_override_extracted_and_not_leaked_into_command(self) -> None:
        command, model_env_override = build_vibe_command(
            ["--model", "custom-model", "--permission-mode", "standard", "/tmp/x"], prompt_text="hi",
        )
        assert model_env_override == "custom-model"
        assert "--model" not in command

    def test_default_model_sentinel_never_becomes_an_env_override(self) -> None:
        _, model_env_override = build_vibe_command(
            ["--model", "vibe-default", "--permission-mode", "standard", "/tmp/x"], prompt_text="hi",
        )
        assert model_env_override is None

    def test_prompt_text_is_passed_through_as_the_dash_p_value(self) -> None:
        command, _ = build_vibe_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="do the thing")
        assert command[0:3] == ["vibe", "-p", "do the thing"]

    def test_output_json_always_present(self) -> None:
        command, _ = build_vibe_command(["--permission-mode", "standard", "/tmp/x"], prompt_text="hi")
        assert command[-2:] == ["--output", "json"]

    def test_permission_mode_flag_with_no_value_raises(self) -> None:
        # "--permission-mode" is the last "rest" token, immediately
        # followed only by the (required) final prompt-path argument —
        # i.e. no actual mode value was ever given.
        with pytest.raises(BridgeArgError):
            build_vibe_command(["--permission-mode", "/tmp/x"], prompt_text="hi")

    def test_model_flag_with_no_value_raises(self) -> None:
        with pytest.raises(BridgeArgError):
            build_vibe_command(["--model", "/tmp/x"], prompt_text="hi")
