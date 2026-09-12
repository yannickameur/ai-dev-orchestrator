"""Tests for CodexAdapter (Phase 1 / Slice 2).

All tests are offline: no network call, no real Codex app-server
invocation. The one real fixture (``fixtures/codex_rate_limits_allowed.json``)
was captured once from a real, strictly read-only
``account/rateLimits/read`` call against ``codex app-server`` (no
``account/usage/read``, no reset credit consumption — see ROADMAP.md /
commit history) and cleaned of any account-identifying data before being
committed.

Low-level JSON-RPC/subprocess mechanics are tested against small in-test
fake process doubles (``_FakeProcess``) rather than a real subprocess or
Codex binary, mirroring the offline-fake pattern already used in
``tests/providers/test_adapter.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.providers.codex_adapter import (
    CodexAdapter,
    CodexProbeTimeout,
    CodexProcessError,
    CodexProtocolError,
    _await_response,
    _run_with_timeout,
    parse_rate_limits_result,
)
from orchestrator.providers.contracts import ResetCreditStatus, UnavailabilityReason

FIXTURES_DIR = Path(__file__).parent / "fixtures"
UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _read_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


class TestParseRateLimitsRealFixture:
    def test_ordinary_usage_allowed_yields_available_provider_state(self) -> None:
        result = _read_fixture("codex_rate_limits_allowed.json")

        state = parse_rate_limits_result(result, observed_at=UTC_NOW)

        assert state.provider == "openai"
        assert state.availability.available is True
        assert state.availability.reason is None
        assert state.observed_at == UTC_NOW

    def test_primary_and_secondary_windows_are_parsed(self) -> None:
        result = _read_fixture("codex_rate_limits_allowed.json")

        state = parse_rate_limits_result(result, observed_at=UTC_NOW)

        window_types = {window.window_type for window in state.quota_windows}
        assert window_types == {"primary_5h", "secondary_7d"}

        primary = next(w for w in state.quota_windows if w.window_type == "primary_5h")
        secondary = next(w for w in state.quota_windows if w.window_type == "secondary_7d")
        assert primary.utilization == 0.5
        assert secondary.utilization == 0.23
        assert primary.source == "codex_app_server"

    def test_reset_at_is_timezone_aware(self) -> None:
        result = _read_fixture("codex_rate_limits_allowed.json")

        state = parse_rate_limits_result(result, observed_at=UTC_NOW)

        primary = next(w for w in state.quota_windows if w.window_type == "primary_5h")
        assert primary.reset_at is not None
        assert primary.reset_at.tzinfo is not None
        assert primary.reset_at == datetime.fromtimestamp(1789236783, tz=timezone.utc)

    def test_reset_credits_present_are_normalized(self) -> None:
        result = _read_fixture("codex_rate_limits_allowed.json")

        state = parse_rate_limits_result(result, observed_at=UTC_NOW)

        assert len(state.reset_credits) == 1
        credit = state.reset_credits[0]
        assert credit.title == "Full reset (Weekly + 5 hr)"
        assert credit.status is ResetCreditStatus.AVAILABLE
        assert credit.auto_consume is False


class TestOrdinaryUsageAllowed:
    def test_true_is_available(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": True, "rateLimits": {}}, observed_at=UTC_NOW
        )
        assert state.availability.available is True
        assert state.availability.reason is None

    def test_false_is_quota_exhausted(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": False, "rateLimits": {}}, observed_at=UTC_NOW
        )
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_missing_is_unknown(self) -> None:
        state = parse_rate_limits_result({"rateLimits": {}}, observed_at=UTC_NOW)
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.UNKNOWN

    def test_incoherent_value_is_unknown(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": "yes", "rateLimits": {}}, observed_at=UTC_NOW
        )
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.UNKNOWN


class TestUtilizationHandling:
    def test_zero_used_percent_is_kept_as_real_zero(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": {"usedPercent": 0}},
            },
            observed_at=UTC_NOW,
        )
        window = state.quota_windows[0]
        assert window.utilization == 0.0
        assert window.utilization is not None

    def test_absent_used_percent_stays_none(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {}}},
            observed_at=UTC_NOW,
        )
        window = state.quota_windows[0]
        assert window.utilization is None

    def test_missing_window_key_produces_no_window(self) -> None:
        # Rule: normalize from what the native response actually contains,
        # never assume both primary and secondary must be present.
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": {"usedPercent": 10}},
            },
            observed_at=UTC_NOW,
        )
        assert len(state.quota_windows) == 1
        assert state.quota_windows[0].window_type == "primary_5h"

    def test_no_rate_limits_object_yields_no_windows(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": True}, observed_at=UTC_NOW
        )
        assert state.quota_windows == ()


class TestResetCredits:
    def test_reset_credits_absent_entirely(self) -> None:
        state = parse_rate_limits_result(
            {"ordinaryUsageAllowed": True, "rateLimits": {}}, observed_at=UTC_NOW
        )
        assert state.reset_credits == ()

    def test_reset_credits_null_summary(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {},
                "rateLimitResetCredits": None,
            },
            observed_at=UTC_NOW,
        )
        assert state.reset_credits == ()

    def test_available_count_only_without_credit_detail(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {},
                "rateLimitResetCredits": {"availableCount": 2, "credits": None},
            },
            observed_at=UTC_NOW,
        )
        assert len(state.reset_credits) == 1
        assert state.reset_credits[0].available_count == 2
        assert state.reset_credits[0].status is ResetCreditStatus.AVAILABLE

    def test_zero_available_count_yields_no_credits(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {},
                "rateLimitResetCredits": {"availableCount": 0, "credits": None},
            },
            observed_at=UTC_NOW,
        )
        assert state.reset_credits == ()

    def test_redeemed_credit_is_consumed(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {},
                "rateLimitResetCredits": {
                    "availableCount": 0,
                    "credits": [{"status": "redeemed", "title": "Reset"}],
                },
            },
            observed_at=UTC_NOW,
        )
        assert state.reset_credits[0].status is ResetCreditStatus.CONSUMED

    def test_auto_consume_is_always_false(self) -> None:
        state = parse_rate_limits_result(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {},
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [{"status": "available", "title": "Reset"}],
                },
            },
            observed_at=UTC_NOW,
        )
        assert all(credit.auto_consume is False for credit in state.reset_credits)

    def test_codex_adapter_exposes_no_consumption_method(self) -> None:
        forbidden = {"consume", "consume_reset_credit", "reset"}
        declared = set(vars(CodexAdapter))
        assert forbidden.isdisjoint(declared)


class TestIncompleteOrInvalidResponse:
    def test_missing_ordinary_usage_allowed_and_rate_limits(self) -> None:
        # An incomplete-but-still-a-dict response degrades to UNKNOWN/no
        # windows rather than raising — the shape itself is still valid
        # JSON, just sparse.
        state = parse_rate_limits_result({}, observed_at=UTC_NOW)
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.UNKNOWN
        assert state.quota_windows == ()


class _FakeStreamWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class _FakeStreamReader:
    def __init__(
        self, lines: list[bytes] | None = None, *, hang: bool = False, read_data: bytes = b""
    ) -> None:
        self._lines = list(lines or [])
        self._hang = hang
        self._read_data = read_data

    async def readline(self) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self, _n: int = -1) -> bytes:
        return self._read_data


class _FakeProcess:
    def __init__(
        self,
        lines: list[bytes] | None = None,
        *,
        hang: bool = False,
        returncode_after_eof: int | None = 1,
        stderr: bytes = b"",
    ) -> None:
        self.stdin = _FakeStreamWriter()
        self.stdout = _FakeStreamReader(lines, hang=hang)
        self.stderr = _FakeStreamReader(read_data=stderr)
        self._returncode_after_eof = returncode_after_eof
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._returncode_after_eof
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


class TestAwaitResponse:
    def test_returns_result_for_matching_id(self) -> None:
        process = _FakeProcess(lines=[_line({"id": 2, "result": {"ok": True}})])

        result = asyncio.run(_await_response(process, 2))

        assert result == {"ok": True}

    def test_skips_unrelated_notifications(self) -> None:
        process = _FakeProcess(
            lines=[
                _line({"method": "remoteControl/status/changed", "params": {}}),
                _line({"id": 1, "result": {"unused": True}}),
                _line({"id": 2, "result": {"ok": True}}),
            ]
        )

        result = asyncio.run(_await_response(process, 2))

        assert result == {"ok": True}

    def test_raises_on_invalid_json(self) -> None:
        process = _FakeProcess(lines=[b"{not valid json\n"])

        with pytest.raises(CodexProtocolError, match="invalid JSON"):
            asyncio.run(_await_response(process, 2))

    def test_raises_on_error_response(self) -> None:
        process = _FakeProcess(
            lines=[_line({"id": 2, "error": {"code": -1, "message": "boom"}})]
        )

        with pytest.raises(CodexProtocolError, match="returned an error"):
            asyncio.run(_await_response(process, 2))

    def test_raises_on_incomplete_response_missing_result(self) -> None:
        process = _FakeProcess(lines=[_line({"id": 2})])

        with pytest.raises(CodexProtocolError, match="missing 'result'"):
            asyncio.run(_await_response(process, 2))

    def test_raises_process_error_on_early_eof(self) -> None:
        process = _FakeProcess(lines=[], returncode_after_eof=17, stderr=b"auth expired")

        with pytest.raises(CodexProcessError) as exc_info:
            asyncio.run(_await_response(process, 2))

        assert exc_info.value.exit_code == 17
        assert "auth expired" in exc_info.value.stderr

    def test_raises_when_no_matching_response_within_line_budget(self) -> None:
        # Never-ending stream of unrelated notifications: bounded, not an
        # infinite loop.
        process = _FakeProcess(
            lines=[_line({"method": "noise"}) for _ in range(100)]
        )

        with pytest.raises(CodexProtocolError, match="no response with id"):
            asyncio.run(_await_response(process, 2))


class TestRunWithTimeout:
    def test_timeout_kills_process_and_raises(self) -> None:
        process = _FakeProcess(hang=True)

        async def _hang_forever() -> dict:
            await asyncio.sleep(3600)
            return {}

        with pytest.raises(CodexProbeTimeout):
            asyncio.run(_run_with_timeout(process, _hang_forever(), 0.05))

        assert process.killed is True

    def test_fast_coroutine_completes_within_timeout(self) -> None:
        process = _FakeProcess()

        async def _fast() -> dict:
            return {"ok": True}

        result = asyncio.run(_run_with_timeout(process, _fast(), 5.0))

        assert result == {"ok": True}
        assert process.killed is False


class TestAdapterProbeWithInjectedTransport:
    def test_probe_returns_provider_state_on_success(self) -> None:
        fixture = _read_fixture("codex_rate_limits_allowed.json")

        async def fake_transport(**kwargs):
            return fixture

        adapter = CodexAdapter(transport=fake_transport, clock=lambda: UTC_NOW)

        state = asyncio.run(adapter.probe())

        assert state.provider == "openai"
        assert state.availability.available is True
        assert len(state.quota_windows) == 2

    def test_probe_propagates_process_error(self) -> None:
        async def fake_transport(**kwargs):
            raise CodexProcessError(1, "not logged in")

        adapter = CodexAdapter(transport=fake_transport, clock=lambda: UTC_NOW)

        with pytest.raises(CodexProcessError):
            asyncio.run(adapter.probe())

    def test_probe_propagates_timeout(self) -> None:
        async def fake_transport(**kwargs):
            raise CodexProbeTimeout("timed out")

        adapter = CodexAdapter(transport=fake_transport, clock=lambda: UTC_NOW)

        with pytest.raises(CodexProbeTimeout):
            asyncio.run(adapter.probe())

    def test_probe_passes_expected_kwargs_to_transport(self) -> None:
        captured = {}

        async def fake_transport(**kwargs):
            captured.update(kwargs)
            return {"ordinaryUsageAllowed": True, "rateLimits": {}}

        adapter = CodexAdapter(
            codex_binary="codex",
            client_name="ai-dev-orchestrator",
            client_version="0.1.0",
            timeout_seconds=12.0,
            transport=fake_transport,
            clock=lambda: UTC_NOW,
        )
        asyncio.run(adapter.probe())

        assert captured == {
            "codex_binary": "codex",
            "client_name": "ai-dev-orchestrator",
            "client_version": "0.1.0",
            "timeout_seconds": 12.0,
        }
