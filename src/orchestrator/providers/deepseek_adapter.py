"""DeepSeek — Anthropic-compatible provider, reached through the existing
``claude`` binary.

REUSE FIRST: DeepSeek's own documentation
(https://api-docs.deepseek.com/guides/anthropic_api/,
https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
describes DeepSeek access as the standard Claude Code CLI redirected, via
``ANTHROPIC_BASE_URL``/``ANTHROPIC_API_KEY``, at
``https://api.deepseek.com/anthropic`` — not a distinct CLI or wire
protocol. So this module never writes a second, independent adapter: it is
a thin factory that builds a :class:`~orchestrator.providers.claude_code_adapter.ClaudeCodeAdapter`
configured for that redirect. See that module's own docstring
("Reuse beyond real Anthropic") for the mechanism.

DeepSeek direct is billed per token (unlike Claude Code/Codex/Vibe, whose
CLIs are already authenticated outside this project against a subscription
— no secret ever handled here). This module therefore never defaults
``DEEPSEEK_API_KEY`` and never stores one: it is read from the process
environment only at the moment this factory is actually called, which
``orchestrator.project_runtime._resolve_provider_adapters`` only does for a
provider an *enabled* worker actually requires. A missing key raises
``DeepSeekConfigError`` (never a bare ``KeyError``/``None`` silently used as
a key) and never affects any other provider.
"""

from __future__ import annotations

import os
from typing import Mapping

from orchestrator.providers.adapter import ProviderConfigError
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter

PROVIDER_NAME = "deepseek"

ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"

# https://api-docs.deepseek.com/guides/anthropic_api/ — verify against
# DeepSeek's own docs before relying on this, never assumed stable forever.
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
# "DeepSeek Flash" == DeepSeek V4.1 Flash at time of writing (2026-09) —
# DeepSeek's own docs are the source of truth for this mapping, not this
# comment; see https://api-docs.deepseek.com/.
DEFAULT_MODEL = "deepseek-flash"


class DeepSeekConfigError(ProviderConfigError):
    """Raised when DEEPSEEK_API_KEY is missing/blank at construction time."""


def build_deepseek_adapter(
    *, env: Mapping[str, str] | None = None, **claude_code_adapter_kwargs: object
) -> ClaudeCodeAdapter:
    """Builds a :class:`ClaudeCodeAdapter` redirected at DeepSeek's
    Anthropic-compatible endpoint.

    ``env`` defaults to the real process environment (``os.environ``); a
    test passes an explicit mapping instead of ever relying on a real key
    being present on the machine running the suite. ``DEEPSEEK_BASE_URL``/
    ``DEEPSEEK_MODEL`` are optional overrides of the documented defaults —
    ``DEEPSEEK_API_KEY`` has no default and is required.
    """
    source = env if env is not None else os.environ
    api_key = source.get(ENV_API_KEY)
    if not api_key or not api_key.strip():
        raise DeepSeekConfigError(
            f"{ENV_API_KEY} is not set. DeepSeek is an optional, billed "
            "provider — it is never required for Claude/Codex/Mistral/Kimi "
            f"to keep working. Set {ENV_API_KEY} only if you actually want "
            "to use DeepSeek."
        )
    base_url = source.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    model = source.get(ENV_MODEL) or DEFAULT_MODEL
    return ClaudeCodeAdapter(
        provider_name=PROVIDER_NAME,
        model=model,
        extra_env={"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": api_key},
        **claude_code_adapter_kwargs,
    )
