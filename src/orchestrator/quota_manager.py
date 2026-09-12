"""QuotaManager — freshness/cache policy layer around ProviderAdapter.

This module answers exactly one question: "what is the currently known,
sufficiently fresh state of a given provider?" It never decides which
provider or worker to use, never falls back between providers, and never
consumes a reset credit — those are the responsibilities of later
components (WorkerSelector, RalphExecutionEngine, and an explicit
human/policy decision, respectively — see ROADMAP.md, Phase 1, steps 6-7).

Responsibilities:

- Hold the last observed :class:`~orchestrator.providers.contracts.ProviderState`
  per provider name, keyed exactly as supplied in the ``adapters`` mapping.
- Decide, using an injectable clock and a configurable TTL
  (:class:`QuotaPolicy`), whether a cached observation is still fresh
  enough to serve without a new probe.
- Call the corresponding :class:`~orchestrator.providers.adapter.ProviderAdapter`
  only when needed (missing/expired cache, or an explicit ``refresh()``).
- Never talk to Claude, Codex, or any provider CLI/protocol directly — it
  only ever calls ``ProviderAdapter.probe()``.

Failure policy — STALE != USABLE FOR ROUTING:

- ``refresh(provider)`` always performs a real probe and never silently
  substitutes a cached value: on adapter failure it raises
  ``ProviderProbeError`` and leaves any previously cached state untouched.
- ``get(provider)`` probes when the cache is missing or expired. If that
  probe fails, ``get()`` raises ``ProviderProbeError`` too — it never falls
  back to returning a previous, now-stale state, even one whose
  ``availability`` was ``AVAILABLE``. A caller (eventually a
  WorkerSelector) must never receive an old observation silently
  presented as usable for a routing decision just because a fresher probe
  failed. A previous state, if any, is not necessarily discarded from the
  internal cache by this failure, but nothing here exposes it as the
  result of ``get()`` — a possible future explicit diagnostic accessor
  (e.g. ``last_known()``) is a separate, deliberate decision this module
  does not make.

Concurrency: concurrent calls that need to probe the same provider (via
``get()`` and/or ``refresh()``) share a single in-flight probe rather than
each launching their own — a small per-provider single-flight guard, not a
scheduler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderState

Clock = Callable[[], datetime]


class QuotaManagerError(Exception):
    """Base for QuotaManager domain errors."""


class UnknownProviderError(QuotaManagerError):
    """Raised when a provider name is not registered with this manager."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"unknown provider: {provider!r}")
        self.provider = provider


class ProviderProbeError(QuotaManagerError):
    """Raised when a provider's adapter fails to produce a ProviderState.

    Wraps the adapter's original exception (available as ``__cause__``)
    without hiding it: callers that need the underlying detail can still
    inspect it.
    """

    def __init__(self, provider: str, cause: BaseException) -> None:
        super().__init__(f"probe failed for provider {provider!r}: {cause}")
        self.provider = provider


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Freshness policy for cached ProviderState observations.

    ``state_ttl`` is the maximum age (relative to the injectable clock) at
    which a cached :class:`ProviderState` is still served without a new
    probe.
    """

    state_ttl: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.state_ttl, timedelta):
            raise TypeError(
                f"QuotaPolicy.state_ttl must be a timedelta, got {type(self.state_ttl)!r}"
            )
        if self.state_ttl <= timedelta(0):
            raise ValueError(
                f"QuotaPolicy.state_ttl must be a positive timedelta, got {self.state_ttl!r}"
            )


class QuotaManager:
    """Freshness/cache policy layer around a set of ProviderAdapters."""

    def __init__(
        self,
        adapters: Mapping[str, ProviderAdapter],
        policy: QuotaPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._states: dict[str, ProviderState] = {}
        self._inflight: dict[str, asyncio.Task[ProviderState]] = {}

    async def get(self, provider: str) -> ProviderState:
        """Return a sufficiently fresh ProviderState, probing only if needed.

        Never returns a stale state: if the cache is missing or expired and
        the resulting probe fails, this raises ``ProviderProbeError`` rather
        than falling back to an old observation (STALE != USABLE FOR
        ROUTING — see module docstring).
        """
        adapter = self._require_adapter(provider)
        now = self._now()
        cached = self._states.get(provider)
        if cached is not None and self._is_fresh(cached, now=now):
            return cached
        return await self._probe_and_cache(provider, adapter)

    async def refresh(self, provider: str) -> ProviderState:
        """Force a real probe for ``provider``, regardless of cache freshness."""
        adapter = self._require_adapter(provider)
        return await self._probe_and_cache(provider, adapter)

    async def refresh_all(self) -> dict[str, ProviderState]:
        """Force a real probe for every registered provider, concurrently.

        Simple by design: this is a thin concurrent ``refresh()`` fan-out,
        not a scheduler. If any provider's probe fails, the resulting
        ``ProviderProbeError`` propagates as-is (no partial-result
        swallowing) — callers that need partial-success semantics can call
        ``refresh()`` per provider themselves.
        """
        names = list(self._adapters)
        results = await asyncio.gather(*(self.refresh(name) for name in names))
        return dict(zip(names, results))

    def _require_adapter(self, provider: str) -> ProviderAdapter:
        try:
            return self._adapters[provider]
        except KeyError:
            raise UnknownProviderError(provider) from None

    def _is_fresh(self, state: ProviderState, *, now: datetime) -> bool:
        return now - state.observed_at <= self._policy.state_ttl

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"QuotaManager clock() must return a timezone-aware datetime, got {value!r}"
            )
        return value

    async def _probe_and_cache(self, provider: str, adapter: ProviderAdapter) -> ProviderState:
        task = self._inflight.get(provider)
        if task is None:
            task = asyncio.ensure_future(self._do_probe(provider, adapter))
            self._inflight[provider] = task
            task.add_done_callback(lambda _t, p=provider: self._inflight.pop(p, None))
        return await task

    async def _do_probe(self, provider: str, adapter: ProviderAdapter) -> ProviderState:
        try:
            state = await adapter.probe()
        except Exception as exc:
            raise ProviderProbeError(provider, exc) from exc
        self._states[provider] = state
        return state
