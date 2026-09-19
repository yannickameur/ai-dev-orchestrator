"""The ProviderAdapter interface.

A ``ProviderAdapter`` is a read-only probe of a single provider's current
state. It has exactly one responsibility: discover and normalize that
state into a :class:`~orchestrator.providers.contracts.ProviderState`.

It must never become an execution engine. Running a worker, completing a
prompt, or reviewing code is the responsibility of the (later)
``RalphExecutionEngine`` — this interface intentionally has no
``execute()``, ``run()``, ``complete()``, ``review()`` or ``fallback()``.
"""

from __future__ import annotations

import abc

from orchestrator.providers.contracts import ProviderState


class ProviderConfigError(Exception):
    """Raised when a provider adapter cannot be constructed because required
    configuration (typically an API key) is missing or invalid.

    Distinct from a probe-time failure (e.g. ``ClaudeProbeError``): this is
    raised at adapter-construction time, before any subprocess/network call
    is ever attempted. A provider-specific adapter factory (e.g.
    ``orchestrator.providers.deepseek_adapter.build_deepseek_adapter``)
    raises a subclass of this for its own missing configuration. Only ever
    raised for a provider actually requested by the caller, never for one
    that is simply configured but unused, so a missing key for one
    optional, API-key-based provider must never prevent any other provider
    from working.
    """


class ProviderAdapter(abc.ABC):
    """Read-only probe of a provider's state.

    Concrete adapters (Claude Code, Codex, Ollama, ...) are added in later
    slices. This slice only defines the contract they must satisfy.
    """

    @abc.abstractmethod
    async def probe(self) -> ProviderState:
        """Discover and return the provider's current state.

        Must never raise for an ordinarily unavailable provider (e.g. quota
        exhausted, auth expired): that is represented via
        ``ProviderState.availability``, not via an exception. Exceptions
        are reserved for adapter-level failures that prevent producing a
        ``ProviderState`` at all.
        """
        raise NotImplementedError
