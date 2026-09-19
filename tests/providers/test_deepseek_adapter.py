"""Tests for the DeepSeek provider factory (build_deepseek_adapter).

All offline: no network call, no real DeepSeek/Claude Code invocation.
``env`` is always an explicit mapping here, never the real process
environment, so these tests never depend on (or leak) a real API key.
"""

from __future__ import annotations

import asyncio
import functools

from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter
from orchestrator.providers.deepseek_adapter import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    PROVIDER_NAME,
    DeepSeekConfigError,
    build_deepseek_adapter,
)

import pytest


class TestMissingApiKey:
    def test_missing_key_raises_controlled_error(self) -> None:
        with pytest.raises(DeepSeekConfigError):
            build_deepseek_adapter(env={})

    def test_blank_key_raises_controlled_error(self) -> None:
        with pytest.raises(DeepSeekConfigError):
            build_deepseek_adapter(env={"DEEPSEEK_API_KEY": "   "})

    def test_missing_key_error_message_names_the_env_var(self) -> None:
        with pytest.raises(DeepSeekConfigError, match="DEEPSEEK_API_KEY"):
            build_deepseek_adapter(env={})


class TestBuildsClaudeCodeAdapter:
    """REUSE FIRST: this must return a real ClaudeCodeAdapter, never a
    second, independent HTTP adapter."""

    def test_returns_a_claude_code_adapter(self) -> None:
        adapter = build_deepseek_adapter(env={"DEEPSEEK_API_KEY": "sk-test"})
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._provider_name == PROVIDER_NAME

    def test_default_base_url_and_model_are_used(self) -> None:
        adapter = build_deepseek_adapter(env={"DEEPSEEK_API_KEY": "sk-test"})
        assert adapter._model == DEFAULT_MODEL
        assert isinstance(adapter._run_subprocess, functools.partial)
        env = adapter._run_subprocess.keywords["env"]
        assert env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
        assert env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_base_url_and_model_are_overridable(self) -> None:
        adapter = build_deepseek_adapter(
            env={
                "DEEPSEEK_API_KEY": "sk-test",
                "DEEPSEEK_BASE_URL": "https://custom.example/anthropic",
                "DEEPSEEK_MODEL": "deepseek-v4-pro",
            }
        )
        assert adapter._model == "deepseek-v4-pro"
        env = adapter._run_subprocess.keywords["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://custom.example/anthropic"

    def test_probe_yields_deepseek_labeled_state(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b'{"type": "result", "is_error": false, "result": "ok"}\n', b""

        adapter = build_deepseek_adapter(
            env={"DEEPSEEK_API_KEY": "sk-test"}, subprocess_runner=fake_runner,
        )

        state = asyncio.run(adapter.probe())

        assert state.provider == "deepseek"
        assert state.availability.available is True
        # No native rate_limit_event in this fixture: honest fallback, no
        # quota windows fabricated for a provider with no known telemetry.
        assert state.quota_windows == ()


class TestDoesNotAffectOtherProviders:
    def test_missing_deepseek_key_does_not_raise_for_unrelated_env(self) -> None:
        # Simulates the real environment of a project that only uses
        # Claude/Codex/Mistral: DEEPSEEK_API_KEY is simply absent, and
        # nothing about this factory is even called unless a worker
        # actually requires the deepseek provider (see project_runtime.py).
        env = {"ANTHROPIC_API_KEY": "unrelated", "PATH": "/usr/bin"}
        with pytest.raises(DeepSeekConfigError):
            build_deepseek_adapter(env=env)
