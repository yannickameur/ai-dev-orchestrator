"""Offline Ollama probes and provider composition."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

import pytest

from orchestrator.project_runtime import resolve_provider_adapters
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason
from orchestrator.providers.ollama_adapter import OllamaAdapter
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.worker_selector import ExecutionProfile, QualityTier, Worker, WorkerSelectionPolicy, WorkerSelectionRequest, WorkerSelector

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


def test_default_http_probe_returns_at_deadline_during_slow_response() -> None:
    release = threading.Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models":[')
            self.wfile.flush()
            release.wait(2)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = OllamaAdapter(
            base_url=f"http://127.0.0.1:{server.server_port}",
            timeout_seconds=0.05,
            required_models=frozenset({"model:latest"}),
            clock=lambda: NOW,
        )
        started = monotonic()
        state = asyncio.run(adapter.probe())
        assert monotonic() - started < 0.5
        assert state.availability.available is False
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_registry_models_are_passed_to_adapter() -> None:
    workers = (_worker("qwen2.5-coder:7b"),)
    adapter = resolve_provider_adapters({"ollama"}, workers=workers)["ollama"]
    assert isinstance(adapter, OllamaAdapter)
    assert adapter._required_models == frozenset({"qwen2.5-coder:7b"})


def test_probe_uses_codex_oss_endpoint_host(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OSS_BASE_URL", "http://ollama.example:11434/v1/")
    adapter = resolve_provider_adapters({"ollama"}, workers=(_worker("qwen2.5-coder:7b"),))["ollama"]
    assert adapter._url == "http://ollama.example:11434/api/tags"


def test_pulled_but_unloaded_model_is_available() -> None:
    async def get(url: str, timeout: float):
        return 200, json.dumps({"models": [{"name": "qwen2.5-coder:7b"}]}).encode()

    state = asyncio.run(OllamaAdapter(required_models=frozenset({"qwen2.5-coder:7b"}), http_get=get).probe())
    assert state.availability.available is True


@pytest.mark.parametrize("required,listed", [
    ("qwen2.5-coder", "qwen2.5-coder:latest"),
    ("qwen2.5-coder:latest", "qwen2.5-coder"),
])
def test_bare_model_and_latest_tag_are_equivalent(required: str, listed: str) -> None:
    async def get(url: str, timeout: float):
        return 200, json.dumps({"models": [{"name": listed}]}).encode()

    state = asyncio.run(OllamaAdapter(required_models=frozenset({required}), http_get=get).probe())
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


def _cloud_adapter(provider: str) -> ProviderAdapter:
    class CloudAdapter(ProviderAdapter):
        async def probe(self) -> ProviderState:
            return ProviderState(
                provider=provider,
                availability=ProviderAvailability(available=True, observed_at=NOW),
                observed_at=NOW,
            )

    return CloudAdapter()


def _selection_workers(*, second_local: bool = True) -> list[Worker]:
    workers = [
        Worker.with_single_profile(
            worker_id="qwen_developer", display_name="Qwen Developer", provider="ollama", backend="codex",
            model="qwen3.5:9b", capabilities={"development"}, priority=10,
        ),
    ]
    if second_local:
        workers.append(Worker.with_single_profile(
            worker_id="qwen_reviewer", display_name="Qwen Reviewer", provider="ollama", backend="codex",
            model="qwen3.5:9b", capabilities={"development"}, priority=9,
        ))
    workers.extend([
        Worker.with_single_profile(
            worker_id="cloud_anthropic", display_name="Cloud A", provider="anthropic", backend="claude_code",
            model="sonnet", capabilities={"development"}, priority=100,
        ),
        Worker.with_single_profile(
            worker_id="cloud_openai", display_name="Cloud B", provider="openai", backend="codex",
            model="gpt", capabilities={"development"}, priority=90,
        ),
    ])
    return workers


def _selection_manager(get) -> QuotaManager:
    return QuotaManager(
        {
            "ollama": OllamaAdapter(required_models=frozenset({"qwen3.5:9b"}), http_get=get, clock=lambda: NOW),
            "anthropic": _cloud_adapter("anthropic"),
            "openai": _cloud_adapter("openai"),
        },
        QuotaPolicy(state_ttl=timedelta(minutes=2)), clock=lambda: NOW,
    )


def test_local_preference_selects_distinct_qwen_workers_with_cloud_available() -> None:
    async def get(url: str, timeout: float):
        return 200, b'{"models":[{"name":"qwen3.5:9b"}]}'

    selector = WorkerSelector(
        _selection_workers(), _selection_manager(get),
        WorkerSelectionPolicy(preferred_provider="ollama"),
    )

    async def scenario() -> None:
        request = WorkerSelectionRequest(required_capabilities=frozenset({"development"}))
        dev_a = await selector.select(request)
        dev_b = await selector.select(WorkerSelectionRequest(
            required_capabilities=request.required_capabilities, author_worker_id=dev_a.worker_id,
        ))
        assert (dev_a.worker_id, dev_b.worker_id) == ("qwen_developer", "qwen_reviewer")
        assert dev_a is not dev_b

    asyncio.run(scenario())


@pytest.mark.parametrize("local_state", ["server_down", "model_missing"])
def test_local_preference_falls_back_to_cloud_when_local_unavailable(local_state: str) -> None:
    async def get(url: str, timeout: float):
        if local_state == "server_down":
            raise OSError("connection refused")
        return 200, b'{"models":[{"name":"other:latest"}]}'

    selector = WorkerSelector(
        _selection_workers(), _selection_manager(get),
        WorkerSelectionPolicy(preferred_provider="ollama"),
    )

    async def scenario() -> None:
        dev_a = await selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"development"})))
        dev_b = await selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), author_worker_id=dev_a.worker_id,
        ))
        assert (dev_a.worker_id, dev_b.worker_id) == ("cloud_anthropic", "cloud_openai")

    asyncio.run(scenario())


def test_local_preference_with_one_qwen_uses_cloud_reviewer() -> None:
    async def get(url: str, timeout: float):
        return 200, b'{"models":[{"name":"qwen3.5:9b"}]}'

    selector = WorkerSelector(
        _selection_workers(second_local=False), _selection_manager(get),
        WorkerSelectionPolicy(preferred_provider="ollama"),
    )

    async def scenario() -> None:
        dev_a = await selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"development"})))
        dev_b = await selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), author_worker_id=dev_a.worker_id,
        ))
        assert (dev_a.worker_id, dev_b.worker_id) == ("qwen_developer", "cloud_anthropic")

    asyncio.run(scenario())


def test_hard_cross_provider_review_rule_overrides_local_preference() -> None:
    async def get(url: str, timeout: float):
        return 200, b'{"models":[{"name":"qwen3.5:9b"}]}'

    selector = WorkerSelector(
        _selection_workers(), _selection_manager(get),
        WorkerSelectionPolicy(preferred_provider="ollama", require_distinct_provider_for_review=True),
    )

    async def scenario() -> None:
        dev_a = await selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"development"})))
        dev_b = await selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), author_worker_id=dev_a.worker_id,
        ))
        assert (dev_a.worker_id, dev_b.worker_id) == ("qwen_developer", "cloud_anthropic")

    asyncio.run(scenario())


def test_without_opt_in_cloud_and_cross_provider_preference_remain() -> None:
    async def get(url: str, timeout: float):
        return 200, b'{"models":[{"name":"qwen3.5:9b"}]}'

    selector = WorkerSelector(_selection_workers(), _selection_manager(get))

    async def scenario() -> None:
        dev_a = await selector.select(WorkerSelectionRequest(required_capabilities=frozenset({"development"})))
        dev_b = await selector.select(WorkerSelectionRequest(
            required_capabilities=frozenset({"development"}), author_worker_id=dev_a.worker_id,
        ))
        assert (dev_a.worker_id, dev_b.worker_id) == ("cloud_anthropic", "cloud_openai")

    asyncio.run(scenario())
