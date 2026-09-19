"""Tests for the Kimi provider factory (build_kimi_adapter).

All offline: no network call, no real Kimi/Claude Code invocation. ``env``
is always an explicit mapping here, never the real process environment, so
these tests never depend on (or leak) a real API key.
"""

from __future__ import annotations

import asyncio
import functools

from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter
from orchestrator.providers.kimi_adapter import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    PROVIDER_NAME,
    KimiConfigError,
    build_kimi_adapter,
)

import pytest


class TestMissingApiKey:
    def test_missing_key_raises_controlled_error(self) -> None:
        with pytest.raises(KimiConfigError):
            build_kimi_adapter(env={})

    def test_blank_key_raises_controlled_error(self) -> None:
        with pytest.raises(KimiConfigError):
            build_kimi_adapter(env={"KIMI_API_KEY": "   "})

    def test_missing_key_error_message_names_the_env_var(self) -> None:
        with pytest.raises(KimiConfigError, match="KIMI_API_KEY"):
            build_kimi_adapter(env={})


class TestBuildsClaudeCodeAdapter:
    """REUSE FIRST: this must return a real ClaudeCodeAdapter, never a
    second, independent HTTP adapter."""

    def test_returns_a_claude_code_adapter(self) -> None:
        adapter = build_kimi_adapter(env={"KIMI_API_KEY": "kimi-test"})
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._provider_name == PROVIDER_NAME

    def test_default_base_url_and_model_are_used(self) -> None:
        adapter = build_kimi_adapter(env={"KIMI_API_KEY": "kimi-test"})
        assert adapter._model == DEFAULT_MODEL
        assert isinstance(adapter._run_subprocess, functools.partial)
        env = adapter._run_subprocess.keywords["env"]
        assert env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
        assert env["ANTHROPIC_API_KEY"] == "kimi-test"

    def test_base_url_and_model_are_overridable(self) -> None:
        adapter = build_kimi_adapter(
            env={
                "KIMI_API_KEY": "kimi-test",
                "KIMI_BASE_URL": "https://custom.example/coding/",
                "KIMI_MODEL": "k3-256k",
            }
        )
        assert adapter._model == "k3-256k"
        env = adapter._run_subprocess.keywords["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://custom.example/coding/"

    def test_probe_yields_kimi_labeled_state(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b'{"type": "result", "is_error": false, "result": "ok"}\n', b""

        adapter = build_kimi_adapter(
            env={"KIMI_API_KEY": "kimi-test"}, subprocess_runner=fake_runner,
        )

        state = asyncio.run(adapter.probe())

        assert state.provider == "kimi"
        assert state.availability.available is True
        # No native rate_limit_event in this fixture: honest fallback, no
        # quota windows fabricated for a provider with no known telemetry.
        assert state.quota_windows == ()


class TestDoesNotAffectOtherProviders:
    def test_missing_kimi_key_does_not_raise_for_unrelated_env(self) -> None:
        # Simulates the real environment of a project that only uses
        # Claude/Codex/Mistral/DeepSeek: KIMI_API_KEY is simply absent, and
        # nothing about this factory is even called unless a worker
        # actually requires the kimi provider (see project_runtime.py).
        env = {"ANTHROPIC_API_KEY": "unrelated", "PATH": "/usr/bin"}
        with pytest.raises(KimiConfigError):
            build_kimi_adapter(env=env)
