"""Read-only availability probe for a local Ollama service."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Awaitable, Callable

from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    UnavailabilityReason,
)

HttpGet = Callable[[str, float], Awaitable[tuple[int, bytes]]]
Clock = Callable[[], datetime]


def _sync_get(url: str, timeout: float) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read(1_048_577)


async def _default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    # asyncio.to_thread leaves a non-daemon executor thread behind on cancellation;
    # asyncio.run then waits for that thread, defeating the probe deadline.
    result: concurrent.futures.Future[tuple[int, bytes]] = concurrent.futures.Future()

    def read() -> None:
        try:
            value = _sync_get(url, timeout)
        except Exception as exc:
            try:
                result.set_exception(exc)
            except concurrent.futures.InvalidStateError:
                pass  # The timed-out probe cancelled its future.
        else:
            try:
                result.set_result(value)
            except concurrent.futures.InvalidStateError:
                pass  # The timed-out probe cancelled its future.

    threading.Thread(target=read, daemon=True).start()
    return await asyncio.wrap_future(result)


def _models_present(body: bytes, required: frozenset[str]) -> bool:
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return False
    names = set()
    for model in payload["models"]:
        if not isinstance(model, dict):
            return False
        name = model.get("name") or model.get("model")
        if not isinstance(name, str) or not name.strip():
            return False
        names.add(name)
    # A bare model name means its default tag. Other tags are distinct models.
    def normalized(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    available = {normalized(candidate) for candidate in names}
    return all(normalized(name) in available for name in required)


class OllamaAdapter(ProviderAdapter):
    """Probe /api/tags. A pulled model need not already be loaded in RAM."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 10.0,
        required_models: frozenset[str] = frozenset(),
        http_get: HttpGet | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._url = f"{base_url.rstrip('/')}/api/tags"
        self._timeout = timeout_seconds
        self._required_models = required_models
        self._http_get = http_get or _default_http_get
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def probe(self) -> ProviderState:
        try:
            status, body = await asyncio.wait_for(
                self._http_get(self._url, self._timeout), timeout=self._timeout
            )
            available = status == 200 and _models_present(body, self._required_models)
        except (asyncio.TimeoutError, OSError, ValueError, TypeError, UnicodeError):
            available = False
        observed_at = self._clock()
        return ProviderState(
            provider="ollama",
            availability=ProviderAvailability(
                available=available,
                observed_at=observed_at,
                reason=None if available else UnavailabilityReason.PROVIDER_ERROR,
            ),
            observed_at=observed_at,
            quota_windows=(),
        )
