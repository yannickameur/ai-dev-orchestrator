"""ClaudeCodeAdapter — probes Claude Code CLI quota/availability state.

Launches the Claude Code CLI in headless mode with ``stream-json`` output,
parses the resulting JSONL stream, and normalizes it into a
:class:`~orchestrator.providers.contracts.ProviderState`. This adapter only
probes state: it never becomes an execution engine (see
``orchestrator.providers.adapter.ProviderAdapter``).

Reference command (see docs/SPIKE_RALPH.md):

    claude -p --model haiku --output-format stream-json --verbose "<prompt>"

Design notes:

- The stream carries a ``rate_limit_event`` per turn with a ``status`` field
  and native ``unifiedWindows`` (``five_hour``, ``seven_day``). One
  ``QuotaWindow`` is built per native window key present — never a fixed
  count, never a single ``reset_at`` at the ``ProviderState`` level.
- When several ``rate_limit_event`` entries appear in one stream, the LAST
  one is authoritative: it is the most recent observation within the same
  probe, and later events supersede earlier ones (documented rule, see
  ``_select_rate_limit_event``).
- ``observed_at`` is the wall-clock instant the adapter captured the
  completed subprocess output (injectable ``clock``), not a timestamp
  parsed out of the Claude stream — the adapter does not trust or depend on
  the provider's own clock for freshness bookkeeping.
- ``resetsAt`` has been observed as a Unix epoch (seconds) in real captures,
  though ISO-8601 strings are also accepted defensively; both are converted
  to timezone-aware ``datetime`` values.
- Subprocess failures (non-zero exit, timeout, invalid/incomplete JSONL) are
  adapter-level failures that prevent producing a ``ProviderState`` at all,
  so they raise a normalized ``ClaudeProbeError`` subclass rather than being
  folded into ``ProviderAvailability`` — per the ``ProviderAdapter`` contract,
  that field is reserved for *provider-reported* unavailability (quota,
  auth), not for our own inability to observe it.
- ``modelUsage``/cost fields are not parsed: they are list-price diagnostics
  (see constraint in ROADMAP.md — list price is not a real charged spend)
  and nothing in this slice consumes them, so keeping ``ProviderState`` free
  of them avoids contaminating the generic contract with Claude-specific
  fields for no consumer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    UnavailabilityReason,
)

PROVIDER_NAME = "anthropic"
SOURCE = "claude_stream_json"
DEFAULT_MODEL = "haiku"
DEFAULT_PROBE_PROMPT = "Réponds uniquement STATUS_OK"
DEFAULT_TIMEOUT_SECONDS = 60.0

SubprocessRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes, bytes]]]
Clock = Callable[[], datetime]


class ClaudeProbeError(Exception):
    """Base for adapter-level failures that prevent producing a ProviderState."""


class ClaudeProbeTimeout(ClaudeProbeError):
    """The claude subprocess did not complete within the allotted timeout."""


class ClaudeProcessError(ClaudeProbeError):
    """The claude subprocess exited with a non-zero exit code."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        super().__init__(f"claude exited with code {exit_code}: {stderr.strip()}")
        self.exit_code = exit_code
        self.stderr = stderr


class ClaudeStreamParseError(ClaudeProbeError):
    """The claude stdout stream is not valid/complete stream-json output."""


