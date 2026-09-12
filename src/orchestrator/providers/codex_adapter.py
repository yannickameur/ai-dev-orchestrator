"""CodexAdapter — probes Codex app-server quota/availability state.

Speaks a minimal, bounded subset of the Codex ``app-server`` JSON-RPC-over-
stdio protocol to perform exactly one read-only round trip:

    1. spawn ``codex app-server``
    2. send ``initialize``, await its response
    3. send the ``initialized`` notification
    4. send ``account/rateLimits/read``, await its response
    5. terminate the subprocess cleanly

This adapter only probes state: it never becomes an execution engine (see
``orchestrator.providers.adapter.ProviderAdapter``), and it never calls any
method other than ``account/rateLimits/read``. In particular it must never
call ``account/usage/read`` or ``account/rateLimitResetCredit/consume`` —
this probe is strictly read-only, and normalizing a reset credit's presence
is never the same thing as consuming one (``ResetCredit.auto_consume`` is
pinned to ``False`` by the contract itself; this adapter defines no
consumption method at all).

Design notes:

- The JSON-RPC exchange is deliberately NOT a generic JSON-RPC client: it
  only implements the fixed 4-message sequence above, with a small,
  explicit message loop that tolerates unrelated server notifications
  (e.g. ``remoteControl/status/changed``) arriving between the expected
  responses, bounded by ``_MAX_LINES_PER_PHASE`` so a chatty or confused
  server can never cause an infinite read loop.
- The whole exchange is wrapped in a single bounded timeout
  (``timeout_seconds``); on timeout the subprocess is killed and a
  normalized ``CodexProbeTimeout`` is raised. There is no per-line timeout
  layered on top — one overall bound is enough and easier to reason about.
- Native ``rateLimits.primary``/``rateLimits.secondary`` map to
  ``QuotaWindow`` types ``primary_5h``/``secondary_7d`` (see
  docs/SPIKE_RALPH.md and tests/providers/test_contracts.py for this
  established naming) — one window is built per native key that is
  actually present, never assuming both must exist.
- ``usedPercent`` (an integer 0-100) is converted to a ``utilization``
  fraction in [0.0, 1.0]; a real zero stays ``0.0`` while a genuinely
  absent value stays ``None``.
- ``ordinaryUsageAllowed`` is the canonical availability signal:
  ``True`` → available; ``False`` → unavailable/``QUOTA_EXHAUSTED``;
  missing or not a bool → unavailable/``UNKNOWN`` (a client must never
  infer availability from percentages/reset times alone — see the native
  schema's own doc comment on this field).
- Reset credits are normalized descriptively only: this module never
  exposes a way to consume one, matching the project-wide
  ``ResetCredit.auto_consume=False`` invariant.
- ``observed_at`` is the wall-clock instant the adapter captured the
  completed exchange (injectable ``clock``), never a timestamp taken from
  the Codex response itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    ResetCredit,
    ResetCreditStatus,
    UnavailabilityReason,
)

PROVIDER_NAME = "openai"
SOURCE = "codex_app_server"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CLIENT_NAME = "ai-dev-orchestrator"
DEFAULT_CLIENT_VERSION = "0.1.0"

_INIT_REQUEST_ID = 1
_RATE_LIMITS_REQUEST_ID = 2
_MAX_LINES_PER_PHASE = 25

# Native rateLimits.<key> -> normalized QuotaWindow.window_type. Confirmed
# native windows are a rolling 5h window and a 7-day window (see
# docs/SPIKE_RALPH.md) — never write "5m" here, that would be a 5-minute
# window, not the observed 5-hour one.
_WINDOW_LABELS = {"primary": "primary_5h", "secondary": "secondary_7d"}

_RESET_CREDIT_STATUS_MAP = {
    "available": ResetCreditStatus.AVAILABLE,
    "redeemed": ResetCreditStatus.CONSUMED,
    # "redeeming" is a transitional state: neither cleanly available nor
    # consumed, so it is normalized to UNKNOWN rather than guessed either way.
    "redeeming": ResetCreditStatus.UNKNOWN,
    "unknown": ResetCreditStatus.UNKNOWN,
}

Transport = Callable[..., Awaitable[dict]]
Clock = Callable[[], datetime]


class CodexProbeError(Exception):
    """Base for adapter-level failures that prevent producing a ProviderState."""


class CodexProbeTimeout(CodexProbeError):
    """The codex app-server exchange did not complete within the timeout."""


class CodexProcessError(CodexProbeError):
    """The codex app-server subprocess exited before completing the exchange."""

    def __init__(self, exit_code: int | None, stderr: str) -> None:
        super().__init__(f"codex app-server exited with code {exit_code}: {stderr.strip()}")
        self.exit_code = exit_code
        self.stderr = stderr


class CodexProtocolError(CodexProbeError):
    """The codex app-server produced an invalid/unexpected JSON-RPC exchange."""


class CodexAdapter(ProviderAdapter):
    """Probes Codex app-server state via a bounded, read-only JSON-RPC exchange."""

    def __init__(
        self,
        *,
        codex_binary: str = "codex",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client_name: str = DEFAULT_CLIENT_NAME,
        client_version: str = DEFAULT_CLIENT_VERSION,
        transport: Transport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._codex_binary = codex_binary
        self._timeout_seconds = timeout_seconds
        self._client_name = client_name
        self._client_version = client_version
        self._transport = transport or _default_transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def probe(self) -> ProviderState:
        result = await self._transport(
            codex_binary=self._codex_binary,
            client_name=self._client_name,
            client_version=self._client_version,
            timeout_seconds=self._timeout_seconds,
        )
        observed_at = self._clock()
        return parse_rate_limits_result(result, observed_at=observed_at)


def parse_rate_limits_result(result: dict, *, observed_at: datetime) -> ProviderState:
    """Normalize a raw ``account/rateLimits/read`` result into a ProviderState.

    Pure function, no subprocess/network involved — used both by
    :meth:`CodexAdapter.probe` and directly by offline tests.
    """
    availability = _availability_from_ordinary_usage_allowed(
        result.get("ordinaryUsageAllowed"), observed_at
    )

    rate_limits = result.get("rateLimits")
    quota_windows = (
        _quota_windows_from_rate_limits(rate_limits, observed_at)
        if isinstance(rate_limits, dict)
        else ()
    )

    reset_credits = _reset_credits_from_summary(result.get("rateLimitResetCredits"))

    return ProviderState(
        provider=PROVIDER_NAME,
        availability=availability,
        observed_at=observed_at,
        quota_windows=quota_windows,
        reset_credits=reset_credits,
    )


def _availability_from_ordinary_usage_allowed(
    value: object, observed_at: datetime
) -> ProviderAvailability:
    if value is True:
        return ProviderAvailability(available=True, observed_at=observed_at)
    if value is False:
        return ProviderAvailability(
            available=False,
            observed_at=observed_at,
            reason=UnavailabilityReason.QUOTA_EXHAUSTED,
        )
    # Missing or not a bool (e.g. null "unavailable" per the native schema):
    # never infer availability from percentages/reset times alone.
    return ProviderAvailability(
        available=False, observed_at=observed_at, reason=UnavailabilityReason.UNKNOWN
    )


def _quota_windows_from_rate_limits(
    rate_limits: dict, observed_at: datetime
) -> tuple[QuotaWindow, ...]:
    windows = []
    for native_key, window_type in _WINDOW_LABELS.items():
        payload = rate_limits.get(native_key)
        if not isinstance(payload, dict):
            continue
        windows.append(
            QuotaWindow(
                window_type=window_type,
                source=SOURCE,
                observed_at=observed_at,
                utilization=_utilization_from_used_percent(payload.get("usedPercent")),
                reset_at=_parse_reset_at(payload.get("resetsAt")),
            )
        )
    return tuple(windows)


def _utilization_from_used_percent(used_percent: object) -> float | None:
    if not isinstance(used_percent, (int, float)) or isinstance(used_percent, bool):
        return None
    return used_percent / 100.0


def _parse_reset_at(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CodexProtocolError(f"unexpected resetsAt type: {type(value)!r}")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raise CodexProtocolError(f"unexpected resetsAt type: {type(value)!r}")


def _reset_credits_from_summary(summary: object) -> tuple[ResetCredit, ...]:
    if not isinstance(summary, dict):
        return ()

    credits_detail = summary.get("credits")
    if isinstance(credits_detail, list) and credits_detail:
        return tuple(
            ResetCredit(
                title=_reset_credit_title(entry),
                status=_reset_credit_status(entry.get("status")),
                auto_consume=False,
            )
            for entry in credits_detail
            if isinstance(entry, dict)
        )

    # No per-credit detail was returned (native "credits: null" case) — fall
    # back to the aggregate count only, if any credit is actually available.
    available_count = summary.get("availableCount")
    if isinstance(available_count, int) and not isinstance(available_count, bool) and available_count > 0:
        return (
            ResetCredit(
                title="Codex reset credit",
                status=ResetCreditStatus.AVAILABLE,
                available_count=available_count,
                auto_consume=False,
            ),
        )
    return ()


def _reset_credit_title(entry: dict) -> str:
    return entry.get("title") or entry.get("description") or "Codex reset credit"


def _reset_credit_status(value: object) -> ResetCreditStatus:
    if isinstance(value, str):
        return _RESET_CREDIT_STATUS_MAP.get(value, ResetCreditStatus.UNKNOWN)
    return ResetCreditStatus.UNKNOWN


# --- Subprocess / JSON-RPC transport -----------------------------------


async def _default_transport(
    *, codex_binary: str, client_name: str, client_version: str, timeout_seconds: float
) -> dict:
    process = await asyncio.create_subprocess_exec(
        codex_binary,
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await _run_with_timeout(
            process,
            _run_rate_limits_exchange(
                process, client_name=client_name, client_version=client_version
            ),
            timeout_seconds,
        )
    finally:
        await _terminate_cleanly(process)


async def _run_with_timeout(process: Any, coro: Awaitable[dict], timeout_seconds: float) -> dict:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise CodexProbeTimeout(
            f"codex app-server probe timed out after {timeout_seconds}s"
        )


async def _run_rate_limits_exchange(process: Any, *, client_name: str, client_version: str) -> dict:
    await _write_message(
        process,
        {
            "id": _INIT_REQUEST_ID,
            "method": "initialize",
            "params": {"clientInfo": {"name": client_name, "version": client_version}},
        },
    )
    await _await_response(process, _INIT_REQUEST_ID)

    await _write_message(process, {"method": "initialized"})

    await _write_message(
        process,
        {"id": _RATE_LIMITS_REQUEST_ID, "method": "account/rateLimits/read", "params": None},
    )
    return await _await_response(process, _RATE_LIMITS_REQUEST_ID)


async def _write_message(process: Any, message: dict) -> None:
    line = json.dumps(message) + "\n"
    process.stdin.write(line.encode("utf-8"))
    await process.stdin.drain()


async def _await_response(process: Any, expected_id: int) -> dict:
    for _ in range(_MAX_LINES_PER_PHASE):
        raw = await process.stdout.readline()
        if raw == b"":
            exit_code = await process.wait()
            stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
            raise CodexProcessError(exit_code, stderr)

        try:
            message = json.loads(raw.decode("utf-8", errors="replace").strip())
        except json.JSONDecodeError as exc:
            raise CodexProtocolError(
                f"invalid JSON from codex app-server: {exc}"
            ) from exc

        if not isinstance(message, dict) or message.get("id") != expected_id:
            # Unrelated notification (e.g. remoteControl/status/changed) or a
            # message for a different request id — ignore and keep reading.
            continue
        if "error" in message:
            raise CodexProtocolError(
                f"codex app-server returned an error: {message['error']!r}"
            )
        if "result" not in message:
            raise CodexProtocolError(
                f"malformed JSON-RPC response, missing 'result': {message!r}"
            )
        return message["result"]

    raise CodexProtocolError(
        f"no response with id={expected_id} after {_MAX_LINES_PER_PHASE} lines"
    )


async def _terminate_cleanly(process: Any) -> None:
    if process.returncode is not None:
        return
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
