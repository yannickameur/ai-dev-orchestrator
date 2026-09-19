"""Kimi (Kimi Code) — Anthropic-compatible provider, reached through the
existing ``claude`` binary.

REUSE FIRST: Kimi's own documentation
(https://www.kimi.com/code/docs/en/,
https://www.kimi.com/code/docs/en/third-party-tools/claude-code.html)
describes Kimi Code access as the standard Claude Code CLI redirected, via
``ANTHROPIC_BASE_URL``/``ANTHROPIC_API_KEY``, at
``https://api.kimi.ai/coding/`` — not a distinct CLI or wire protocol. So
this module never writes a second, independent adapter: it is a thin
factory that builds a :class:`~orchestrator.providers.claude_code_adapter.ClaudeCodeAdapter`
configured for that redirect. See that module's own docstring
("Reuse beyond real Anthropic") for the mechanism.

Kimi Code is a subscription (quota-based, "Kimi Code" membership tiers), not
plain pay-as-you-go — the explicit reason it was prioritized over a generic
PAYG integration. It still requires its own ``KIMI_API_KEY`` (unlike Claude
Code/Codex/Vibe, whose CLIs are already authenticated outside this project —
no secret ever handled here). This module therefore never defaults
``KIMI_API_KEY`` and never stores one: it is read from the process
environment only at the moment this factory is actually called, which
``orchestrator.project_runtime._resolve_provider_adapters`` only does for a
provider an *enabled* worker actually requires. A missing key raises
``KimiConfigError`` (never a bare ``KeyError``/``None`` silently used as a
key) and never affects any other provider.
"""

from __future__ import annotations

import os
from typing import Mapping

from orchestrator.providers.adapter import ProviderConfigError
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter

PROVIDER_NAME = "kimi"

ENV_API_KEY = "KIMI_API_KEY"
ENV_BASE_URL = "KIMI_BASE_URL"
ENV_MODEL = "KIMI_MODEL"

# International (non-China) Anthropic-compatible endpoint — verify against
# Kimi's own docs before relying on this, never assumed stable forever:
# https://www.kimi.com/code/docs/en/third-party-tools/claude-code.html
DEFAULT_BASE_URL = "https://api.kimi.ai/coding/"
# "kimi-for-coding" == K2.8 Preview at time of writing (2026-09), up to a
# 1M-token context depending on membership tier — Kimi's own docs are the
# source of truth for this mapping, not this comment.
DEFAULT_MODEL = "kimi-for-coding"


class KimiConfigError(ProviderConfigError):
    """Raised when KIMI_API_KEY is missing/blank at construction time."""


def build_kimi_adapter(
    *, env: Mapping[str, str] | None = None, **claude_code_adapter_kwargs: object
) -> ClaudeCodeAdapter:
    """Builds a :class:`ClaudeCodeAdapter` redirected at Kimi Code's
    Anthropic-compatible endpoint.

    ``env`` defaults to the real process environment (``os.environ``); a
    test passes an explicit mapping instead of ever relying on a real key
    being present on the machine running the suite. ``KIMI_BASE_URL``/
    ``KIMI_MODEL`` are optional overrides of the documented defaults —
    ``KIMI_API_KEY`` has no default and is required.
    """
    source = env if env is not None else os.environ
    api_key = source.get(ENV_API_KEY)
    if not api_key or not api_key.strip():
        raise KimiConfigError(
            f"{ENV_API_KEY} is not set. Kimi is an optional, subscription-based "
            "provider — it is never required for Claude/Codex/Mistral/DeepSeek "
            f"to keep working. Set {ENV_API_KEY} only if you actually want to "
            "use Kimi."
        )
    base_url = source.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    model = source.get(ENV_MODEL) or DEFAULT_MODEL
    return ClaudeCodeAdapter(
        provider_name=PROVIDER_NAME,
        model=model,
        extra_env={"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": api_key},
        **claude_code_adapter_kwargs,
    )
