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
