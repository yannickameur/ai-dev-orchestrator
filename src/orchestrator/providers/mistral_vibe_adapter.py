"""MistralVibeAdapter — probes Vibe CLI availability via a minimal real call.

EXECUTION_PROBE_ONLY (see docs/VIBE_SPIKE.md §9, §12): unlike
``ClaudeCodeAdapter``/``CodexAdapter``, Vibe exposes **no** structured
quota/rate-limit telemetry anywhere — confirmed both by a real spike
execution (zero `usage`/`token`/`quota`/`rate_limit` fields in its
``--output json`` transcript) and by official Mistral documentation (no
CLI command or method to query current quota/usage/reset). The only
honest signal available is whether one minimal, bounded, real call
succeeds or fails.

This adapter never fabricates what it cannot know:

- ``quota_windows`` is always empty — there is no native window data to
  normalize, so none is invented.
- ``reset_at`` is never populated (there is no ``QuotaWindow`` to carry one).
- A failure's ``reason`` is ``UNKNOWN`` unless stderr/stdout matches one of
  a small, explicitly fragile set of generic auth/rate-limit text markers
  (below). These are common CLI/HTTP conventions, not confirmed
  Mistral-specific behavior — a real Vibe failure was never observed
  during the spike, so this classification is best-effort only and must
  never be treated as authoritative.

Reference command (see docs/VIBE_SPIKE.md, real spike evidence):

    vibe -p "<prompt>" --output json --trust --auto-approve --max-turns 1
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason

PROVIDER_NAME = "mistral"
DEFAULT_PROBE_PROMPT = "Reply with exactly: OK"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_VIBE_BINARY = "vibe"
DEFAULT_MAX_TURNS = "1"

SubprocessRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes, bytes]]]
Clock = Callable[[], datetime]

# Deliberately fragile, best-effort text classification — see module
# docstring. Anything not matched here stays UNKNOWN; never guessed as
# QUOTA_EXHAUSTED without a real observed pattern to justify it.
_RATE_LIMIT_MARKERS = ("rate limit", "429", "quota", "too many requests")
_AUTH_ERROR_MARKERS = ("unauthorized", "401", "not authenticated", "api key", "authentication")


class VibeProbeError(Exception):
    """Base for adapter-level failures that prevent producing a ProviderState."""


class VibeProbeTimeout(VibeProbeError):
    """The vibe subprocess did not complete within the allotted timeout."""


def _classify_failure_reason(stdout_text: str, stderr_text: str) -> UnavailabilityReason:
    combined = f"{stdout_text}\n{stderr_text}".lower()
    if any(marker in combined for marker in _RATE_LIMIT_MARKERS):
        return UnavailabilityReason.QUOTA_EXHAUSTED
    if any(marker in combined for marker in _AUTH_ERROR_MARKERS):
        return UnavailabilityReason.AUTH_ERROR
    return UnavailabilityReason.UNKNOWN


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
        raise VibeProbeTimeout(f"vibe probe timed out after {timeout}s: {' '.join(args)!r}")
    return process.returncode, stdout, stderr


class MistralVibeAdapter(ProviderAdapter):
    """Probes Vibe CLI state via one bounded, tool-free, non-destructive `-p` call.

    The probe prompt is purely conversational (no file/shell tool call is
    requested by it), so no ``--disabled-tools`` flag is relied upon to
    keep the probe side-effect-free — this was a deliberate simplification
    over guessing an unverified tool-filtering flag shape.
    """

    def __init__(
        self,
        *,
        prompt: str = DEFAULT_PROBE_PROMPT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        vibe_binary: str = DEFAULT_VIBE_BINARY,
        subprocess_runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._prompt = prompt
        self._timeout_seconds = timeout_seconds
        self._vibe_binary = vibe_binary
        self._run_subprocess = subprocess_runner or _default_subprocess_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def probe(self) -> ProviderState:
        args = [
            self._vibe_binary,
            "-p",
            self._prompt,
            "--output",
            "json",
            "--trust",
            "--auto-approve",
            "--max-turns",
            DEFAULT_MAX_TURNS,
        ]
        exit_code, stdout, stderr = await self._run_subprocess(args, self._timeout_seconds)
        observed_at = self._clock()

        if exit_code == 0:
            availability = ProviderAvailability(available=True, observed_at=observed_at)
        else:
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            reason = _classify_failure_reason(stdout_text, stderr_text)
            availability = ProviderAvailability(available=False, observed_at=observed_at, reason=reason)

        return ProviderState(
            provider=PROVIDER_NAME,
            availability=availability,
            observed_at=observed_at,
            quota_windows=(),
        )