async def _default_subprocess_runner(
    args: Sequence[str], timeout: float
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ClaudeProbeTimeout(
            f"claude probe timed out after {timeout}s: {' '.join(args)!r}"
        )
    return process.returncode, stdout, stderr


class ClaudeCodeAdapter(ProviderAdapter):
    """Probes Claude Code CLI state via a bounded headless ``-p`` invocation."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        prompt: str = DEFAULT_PROBE_PROMPT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        claude_binary: str = "claude",
        subprocess_runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._model = model
        self._prompt = prompt
        self._timeout_seconds = timeout_seconds
        self._claude_binary = claude_binary
        self._run_subprocess = subprocess_runner or _default_subprocess_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def probe(self) -> ProviderState:
        args = [
            self._claude_binary,
            "-p",
            "--model",
            self._model,
            "--output-format",
            "stream-json",
            "--verbose",
            self._prompt,
        ]
        exit_code, stdout, stderr = await self._run_subprocess(args, self._timeout_seconds)
        observed_at = self._clock()
        if exit_code != 0:
            raise ClaudeProcessError(exit_code, stderr.decode("utf-8", errors="replace"))
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        return parse_claude_stream(lines, observed_at=observed_at)


def parse_claude_stream(lines: Sequence[str], *, observed_at: datetime) -> ProviderState:
    """Parse Claude Code's stream-json lines into a normalized ProviderState.

    Pure function, no subprocess/network involved — used both by
    :meth:`ClaudeCodeAdapter.probe` and directly by offline tests.
    """
    events = _parse_json_lines(lines)

    result_events = [event for event in events if event.get("type") == "result"]
    if not result_events:
        raise ClaudeStreamParseError(
            "incomplete Claude stream: no terminal 'result' event found"
        )

    rate_limit_events = [event for event in events if event.get("type") == "rate_limit_event"]

    if rate_limit_events:
        chosen = _select_rate_limit_event(rate_limit_events)
        rate_limit_info = chosen.get("rate_limit_info", {})
        availability = _availability_from_status(rate_limit_info.get("status"), observed_at)
        quota_windows = _quota_windows_from_unified_windows(
            rate_limit_info.get("unifiedWindows", {}), observed_at
        )
    else:
        availability = _availability_from_result_fallback(
            result_events[-1].get("is_error"), observed_at
        )
        quota_windows = ()

    return ProviderState(
        provider=PROVIDER_NAME,
        availability=availability,
        observed_at=observed_at,
        quota_windows=quota_windows,
    )


def _parse_json_lines(lines: Sequence[str]) -> list[dict]:
    events = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ClaudeStreamParseError(f"invalid JSON line in Claude stream: {exc}") from exc
    return events


def _select_rate_limit_event(rate_limit_events: Sequence[dict]) -> dict:
    # Documented rule: the LAST rate_limit_event in the stream is the most
    # recent observation and supersedes any earlier one in the same probe.
    return rate_limit_events[-1]


def _availability_from_status(
    status: str | None, observed_at: datetime
) -> ProviderAvailability:
    if status is None:
        return ProviderAvailability(
            available=False, observed_at=observed_at, reason=UnavailabilityReason.UNKNOWN
        )
    if status.lower() == "allowed":
        return ProviderAvailability(available=True, observed_at=observed_at)
    return ProviderAvailability(
        available=False,
        observed_at=observed_at,
        reason=UnavailabilityReason.QUOTA_EXHAUSTED,
    )


def _availability_from_result_fallback(
    is_error: bool | None, observed_at: datetime
) -> ProviderAvailability:
    # No rate_limit_event was observed at all: fall back to the terminal
    # result's own error flag as the only remaining availability signal.
    if is_error is False:
        return ProviderAvailability(available=True, observed_at=observed_at)
    if is_error is True:
        return ProviderAvailability(
            available=False,
            observed_at=observed_at,
            reason=UnavailabilityReason.PROVIDER_ERROR,
        )
    return ProviderAvailability(
        available=False, observed_at=observed_at, reason=UnavailabilityReason.UNKNOWN
    )


def _quota_windows_from_unified_windows(
    unified_windows: dict, observed_at: datetime
) -> tuple[QuotaWindow, ...]:
    windows = []
    for window_type, payload in unified_windows.items():
        if not isinstance(payload, dict):
            continue
        windows.append(
            QuotaWindow(
                window_type=window_type,
                source=SOURCE,
                observed_at=observed_at,
                utilization=payload.get("utilization"),
                reset_at=_parse_reset_at(payload.get("resetsAt")),
            )
        )
    return tuple(windows)


def _parse_reset_at(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ClaudeStreamParseError(f"unexpected resetsAt type: {type(value)!r}")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise ClaudeStreamParseError(f"unexpected resetsAt type: {type(value)!r}")
