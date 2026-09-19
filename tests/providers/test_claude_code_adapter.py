"""Tests for ClaudeCodeAdapter (Phase 1 / Slice 1).

All tests are offline: no network call, no real Claude Code invocation. The
one real fixture (``fixtures/claude_stream_allowed.jsonl``) was captured
once from a real ``claude -p ... --output-format stream-json`` run (see
ROADMAP.md / commit history) and cleaned of any session-specific,
non-essential data before being committed.

The single timeout test spawns the local ``sleep`` binary directly (not
Claude, no network) to exercise the real subprocess-timeout wrapper.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.providers.claude_code_adapter import (
    ClaudeCodeAdapter,
    ClaudeProbeTimeout,
    ClaudeProcessError,
    ClaudeStreamParseError,
    _default_subprocess_runner,
    parse_claude_stream,
)
from orchestrator.providers.contracts import UnavailabilityReason

FIXTURES_DIR = Path(__file__).parent / "fixtures"
UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _read_fixture(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text().splitlines()


class TestParseClaudeStreamRealFixture:
    def test_allowed_status_yields_available_provider_state(self) -> None:
        lines = _read_fixture("claude_stream_allowed.jsonl")

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        assert state.provider == "anthropic"
        assert state.availability.available is True
        assert state.availability.reason is None
        assert state.observed_at == UTC_NOW

    def test_five_hour_and_seven_day_windows_are_parsed(self) -> None:
        lines = _read_fixture("claude_stream_allowed.jsonl")

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        window_types = {window.window_type for window in state.quota_windows}
        assert window_types == {"five_hour", "seven_day"}

        five_hour = next(w for w in state.quota_windows if w.window_type == "five_hour")
        seven_day = next(w for w in state.quota_windows if w.window_type == "seven_day")
        assert five_hour.utilization == 0.59
        assert seven_day.utilization == 0.06
        assert five_hour.source == "claude_stream_json"

    def test_reset_at_is_converted_to_timezone_aware_datetime(self) -> None:
        lines = _read_fixture("claude_stream_allowed.jsonl")

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        five_hour = next(w for w in state.quota_windows if w.window_type == "five_hour")
        assert five_hour.reset_at is not None
        assert five_hour.reset_at.tzinfo is not None
        assert five_hour.reset_at == datetime.fromtimestamp(1789222800, tz=timezone.utc)


class TestUtilizationHandling:
    def _lines_with_windows(self, unified_windows: dict) -> list[str]:
        import json

        return [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed",
                        "unifiedWindows": unified_windows,
                    },
                }
            ),
            json.dumps({"type": "result", "is_error": False, "result": "STATUS_OK"}),
        ]

    def test_zero_utilization_is_kept_as_real_zero(self) -> None:
        lines = self._lines_with_windows({"five_hour": {"utilization": 0.0}})

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        window = state.quota_windows[0]
        assert window.utilization == 0.0
        assert window.utilization is not None

    def test_absent_utilization_stays_none(self) -> None:
        lines = self._lines_with_windows({"five_hour": {}})

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        window = state.quota_windows[0]
        assert window.utilization is None


class TestAvailabilityFromStatus:
    def _lines_with_status(self, status: str | None) -> list[str]:
        import json

        info = {"unifiedWindows": {}}
        if status is not None:
            info["status"] = status
        return [
            json.dumps({"type": "rate_limit_event", "rate_limit_info": info}),
            json.dumps({"type": "result", "is_error": False, "result": "STATUS_OK"}),
        ]

    def test_status_allowed_is_available(self) -> None:
        state = parse_claude_stream(
            self._lines_with_status("allowed"), observed_at=UTC_NOW
        )
        assert state.availability.available is True
        assert state.availability.reason is None

    def test_status_rejected_is_quota_exhausted(self) -> None:
        state = parse_claude_stream(
            self._lines_with_status("rejected"), observed_at=UTC_NOW
        )
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_missing_status_is_unknown(self) -> None:
        state = parse_claude_stream(self._lines_with_status(None), observed_at=UTC_NOW)
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.UNKNOWN


class TestMultipleRateLimitEvents:
    def test_last_rate_limit_event_wins(self) -> None:
        import json

        lines = [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "unifiedWindows": {"five_hour": {"utilization": 1.0}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed",
                        "unifiedWindows": {"five_hour": {"utilization": 0.1}},
                    },
                }
            ),
            json.dumps({"type": "result", "is_error": False, "result": "STATUS_OK"}),
        ]

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        # Documented rule: the LAST rate_limit_event observed in the stream
        # is authoritative, not the first.
        assert state.availability.available is True
        assert state.quota_windows[0].utilization == 0.1


class TestNoRateLimitEventFallback:
    def test_successful_result_without_rate_limit_event_is_available(self) -> None:
        import json

        lines = [json.dumps({"type": "result", "is_error": False, "result": "ok"})]

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        assert state.availability.available is True
        assert state.quota_windows == ()

    def test_error_result_without_rate_limit_event_is_provider_error(self) -> None:
        import json

        lines = [json.dumps({"type": "result", "is_error": True, "result": ""})]

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.PROVIDER_ERROR


class TestMalformedStreams:
    def test_incomplete_stream_without_result_event_raises(self) -> None:
        import json

        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}),
        ]

        with pytest.raises(ClaudeStreamParseError, match="no terminal 'result' event"):
            parse_claude_stream(lines, observed_at=UTC_NOW)

    def test_invalid_json_line_raises(self) -> None:
        lines = ["{not valid json", '{"type": "result", "is_error": false}']

        with pytest.raises(ClaudeStreamParseError, match="invalid JSON"):
            parse_claude_stream(lines, observed_at=UTC_NOW)

    def test_blank_lines_are_ignored(self) -> None:
        import json

        lines = ["", "   ", json.dumps({"type": "result", "is_error": False, "result": "ok"})]

        state = parse_claude_stream(lines, observed_at=UTC_NOW)

        assert state.availability.available is True


class TestAdapterProbeWithInjectedSubprocess:
    async def _fake_runner_factory(self, exit_code: int, stdout: bytes, stderr: bytes):
        async def _runner(args, timeout):
            return exit_code, stdout, stderr

        return _runner

    def test_probe_returns_provider_state_on_success(self) -> None:
        fixture_bytes = (FIXTURES_DIR / "claude_stream_allowed.jsonl").read_bytes()

        async def fake_runner(args, timeout):
            return 0, fixture_bytes, b""

        adapter = ClaudeCodeAdapter(
            subprocess_runner=fake_runner, clock=lambda: UTC_NOW
        )

        state = asyncio.run(adapter.probe())

        assert state.provider == "anthropic"
        assert state.availability.available is True
        assert len(state.quota_windows) == 2

    def test_probe_raises_on_non_zero_exit_code(self) -> None:
        async def fake_runner(args, timeout):
            return 1, b"", b"error: not logged in"

        adapter = ClaudeCodeAdapter(subprocess_runner=fake_runner, clock=lambda: UTC_NOW)

        with pytest.raises(ClaudeProcessError) as exc_info:
            asyncio.run(adapter.probe())

        assert exc_info.value.exit_code == 1
        assert "not logged in" in exc_info.value.stderr

    def test_probe_builds_expected_command_line(self) -> None:
        captured_args = {}

        async def fake_runner(args, timeout):
            captured_args["args"] = list(args)
            return 0, b'{"type": "result", "is_error": false, "result": "ok"}\n', b""

        adapter = ClaudeCodeAdapter(
            model="haiku",
            prompt="Réponds uniquement STATUS_OK",
            subprocess_runner=fake_runner,
            clock=lambda: UTC_NOW,
        )
        asyncio.run(adapter.probe())

        assert captured_args["args"] == [
            "claude",
            "-p",
            "--model",
            "haiku",
            "--output-format",
            "stream-json",
            "--verbose",
            "Réponds uniquement STATUS_OK",
        ]


class TestDefaultSubprocessRunnerTimeout:
    def test_timeout_kills_process_and_raises(self) -> None:
        # Exercises the real subprocess/timeout wrapper against the local
        # `sleep` binary — no Claude, no network involved.
        with pytest.raises(ClaudeProbeTimeout):
            asyncio.run(_default_subprocess_runner(["sleep", "5"], timeout=0.2))

    def test_fast_process_completes_within_timeout(self) -> None:
        exit_code, stdout, stderr = asyncio.run(
            _default_subprocess_runner(["true"], timeout=5.0)
        )
        assert exit_code == 0

    def test_env_kwarg_is_visible_to_the_subprocess(self) -> None:
        # Real subprocess (python3 -c ...), no Claude/network — proves the
        # new `env` kwarg actually reaches the child process, the mechanism
        # DeepSeek/Kimi reuse (ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY).
        env = {**os.environ, "AIDO_TEST_PROBE_VAR": "hello-from-env"}
        exit_code, stdout, _ = asyncio.run(
            _default_subprocess_runner(
                [sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ.get('AIDO_TEST_PROBE_VAR',''))"],
                timeout=5.0,
                env=env,
            )
        )
        assert exit_code == 0
        assert stdout == b"hello-from-env"

    def test_env_none_still_inherits_the_process_environment(self) -> None:
        # env=None (the default) must remain equivalent to not passing env
        # at all — real Anthropic usage must see no behavior change.
        exit_code, stdout, _ = asyncio.run(
            _default_subprocess_runner(
                [sys.executable, "-c", "import os,sys; sys.stdout.write('yes' if 'PATH' in os.environ else 'no')"],
                timeout=5.0,
            )
        )
        assert exit_code == 0
        assert stdout == b"yes"


class TestProviderNameAndExtraEnvReuse:
    """provider_name/extra_env — the seam DeepSeek/Kimi reuse instead of a
    second, independent adapter (see module docstring, "Reuse beyond real
    Anthropic")."""

    def test_default_provider_name_is_anthropic(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter._run_subprocess is _default_subprocess_runner
        assert adapter._provider_name == "anthropic"

    def test_custom_provider_name_reaches_probed_state(self) -> None:
        fixture_bytes = (FIXTURES_DIR / "claude_stream_allowed.jsonl").read_bytes()

        async def fake_runner(args, timeout):
            return 0, fixture_bytes, b""

        adapter = ClaudeCodeAdapter(
            provider_name="deepseek", subprocess_runner=fake_runner, clock=lambda: UTC_NOW
        )

        state = asyncio.run(adapter.probe())

        assert state.provider == "deepseek"

    def test_extra_env_is_overlaid_onto_the_default_runner(self) -> None:
        adapter = ClaudeCodeAdapter(
            extra_env={"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_API_KEY": "sk-test"}
        )

        assert isinstance(adapter._run_subprocess, functools.partial)
        assert adapter._run_subprocess.func is _default_subprocess_runner
        merged_env = adapter._run_subprocess.keywords["env"]
        assert merged_env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert merged_env["ANTHROPIC_API_KEY"] == "sk-test"
        # Overlaid onto (not replacing) the ambient environment.
        assert merged_env.get("PATH") == os.environ.get("PATH")

    def test_empty_extra_env_behaves_like_no_extra_env(self) -> None:
        adapter = ClaudeCodeAdapter(extra_env={})
        assert adapter._run_subprocess is _default_subprocess_runner

    def test_explicit_subprocess_runner_takes_priority_over_extra_env(self) -> None:
        async def fake_runner(args, timeout):
            return 0, b'{"type": "result", "is_error": false, "result": "ok"}\n', b""

        adapter = ClaudeCodeAdapter(
            subprocess_runner=fake_runner, extra_env={"ANTHROPIC_API_KEY": "sk-test"}
        )

        assert adapter._run_subprocess is fake_runner
