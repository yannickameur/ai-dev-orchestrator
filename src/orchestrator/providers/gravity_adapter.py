"""GravityAdapter — probes Gravity (`agy`) quota/availability via the real
`/usage` read-only slash command.

VERIFIED real (ROADMAP.md, P20, ``agy`` 1.2.12, on the maintainer's
machine): ``agy -p "/usage" --output-format json`` is recognized as a
genuine slash command (the response carries a top-level ``command.name ==
"usage"`` key, never present for a normal conversational turn) and, unlike
a conversational probe prompt, consumes **no** model turn at all —
observed directly: ``num_turns: 0`` and every ``usage.*_tokens`` field
``0`` in the real response. This is therefore no longer
EXECUTION_PROBE_ONLY (P19's original, more conservative design, when
``/usage`` had not yet been checked): real quota telemetry is available
and used, exactly like ``ClaudeCodeAdapter``/``CodexAdapter`` already do
for their own providers — never fabricated, never left as a guessed
percentage.

Real ``/usage`` response shape observed (P20, ``agy`` 1.2.12 — **not**
documented anywhere as a stable public API contract, so every field below
is read defensively; a missing/malformed field degrades to ``None``,
never a fabricated value, and never crashes the whole probe):

```json
{
  "status": "SUCCESS",
  "command": {
    "name": "usage",
    "data": {
      "groups": [
        {
          "name": "Gemini Models",
          "buckets": [
            {
              "id": "gemini-weekly",
              "window": "weekly",
              "remaining_fraction": 0.9273071885108948,
              "reset_time": "2026-10-05T14:55:49Z"
            }
          ]
        },
        {"name": "Claude and GPT models", "buckets": [{"id": "3p-weekly", ...}]}
      ]
    }
  }
}
```

Mapping, per bucket, to one ``QuotaWindow``:

- ``window_type`` <- the bucket's own ``id`` (e.g. ``"3p-weekly"``), not
  the bare ``window`` field (``"weekly"`` for every bucket observed,
  which would collide across groups and silently discard the one field
  that actually distinguishes them — the real per-model-group identifier
  Gravity itself already provides, exactly the role Claude's own
  ``five_hour``/``seven_day`` window_type strings play). Falls back to
  ``window`` only if ``id`` is missing; a bucket with neither is skipped
  rather than given a fabricated label.
- ``utilization`` <- ``1.0 - remaining_fraction`` when ``remaining_fraction``
  is a real number in ``[0.0, 1.0]``; ``None`` (never ``0.0``) otherwise.
- ``reset_at`` <- ``reset_time`` parsed as an ISO-8601 timestamp;
  ``None`` if absent or unparseable.

A whole account has one shared quota regardless of how many Gravity
workers exist (``gravity_primary``/``gravity_secondary`` — see
ROADMAP.md, P20): both share this exact same ``ProviderState``, probed
once, exactly like Alice/Lydie already share one Anthropic quota.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, QuotaWindow, UnavailabilityReason

PROVIDER_NAME = "gravity"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_GRAVITY_BINARY = "agy"
SOURCE = "agy_usage_command"

SubprocessRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes, bytes]]]
Clock = Callable[[], datetime]

# Deliberately fragile, best-effort text classification — see module
# docstring of the original probe design (still used for a non-zero exit
# or an explicit "status": "ERROR"). Anything not matched here stays
# UNKNOWN; never guessed as QUOTA_EXHAUSTED without a real observed
# pattern to justify it.
_RATE_LIMIT_MARKERS = ("rate limit", "429", "quota", "too many requests")
_AUTH_ERROR_MARKERS = ("unauthorized", "401", "not authenticated", "api key", "authentication")


class GravityProbeError(Exception):
    """Base for adapter-level failures that prevent producing a ProviderState."""


class GravityProbeTimeout(GravityProbeError):
    """The agy subprocess did not complete within the allotted timeout."""


class GravityProbeMalformedResponseError(GravityProbeError):
    """``agy`` exited 0 but its stdout was not parseable JSON at all — a
    genuine adapter-level anomaly (the process itself is reachable, but
    this adapter cannot honestly produce a ``ProviderState`` from it),
    never silently downgraded to "unavailable"."""


def _classify_failure_reason(stdout_text: str, stderr_text: str) -> UnavailabilityReason:
    combined = f"{stdout_text}\n{stderr_text}".lower()
    if any(marker in combined for marker in _RATE_LIMIT_MARKERS):
        return UnavailabilityReason.QUOTA_EXHAUSTED
    if any(marker in combined for marker in _AUTH_ERROR_MARKERS):
        return UnavailabilityReason.AUTH_ERROR
    return UnavailabilityReason.UNKNOWN


def _parse_reset_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _quota_windows_from_usage_payload(payload: dict, *, observed_at: datetime) -> tuple[QuotaWindow, ...]:
    """Tolerant, defensive walk of the real (undocumented-as-stable)
    ``/usage`` shape — a missing/unexpected field at any level simply
    yields fewer windows, never a fabricated one, never a crash."""
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return ()

    windows: list[QuotaWindow] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            window_type = bucket.get("id") or bucket.get("window")
            if not isinstance(window_type, str) or not window_type:
                continue  # no honest label available for this bucket — skip it, never fabricate one

            remaining_fraction = bucket.get("remaining_fraction")
            utilization = None
            if isinstance(remaining_fraction, (int, float)) and not isinstance(remaining_fraction, bool):
                if 0.0 <= remaining_fraction <= 1.0:
                    utilization = 1.0 - remaining_fraction

            windows.append(
                QuotaWindow(
                    window_type=window_type,
                    source=SOURCE,
                    observed_at=observed_at,
                    utilization=utilization,
                    reset_at=_parse_reset_time(bucket.get("reset_time")),
                )
            )
    return tuple(windows)


async def _default_subprocess_runner(args: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
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
        raise GravityProbeTimeout(f"gravity probe timed out after {timeout}s: {' '.join(args)!r}")
    return process.returncode, stdout, stderr


class GravityAdapter(ProviderAdapter):
    """Probes `agy` state via the real, read-only ``/usage`` slash
    command — no conversational prompt, no model turn, no `--mode=
    accept-edits` needed (nothing is ever edited by a probe)."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        gravity_binary: str = DEFAULT_GRAVITY_BINARY,
        subprocess_runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._gravity_binary = gravity_binary
        self._run_subprocess = subprocess_runner or _default_subprocess_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def probe(self) -> ProviderState:
        args = [self._gravity_binary, "-p", "/usage", "--output-format", "json"]
        exit_code, stdout, stderr = await self._run_subprocess(args, self._timeout_seconds)
        observed_at = self._clock()

        if exit_code != 0:
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            reason = _classify_failure_reason(stdout_text, stderr_text)
            return ProviderState(
                provider=PROVIDER_NAME,
                availability=ProviderAvailability(available=False, observed_at=observed_at, reason=reason),
                observed_at=observed_at,
                quota_windows=(),
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise GravityProbeMalformedResponseError(
                f"agy /usage exited 0 but stdout was not valid JSON: {stdout_text[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise GravityProbeMalformedResponseError(
                f"agy /usage returned valid JSON that was not an object: {type(payload)!r}"
            )

        if payload.get("status") != "SUCCESS":
            stderr_text = stderr.decode("utf-8", errors="replace")
            error_text = str(payload.get("error") or "")
            reason = _classify_failure_reason(f"{stdout_text}\n{error_text}", stderr_text)
            return ProviderState(
                provider=PROVIDER_NAME,
                availability=ProviderAvailability(available=False, observed_at=observed_at, reason=reason),
                observed_at=observed_at,
                quota_windows=(),
            )

        windows = _quota_windows_from_usage_payload(payload, observed_at=observed_at)
        # A bucket with no remaining fraction makes every model of its group
        # fail with HTTP 429 (observed with agy 1.3.3): like an exhausted
        # Claude window, one exhausted observed window makes the shared
        # account unavailable rather than letting Ralph loop on 429s.
        if any(window.utilization is not None and window.utilization >= 1.0 for window in windows):
            availability = ProviderAvailability(
                available=False, observed_at=observed_at, reason=UnavailabilityReason.QUOTA_EXHAUSTED,
            )
        else:
            availability = ProviderAvailability(available=True, observed_at=observed_at)
        return ProviderState(
            provider=PROVIDER_NAME,
            availability=availability,
            observed_at=observed_at,
            quota_windows=windows,
        )
