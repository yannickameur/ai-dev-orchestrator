"""Offline Ollama probes and provider composition."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.project_runtime import resolve_provider_adapters
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason
from orchestrator.providers.ollama_adapter import OllamaAdapter
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.worker_selector import ExecutionProfile, QualityTier, Worker, WorkerSelectionRequest, WorkerSelector

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _worker(model: str) -> Worker:
    return Worker(
        worker_id="local", display_name="Local", provider="ollama", backend="ollama",
        capabilities=frozenset({"development"}),
        profiles=(ExecutionProfile(profile_id="standard", quality_tier=QualityTier.STANDARD, model=model),),
    )


@pytest.mark.parametrize("body,expected", [
    (b'{"models":[{"name":"qwen2.5-coder:7b"}]}', True),
    (b'{"models":[{"name":"other:latest"}]}', False),
    (b'{"models":[]}', False),
    (b'{}', False),
    (b'{"models":"wrong"}', False),
    (b'{"models":[{}]}', False),
    (b'not json', False),
])
def test_tags_probe(body: bytes, expected: bool) -> None:
    calls = []

    async def get(url: str, timeout: float):
        calls.append((url, timeout))
        return 200, body

    adapter = OllamaAdapter(required_models=frozenset({"qwen2.5-coder:7b"}), http_get=get, clock=lambda: NOW)
    state = asyncio.run(adapter.probe())
    assert state.provider == "ollama"
    assert state.observed_at == NOW
    assert state.availability.available is expected
    assert state.availability.reason == (None if expected else UnavailabilityReason.PROVIDER_ERROR)
    assert state.quota_windows == ()
    assert calls == [("http://localhost:11434/api/tags", 10.0)]


@pytest.mark.parametrize("failure", ["timeout", "connection", "http"])
def test_failed_probe_is_unavailable(failure: str) -> None:
    async def get(url: str, timeout: float):
        if failure == "timeout":
            await asyncio.sleep(0.02)
        if failure == "connection":
            raise OSError("connection refused")
        return 503, b""

    adapter = OllamaAdapter(timeout_seconds=0.001, http_get=get, clock=lambda: NOW)
    state = asyncio.run(adapter.probe())
    assert state.availability.available is False
    assert state.quota_windows == ()


def test_registry_models_are_passed_to_adapter() -> None:
    workers = (_worker("qwen2.5-coder:7b"),)
    adapter = resolve_provider_adapters({"ollama"}, workers=workers)["ollama"]
    assert isinstance(adapter, OllamaAdapter)
    assert adapter._required_models == frozenset({"qwen2.5-coder:7b"})


def test_pulled_but_unloaded_model_is_available() -> None:
    async def get(url: str, timeout: float):
        return 200, json.dumps({"models": [{"name": "qwen2.5-coder:7b"}]}).encode()

    state = asyncio.run(OllamaAdapter(required_models=frozenset({"qwen2.5-coder:7b"}), http_get=get).probe())
    assert state.availability.available is True


def test_failed_local_probe_does_not_block_cloud_selection() -> None:
    async def get(url: str, timeout: float):
        raise OSError("connection refused")

    class CloudAdapter(ProviderAdapter):
        async def probe(self) -> ProviderState:
            return ProviderState(
                provider="openai",
                availability=ProviderAvailability(available=True, observed_at=NOW),
                observed_at=NOW,
            )

    local = _worker("qwen2.5-coder:7b")
    cloud = Worker.with_single_profile(
        worker_id="cloud", display_name="Cloud", provider="openai", backend="codex",
        model="gpt", capabilities=frozenset({"development"}),
    )
    manager = QuotaManager(
        {"ollama": OllamaAdapter(http_get=get, clock=lambda: NOW), "openai": CloudAdapter()},
        QuotaPolicy(state_ttl=timedelta(minutes=2)), clock=lambda: NOW,
    )
    selected = asyncio.run(WorkerSelector([local, cloud], manager).select(
        WorkerSelectionRequest(required_capabilities=frozenset({"development"}))
    ))
    assert selected.worker_id == "cloud"


def test_failed_refresh_replaces_previous_available_observation() -> None:
    responses = iter((b'{"models":[{"name":"qwen2.5-coder:7b"}]}', b'broken'))

    async def get(url: str, timeout: float):
        return 200, next(responses)

    manager = QuotaManager(
        {"ollama": OllamaAdapter(
            required_models=frozenset({"qwen2.5-coder:7b"}), http_get=get, clock=lambda: NOW
        )},
        QuotaPolicy(state_ttl=timedelta(minutes=2)), clock=lambda: NOW,
    )

    async def scenario() -> None:
        assert (await manager.get("ollama")).availability.available is True
        assert (await manager.refresh("ollama")).availability.available is False
        assert (await manager.get("ollama")).availability.available is False

    asyncio.run(scenario())
