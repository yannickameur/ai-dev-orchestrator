"""Normalized provider-state contracts and the ProviderAdapter interface.

This package holds only the domain contracts (Phase 1 / Slice 0). It is
deliberately independent of any concrete provider (Claude Code, Codex,
Ollama) and of Ralph: no provider-specific adapter and no execution engine
lives here.
"""

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    ResetCredit,
    ResetCreditStatus,
    UnavailabilityReason,
)

__all__ = [
    "ClaudeCodeAdapter",
    "ProviderAdapter",
    "ProviderAvailability",
    "ProviderState",
    "QuotaWindow",
    "ResetCredit",
    "ResetCreditStatus",
    "UnavailabilityReason",
]
