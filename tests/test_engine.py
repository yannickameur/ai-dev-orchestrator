"""Tests for OrchestratorEngine: the public façade P13 introduces for a
frontend (AIDO Code) to drive a project without touching ProjectRuntime/
MVPManager/WorkerSelector/QuotaManager/ProviderAdapters/
GitGovernanceService/InternalQAEngine/Store directly.

Offline only: real Git repos/worker registries under pytest's ``tmp_path``,
fake provider adapters (never real Claude/Codex/Vibe/DeepSeek/Kimi probes),
a scripted fake Ralph subprocess runner (never real Ralph); same fixtures
and conventions as ``tests/test_cli.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from orchestrator.engine import (
    EngineConfigError,
    EngineError,
    OrchestratorEngine,
    ProjectSnapshot,
    ProjectStatusSnapshot,
)
from orchestrator.project_config import ProjectConfig
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason

UTC_T0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


class _FakeAdapter(ProviderAdapter):
    def __init__(self, *, available: bool = True) -> None:
        self.probe_calls = 0
        self._available = available

    async def probe(self) -> ProviderState:
        self.probe_calls += 1
        return ProviderState(
            provider="fake",
            availability=ProviderAvailability(
                available=self._available, observed_at=UTC_T0,
                reason=None if self._available else UnavailabilityReason.QUOTA_EXHAUSTED,
            ),
            observed_at=UTC_T0,
        )


class _NeverCalledAdapter(ProviderAdapter):
    async def probe(self) -> ProviderState:
        raise AssertionError("provider probe must never be called by this command")


class _ScriptedRalphRunner:
    """Same shape as ``tests/test_cli.py``'s own: genuinely mutates the
    workspace and writes real Ralph event files, never a real Ralph/Claude/
    Codex/Vibe subprocess."""

    def __init__(self, steps: list[dict]) -> None:
        self._steps = list(steps)
        self.calls: list[tuple] = []

    async def __call__(self, args: list[str], cwd: Path, timeout: float) -> tuple[int, bytes, bytes]:
        self.calls.append((args, cwd, timeout))
        assert self._steps, "fake Ralph runner called more times than scripted"
        step = self._steps.pop(0)
        mutate = step.get("mutate")
        if mutate is not None:
            mutate(Path(cwd))

        ralph_dir = Path(cwd) / ".ralph"
        ralph_dir.mkdir(parents=True, exist_ok=True)
        (ralph_dir / "current-loop-id").write_text(step.get("loop_id", "engine-test-loop"))
        events_filename = f"events-{len(self.calls)}.jsonl"
        (ralph_dir / "current-events").write_text(f".ralph/{events_filename}")
        line = json.dumps({"topic": step["topic"], "ts": UTC_T0.isoformat(), "payload": step.get("payload")})
        (ralph_dir / events_filename).write_text(line + "\n")
        return step.get("exit_code", 0), step.get("stdout", b""), step.get("stderr", b"")


def _commit_action(repo_cwd_relative_file: str, content: str, message: str):
    def _mutate(cwd: Path) -> None:
        (cwd / repo_cwd_relative_file).write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
        subprocess.run(
            ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E", "commit", "-q", "-m", message],
            cwd=str(cwd), check=True,
        )
    return _mutate


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E", "add", "-A"],
        cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E", "commit", "-q", "-m", "init"],
        cwd=str(path), check=True,
    )
    return path


REGISTRY_TWO_WORKERS = dedent(
    """
    workers:
      - worker_id: alice
        display_name: Alice
        provider: anthropic
        backend: claude_code
        capabilities: [development]
        profiles:
          standard: {quality_tier: STANDARD, model: sonnet}
      - worker_id: bob
        display_name: Bob
        provider: anthropic
        backend: claude_code
        capabilities: [development]
        profiles:
          standard: {quality_tier: STANDARD, model: sonnet}
    """
)


def _write_config(
    tmp_path: Path, *, state_dir: str = "state", permission_mode: str = "standard",
    registry: str = REGISTRY_TWO_WORKERS,
) -> Path:
    _init_git_repo(tmp_path / "proj")
    (tmp_path / "workers.yaml").write_text(registry)
    config_path = tmp_path / "aido.yaml"
    config_path.write_text(
        dedent(
            f"""
            schema_version: 1
            project:
              id: demo
              name: Demo
              workspace: proj
              state_dir: {state_dir}
            workers:
              registry: workers.yaml
            execution:
              permission_mode: {permission_mode}
            git:
              base_branch: main
            mvp:
              id: mvp-1
              objective: Ship it
            work_items:
              - id: wi-1
                title: Do the thing
                required_capabilities: [development]
            qa:
              - id: qa-1
                kind: unit_test
                argv: ["{sys.executable}", "-c", "pass"]
            """
        )
    )
    return config_path


class TestOpen:
    def test_open_loads_valid_config(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path))
        assert isinstance(engine, OrchestratorEngine)

    def test_open_raises_engine_config_error_for_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(EngineConfigError):
            OrchestratorEngine.open(str(tmp_path / "does-not-exist.yaml"))

    def test_open_raises_engine_config_error_for_bad_worker_registry(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, registry="workers:\n  - worker_id: alice\n")
        with pytest.raises(EngineConfigError):
            OrchestratorEngine.open(str(config_path))

    def test_open_never_touches_provider_or_state_dir(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        assert not config.project.state_dir.exists()


class TestValidate:
    def test_validate_returns_project_snapshot(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        snapshot = engine.validate()
        assert isinstance(snapshot, ProjectSnapshot)
        assert snapshot.project_id == "demo"
        assert snapshot.mvp_id == "mvp-1"
        assert snapshot.work_item_count == 1
        assert snapshot.qa_command_count == 1
        assert snapshot.enabled_worker_count == 2
        assert snapshot.providers == ("anthropic",)
        assert snapshot.permission_mode == "standard"
        assert snapshot.base_branch == "main"

    def test_validate_never_touches_a_provider(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        engine.validate()  # would raise via _NeverCalledAdapter.probe if ever called


class TestWorkers:
    def test_workers_lists_every_configured_worker(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        workers = engine.workers()
        assert {w.worker_id for w in workers} == {"alice", "bob"}
        alice = next(w for w in workers if w.worker_id == "alice")
        assert alice.enabled is True
        assert alice.provider == "anthropic"
        assert alice.backend == "claude_code"
        assert alice.capabilities == ("development",)
        assert alice.model == "sonnet"

    def test_workers_includes_disabled_workers(self, tmp_path: Path) -> None:
        registry = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                enabled: false
                provider: anthropic
                backend: claude_code
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: sonnet}
            """
        )
        config_path = _write_config(tmp_path, registry=registry)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={})
        workers = engine.workers()
        assert len(workers) == 1
        assert workers[0].enabled is False

    def test_workers_never_touches_a_provider(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        engine.workers()  # would raise via _NeverCalledAdapter.probe if ever called


class TestStatus:
    def test_uninitialized_project_reports_not_initialized(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        snapshot = engine.status()
        assert isinstance(snapshot, ProjectStatusSnapshot)
        assert snapshot.initialized is False
        assert snapshot.mvp is None
        assert snapshot.work_items == ()

    def test_uninitialized_status_does_not_create_state_dir(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        assert not config.project.state_dir.exists()
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        engine.status()
        assert not config.project.state_dir.exists()

    def test_initialized_project_reports_workitem_status(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        snapshot = engine.status()
        assert snapshot.initialized is True
        assert snapshot.mvp.status == "planned"
        assert len(snapshot.work_items) == 1
        assert snapshot.work_items[0].work_item_id == "wi-1"
        assert snapshot.work_items[0].status == "ready"

    def test_status_never_touches_a_provider(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        engine.status()  # would raise via _NeverCalledAdapter.probe if ever called

    def test_status_never_instantiates_a_real_provider_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestrator.providers import claude_code_adapter

        def _boom(self, *a, **k):
            raise AssertionError("no provider adapter may ever be instantiated by status()")

        monkeypatch.setattr(claude_code_adapter.ClaudeCodeAdapter, "__init__", _boom)

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        engine = OrchestratorEngine.open(str(config_path))
        engine.status()  # would raise via the patched __init__ if status ever built a real adapter


class TestProbeWorkers:
    def test_probe_returns_a_snapshot_per_provider_of_enabled_workers(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        adapter = _FakeAdapter(available=True)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": adapter})
        snapshots = engine.probe_workers()
        assert len(snapshots) == 1
        assert snapshots[0].provider == "anthropic"
        assert snapshots[0].available is True
        assert snapshots[0].reason == "available"
        assert adapter.probe_calls == 1

    def test_probe_reports_unavailable_honestly(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=False)}
        )
        snapshots = engine.probe_workers()
        assert snapshots[0].available is False
        assert snapshots[0].reason == "quota_exhausted"

    def test_probe_never_fabricates_availability_on_probe_error(self, tmp_path: Path) -> None:
        class _BoomAdapter(ProviderAdapter):
            async def probe(self):
                raise RuntimeError("simulated network failure")

        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _BoomAdapter()})
        snapshots = engine.probe_workers()
        assert snapshots[0].available is False
        assert snapshots[0].reason.startswith("probe_error:")

    def test_probe_with_no_enabled_workers_returns_empty(self, tmp_path: Path) -> None:
        registry = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                enabled: false
                provider: anthropic
                backend: claude_code
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: sonnet}
            """
        )
        config_path = _write_config(tmp_path, registry=registry)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={})
        assert engine.probe_workers() == ()

    def test_probe_never_launches_ralph(self, tmp_path: Path) -> None:
        class _NeverCalledRunner:
            def __call__(self, *a, **k):
                raise AssertionError("probe_workers must never launch a Ralph subprocess")

        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(
            str(config_path),
            provider_adapters={"anthropic": _FakeAdapter()},
            subprocess_runner=_NeverCalledRunner(),
        )
        engine.probe_workers()  # would raise if it ever touched the Ralph subprocess seam


class TestRun:
    def test_run_drives_full_workitem_flow_to_completed(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        engine = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        result = engine.run()
        assert result.all_terminal is True
        assert result.reached_max_cycles is False
        assert len(result.work_items) == 1
        assert result.work_items[0].status == "completed"
        assert len(runner.calls) == 2
        assert [e.kind for e in result.events] == ["work_item.completed"]

    def test_run_is_also_resume_and_never_reruns_a_completed_workitem(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        engine = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        engine.run()

        second_runner = _ScriptedRalphRunner([])
        engine2 = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=second_runner,
        )
        result = engine2.run()
        # cycles_run == 1 here (not 0): matches `aido run`'s own established
        # semantics. One cycle is attempted, finds nothing eligible, and
        # stops; it is not a count of WorkItems actually executed.
        assert result.cycles_run == 1
        assert result.all_terminal is True
        assert len(second_runner.calls) == 0

    def test_run_reports_waiting_when_no_provider_available(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        engine = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=False)},
        )
        with pytest.raises(Exception):
            # No eligible worker and no diagnosable reset -> NoEligibleWorkerError
            # propagates, exactly like the underlying MVPManager/CLI behavior.
            engine.run()
