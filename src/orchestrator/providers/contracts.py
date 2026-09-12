"""Normalized, provider-agnostic domain contracts for provider state.

These types describe what a :class:`~orchestrator.providers.adapter.ProviderAdapter`
observes about a provider (Claude Code, Codex, Ollama, or any future CLI/API
provider) at a single point in time. They carry no behavior beyond input
validation: no execution, no caching/TTL policy, no consumption of reset
credits. Those responsibilities belong to later components (QuotaManager,
RalphExecutionEngine, explicit policy decisions) — see ROADMAP.md, Phase 1.

Design invariants (see ROADMAP.md / docs/SPIKE_RALPH.md for the rationale):

- A provider may expose several simultaneous quota windows (e.g. a 5h and a
  7d window) — there is no single ``reset_at`` at the ``ProviderState``
  level, and no assumption that exactly two windows exist.
- Every observation is timestamped (``observed_at``, timezone-aware) so a
  caller can judge freshness; this module does not implement freshness
  policy itself.
- Unknown quota utilization is represented as ``None``, never coerced to
  ``0.0`` — a real zero usage and an unknown usage are different facts.
- ``ProviderAvailability`` only describes the provider's own reachability/
  quota state. It must never be confused with task/execution-level states
  such as ``WAITING_RESET``, ``RECOVERY_REQUIRED`` or ``INTERRUPTED``,
  which belong to the orchestrator's execution model, not to the provider.
- A reset credit is purely descriptive. Nothing in this module can consume
  one: ``auto_consume`` is a policy flag that this module pins to ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware datetime, got a naive one: {moment!r}"
        )


def _require_non_empty(value: str, *, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class UnavailabilityReason(str, Enum):
    """Normalized, provider-agnostic reason a provider is not available.

    Deliberately generic: it must be able to represent Claude, Codex,
    Ollama, or any future provider without provider-specific members.
    """

    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_ERROR = "auth_error"
    PROVIDER_ERROR = "provider_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """Whether the provider itself is currently usable.

    This is strictly about the provider's own reachability/quota state —
    never about a task's or execution's status. ``WAITING_RESET``,
    ``RECOVERY_REQUIRED`` and ``INTERRUPTED`` are orchestrator/execution
    concepts and have no place here.
    """

    available: bool
    observed_at: datetime
    reason: UnavailabilityReason | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, field_name="ProviderAvailability.observed_at")
        if self.available and self.reason is not None:
            raise ValueError(
                "ProviderAvailability.reason must be None when available=True"
            )
        if not self.available and self.reason is None:
            raise ValueError(
                "ProviderAvailability.reason is required when available=False "
                f"(use {UnavailabilityReason.UNKNOWN!r} if the cause is not known)"
            )


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """A single observed quota window for a provider.

    A provider may expose several of these at once (e.g. Claude's
    ``five_hour``/``seven_day``, Codex's ``primary``/``secondary``).
    ``window_type`` is an opaque, provider-supplied label — this contract
    never assumes a fixed count or a fixed set of window identifiers.
    """

    window_type: str
    source: str
    observed_at: datetime
    utilization: float | None = None
    reset_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.window_type, field_name="QuotaWindow.window_type")
        _require_non_empty(self.source, field_name="QuotaWindow.source")
        _require_aware(self.observed_at, field_name="QuotaWindow.observed_at")
        if self.utilization is not None and not (0.0 <= self.utilization <= 1.0):
            raise ValueError(
                "QuotaWindow.utilization must be a fraction in [0.0, 1.0] or None "
                f"(unknown), got {self.utilization!r}"
            )
        if self.reset_at is not None:
            _require_aware(self.reset_at, field_name="QuotaWindow.reset_at")


class ResetCreditStatus(str, Enum):
    """Normalized status of a reset credit, independent of any provider."""

    AVAILABLE = "available"
    CONSUMED = "consumed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResetCredit:
    """A provider-issued credit that could reset a quota window early.

    Purely descriptive: nothing here can consume a credit. Consumption is a
    deliberate, later policy decision made outside this module — this type
    only pins the project policy that it is never automatic.
    """

    title: str
    status: ResetCreditStatus
    available_count: int | None = None
    auto_consume: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.title, field_name="ResetCredit.title")
        if self.available_count is not None and self.available_count < 0:
            raise ValueError(
                "ResetCredit.available_count must be >= 0, got "
                f"{self.available_count!r}"
            )
        if self.auto_consume is not False:
            raise ValueError(
                "ResetCredit.auto_consume must be False: automatic consumption "
                "of a reset credit is a policy decision this contract never "
                "authorizes implicitly"
            )


@dataclass(frozen=True, slots=True)
class ProviderState:
    """A single, timestamped snapshot of a provider's observable state.

    Returned by :meth:`ProviderAdapter.probe`. Never an execution result:
    this type only normalizes what an adapter discovered about the
    provider (availability, quota windows, reset credits), not what a
    worker did on that provider.
    """

    provider: str
    availability: ProviderAvailability
    observed_at: datetime
    quota_windows: tuple[QuotaWindow, ...] = ()
    reset_credits: tuple[ResetCredit, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.provider, field_name="ProviderState.provider")
        _require_aware(self.observed_at, field_name="ProviderState.observed_at")
        if not isinstance(self.availability, ProviderAvailability):
            raise TypeError(
                "ProviderState.availability must be a ProviderAvailability, "
                f"got {type(self.availability)!r}"
            )
        object.__setattr__(
            self, "quota_windows", _as_tuple(self.quota_windows, QuotaWindow)
        )
        object.__setattr__(
            self, "reset_credits", _as_tuple(self.reset_credits, ResetCredit)
        )


def _as_tuple(values: Iterable[object], expected_type: type) -> tuple:
    result = tuple(values)
    for item in result:
        if not isinstance(item, expected_type):
            raise TypeError(f"expected {expected_type.__name__}, got {type(item)!r}")
    return result
