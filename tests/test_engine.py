"""Tests for OrchestratorEngine: the public façade P13 introduces for a
frontend (AIDO Code) to drive a project without touching ProjectRuntime/
MVPManager/WorkerSelector/QuotaManager/ProviderAdapters/
GitGovernanceService/InternalQAEngine/Store directly.

Offline only: real Git repos/worker registries under pytest's ``tmp_path``,
fake provider adapters (never real Claude/Codex/Vibe/Gravity probes),
a scripted fake Ralph subprocess runner (never real Ralph); same fixtures
and conventions as ``tests/test_cli.py``.
"""

from __future__ import annotations

import asyncio
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
    EngineEvent,
    ExecutionTimeSnapshot,
    OrchestratorEngine,
    ProjectSnapshot,
    ProjectStatusSnapshot,
)
from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.project_config import (
    ExecutionConfig,
    GitConfig,
    MVPConfig,
    NoWorkerRegistryConfiguredError,
    ProjectConfig,
    ProjectIdentity,
    WorkItemConfig,
)
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason
from orchestrator.validation import ValidationCommand, ValidationKind
from orchestrator.worker_registry import WorkerRegistry
from orchestrator.worker_selector import Worker

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
    registry: str = REGISTRY_TWO_WORKERS, project_id: str = "demo",
    include_workers_section: bool = True,
) -> Path:
    _init_git_repo(tmp_path / "proj")
    if include_workers_section:
        (tmp_path / "workers.yaml").write_text(registry)
        body = dedent(
            f"""
            schema_version: 1
            project:
              id: {project_id}
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
    else:
        # No `workers:` section at all — the modern engine boundary: the
        # caller injects a WorkerRegistry directly, and this ProjectConfig
        # never needs a workers.registry path to be valid.
        body = dedent(
            f"""
            schema_version: 1
            project:
              id: {project_id}
              name: Demo
              workspace: proj
              state_dir: {state_dir}
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
    config_path = tmp_path / "aido.yaml"
    config_path.write_text(body)
    return config_path


def _worker_registry(*worker_ids: str) -> WorkerRegistry:
    """A ``WorkerRegistry`` built directly in Python — never read from a
    YAML file — exactly the shape an embedding application (AIDO Code)
    constructs/owns itself and injects into ``OrchestratorEngine``."""
    return WorkerRegistry([
        Worker.with_single_profile(
            worker_id=worker_id, display_name=worker_id.title(), provider="anthropic",
            backend="claude_code", model="sonnet", capabilities=["development"],
        )
        for worker_id in worker_ids
    ])


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

    def test_genuine_read_failure_never_masquerades_as_not_initialized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUD-3: a real read failure (corrupted state, I/O error, a bug —
        anything other than the project genuinely never having been
        bootstrapped) must propagate, never be silently folded into
        ``initialized=False``. Only the one real, typed "project truly
        does not exist yet" case (``UnknownProjectError``) may produce
        that snapshot."""
        from orchestrator.project_state import ProjectStateStore

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        def _boom(self, project_id):
            raise RuntimeError("simulated corrupted project store")

        monkeypatch.setattr(ProjectStateStore, "get_project", _boom)

        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        with pytest.raises(RuntimeError, match="simulated corrupted project store"):
            engine.status()


class TestExecutionTimes:
    def _bootstrapped(self, tmp_path: Path):
        from orchestrator.execution_store import ExecutionStore
        from orchestrator.project_runtime import STORE_FILENAMES, ProjectRuntime

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()
        store = ExecutionStore(config.project.state_dir / STORE_FILENAMES["executions"])
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        return engine, store

    @staticmethod
    def _add(store, eid: str, provider: str, seconds: float | None, *, task="wi-1", role="developer", fail=False):
        store.create(
            execution_id=eid, task_id=task, worker_id="w", provider=provider, backend="b", model="m",
            role=role, started_at=UTC_T0,
        )
        if seconds is None:
            return
        end = UTC_T0 + timedelta(seconds=seconds)
        if fail == "interrupted":
            store.mark_interrupted(eid, finished_at=end)
        elif fail == "recovered":
            store.mark_recovery_required(eid, finished_at=end)
        elif fail:
            store.mark_failed(eid, finished_at=end)
        else:
            store.mark_succeeded(eid, finished_at=end)

    def test_not_bootstrapped_is_empty_and_creates_nothing(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        snap = engine.execution_times()
        assert isinstance(snap, ExecutionTimeSnapshot)
        assert snap == ExecutionTimeSnapshot(mvp_id="mvp-1", providers=(), total_seconds=None, unknown_executions=0)
        assert not config.project.state_dir.exists()

    def test_bootstrapped_without_executions_is_empty(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        store.close()
        snap = engine.execution_times()
        assert snap.providers == () and snap.total_seconds is None and snap.unknown_executions == 0

    def test_one_provider_sums_every_attempt(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", 10)
        self._add(store, "e2", "anthropic", 5.5)
        store.close()
        snap = engine.execution_times()
        assert [(p.provider, p.seconds, p.executions, p.unknown_executions) for p in snap.providers] == [
            ("anthropic", 15.5, 2, 0)
        ]
        assert snap.total_seconds == 15.5 and snap.unknown_executions == 0

    def test_several_providers_sorted(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "openai", 3)
        self._add(store, "e2", "anthropic", 4)
        self._add(store, "e3", "mistral", 5)
        store.close()
        snap = engine.execution_times()
        assert [p.provider for p in snap.providers] == ["anthropic", "mistral", "openai"]
        assert snap.total_seconds == 12

    def test_failed_interrupted_and_recovered_are_counted(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", 2, fail=True)
        self._add(store, "e2", "anthropic", 3, fail="interrupted")
        self._add(store, "e3", "anthropic", 4, fail="recovered")
        store.close()
        snap = engine.execution_times()
        assert snap.providers[0].executions == 3 and snap.providers[0].seconds == 9

    def test_running_execution_is_unknown_not_zero(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", None)
        self._add(store, "e2", "openai", 7)
        self._add(store, "e3", "openai", None)
        store.close()
        snap = engine.execution_times()
        by = {p.provider: p for p in snap.providers}
        assert by["anthropic"].seconds is None and by["anthropic"].unknown_executions == 1
        assert by["openai"].seconds == 7 and by["openai"].executions == 2 and by["openai"].unknown_executions == 1
        assert snap.total_seconds == 7 and snap.unknown_executions == 2

    def test_only_unknown_gives_no_total(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", None)
        store.close()
        snap = engine.execution_times()
        assert snap.total_seconds is None and snap.unknown_executions == 1

    def test_non_work_item_roles_excluded(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", 4)
        self._add(store, "e2", "openai", 100, role="estimator")
        self._add(store, "e3", "mistral", 100, role="qa")
        store.close()
        snap = engine.execution_times()
        assert [p.provider for p in snap.providers] == ["anthropic"]
        assert snap.total_seconds == 4

    def test_other_mvp_executions_excluded(self, tmp_path: Path) -> None:
        from orchestrator.project_runtime import STORE_FILENAMES
        from orchestrator.project_state import ProjectStateStore

        engine, store = self._bootstrapped(tmp_path)
        config = ProjectConfig.load(tmp_path / "aido.yaml")
        projects = ProjectStateStore(config.project.state_dir / STORE_FILENAMES["project"])
        try:
            projects.create_mvp(mvp_id="mvp-2", project_id=config.project.id, objective="Other MVP")
            projects.create_work_item(work_item_id="wi-2", mvp_id="mvp-2", title="Other work")
        finally:
            projects.close()
        self._add(store, "e1", "anthropic", 4)
        self._add(store, "e2", "openai", 50, task="wi-2")
        store.close()
        snap = engine.execution_times()
        assert [p.provider for p in snap.providers] == ["anthropic"]

    def test_never_calls_a_provider(self, tmp_path: Path) -> None:
        engine, store = self._bootstrapped(tmp_path)
        self._add(store, "e1", "anthropic", 1)
        store.close()
        engine.execution_times()  # _NeverCalledAdapter.probe would raise


class TestProbeWorkers:
    def test_independent_probes_overlap_and_preserve_partial_failure(self, tmp_path: Path) -> None:
        started = set()
        both_started = None

        class BarrierAdapter(_FakeAdapter):
            def __init__(self, name):
                super().__init__()
                self.name = name

            async def probe(self):
                nonlocal both_started
                if both_started is None:
                    both_started = asyncio.Event()
                started.add(self.name)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)
                if self.name == "openai":
                    self.probe_calls += 1
                    raise RuntimeError("probe unavailable")
                return await super().probe()

        registry = REGISTRY_TWO_WORKERS.replace("provider: anthropic", "provider: openai", 1)
        config_path = _write_config(tmp_path, registry=registry)
        adapters = {p: BarrierAdapter(p) for p in ("anthropic", "openai")}
        engine = OrchestratorEngine.open(str(config_path), provider_adapters=adapters)

        # Explicit probes stay fresh on each call, with one attempt per provider.
        for count in (1, 2):
            started.clear()
            both_started = None
            snapshots = engine.probe_workers()
            assert [s.provider for s in snapshots] == ["anthropic", "openai"]
            assert snapshots[0].available is True
            assert snapshots[1].available is False
            assert "probe unavailable" in snapshots[1].reason
            assert all(a.probe_calls == count for a in adapters.values())

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
    def test_run_drives_full_workitem_flow_to_completed(self, tmp_path: Path, monkeypatch) -> None:
        from orchestrator.internal_qa_engine import InternalQAEngine

        plans = []
        build_plan = InternalQAEngine.build_plan

        def record_plan(self, request, **kwargs):
            plan = build_plan(self, request, **kwargs)
            plans.append(plan)
            return plan

        monkeypatch.setattr(InternalQAEngine, "build_plan", record_plan)
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
        assert len(plans) == 1  # same plan supplies the manifest and the actual QA run
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

    def test_refused_merge_is_readable_then_resumed_by_the_next_run(self, tmp_path: Path, monkeypatch) -> None:
        from orchestrator.git_governance import DirtyWorkingTreeError, GitGovernanceService

        real_merge = GitGovernanceService.merge

        def refuse(self, *a, **k):
            raise DirtyWorkingTreeError(["__pycache__/x.pyc"])

        monkeypatch.setattr(GitGovernanceService, "merge", refuse)
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
        with pytest.raises(EngineError, match=r"merge is pending.*__pycache__/x\.pyc.*run again") as excinfo:
            engine.run()
        assert isinstance(excinfo.value.__cause__.__cause__, DirtyWorkingTreeError)

        monkeypatch.setattr(GitGovernanceService, "merge", real_merge)
        second_runner = _ScriptedRalphRunner([])
        seen: list[EngineEvent] = []
        result = OrchestratorEngine.open(
            str(config_path), provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=second_runner,
        ).run(on_event=seen.append)

        assert [e.kind for e in seen] == ["git.merge_ready", "git.merge_completed", "work_item.completed"]
        assert seen[1].payload == {"tag": "feature/wi-1/done"}
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


class TestThreadInterrupt:
    """``run(interrupt=...)`` gives a caller running the engine off the
    main thread (where Ctrl+C never reaches ``asyncio.run()``) the same
    graceful interruption as a real Ctrl+C."""

    def test_interrupt_set_from_another_thread_cancels_dev_a_like_ctrl_c(self, tmp_path: Path) -> None:
        import threading

        started = threading.Event()

        class BlockingRunner:
            calls = 0

            async def __call__(self, args, cwd, timeout, *, on_output=None):
                BlockingRunner.calls += 1
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("the blocked worker must be cancelled, never complete")

        engine = OrchestratorEngine.open(
            str(_write_config(tmp_path)),
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=BlockingRunner(),
        )
        interrupt = threading.Event()
        seen: list = []
        outcome: dict = {}

        def drive() -> None:
            try:
                engine.run(on_event=seen.append, interrupt=interrupt)
            except BaseException as exc:  # noqa: BLE001 - the outcome under test
                outcome["exc"] = exc

        thread = threading.Thread(target=drive)
        thread.start()
        assert started.wait(10)
        interrupt.set()
        thread.join(10)

        assert not thread.is_alive()
        assert isinstance(outcome.get("exc"), KeyboardInterrupt)
        assert BlockingRunner.calls == 1
        kinds = [e.kind for e in seen]
        assert kinds[-3:] == ["run.interruption_requested", "dev_a.interrupted", "run.interrupted"]
        assert not any(k.startswith("qa.") or k.endswith(".completed") for k in kinds)

    def test_interrupt_already_set_starts_no_cycle(self, tmp_path: Path) -> None:
        import threading

        runner = _ScriptedRalphRunner([])
        engine = OrchestratorEngine.open(
            str(_write_config(tmp_path)),
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        interrupt = threading.Event()
        interrupt.set()
        with pytest.raises(KeyboardInterrupt):
            engine.run(interrupt=interrupt)
        assert runner.calls == []


class TestOnEvent:
    """P18-01: the optional, synchronous ``on_event`` live channel.

    ``RunResult``/``RunResult.events`` must stay byte-for-byte identical
    to pre-P18 behavior in every scenario here — ``on_event`` is a
    strictly additive, live delivery channel, never a replacement.
    """

    def test_on_event_none_is_byte_for_byte_the_pre_p18_behavior(self, tmp_path: Path) -> None:
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
        result = engine.run()  # on_event omitted entirely, exactly like every pre-P18 caller
        assert result.all_terminal is True
        assert result.reached_max_cycles is False
        assert result.work_items[0].status == "completed"
        assert [e.kind for e in result.events] == ["work_item.completed"]

    def test_on_event_receives_started_live_before_run_returns_and_events_unchanged(
        self, tmp_path: Path,
    ) -> None:
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
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)

        # Live channel: work_item.started fires first (P18-01), the
        # historical work_item.completed fires last (real emission
        # order) - fine-grained DEV/QA/Git events in between are P18-02's
        # own scope, asserted in detail elsewhere.
        kinds = [e.kind for e in seen]
        assert kinds[0] == "work_item.started"
        assert kinds[-1] == "work_item.completed"
        for event in seen:
            assert event.project_id == "demo"
            assert event.mvp_id == "mvp-1"
            assert event.work_item_id == "wi-1"

        # RunResult.events is untouched by on_event: still only the one
        # coarse historical event, never the fine "started" one.
        assert [e.kind for e in result.events] == ["work_item.completed"]
        assert result.events == (seen[-1],)

    def test_on_event_exception_propagates_immediately_before_any_development_runs(
        self, tmp_path: Path,
    ) -> None:
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

        class _Boom(RuntimeError):
            pass

        def _raise(event: EngineEvent) -> None:
            raise _Boom(f"frontend callback failure on {event.kind}")

        with pytest.raises(_Boom):
            engine.run(on_event=_raise)

        # The callback raised on the very first event (work_item.started,
        # before DEV A ever ran) -> no development execution was ever
        # launched, and the exception was never swallowed/converted.
        assert runner.calls == []

    def test_on_event_deterministic_order_across_two_cycles(self, tmp_path: Path) -> None:
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
        seen: list[EngineEvent] = []
        engine.run(on_event=seen.append)
        # A single WorkItem, one fresh attempt: exactly the real order the
        # facts occurred in, never reordered/batched.
        kinds = [e.kind for e in seen]
        assert kinds.index("work_item.started") < kinds.index("work_item.completed")

    def test_on_event_receives_recovery_required_for_an_orphaned_execution(
        self, tmp_path: Path,
    ) -> None:
        """A WorkItem left RUNNING behind an orphaned execution (e.g. a
        crashed prior process — the exact scenario ``RecoveryCoordinator``
        already reconciles, see ``test_recovery.py``) must surface
        ``work_item.recovery_required`` live, before the resumed attempt's
        own events."""
        from orchestrator.project_runtime import ProjectRuntime

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)

        with ProjectRuntime.open(
            config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()},
        ) as rt:
            rt.bootstrap()
            rt.project_store.refresh_readiness("mvp-1")
            rt.project_store.mark_mvp_running("mvp-1")
            rt.project_store.mark_work_item_running("wi-1")
            rt.execution_store.create(
                execution_id="orphan-exec-1", task_id="wi-1", worker_id="alice",
                provider="anthropic", backend="claude_code", model="sonnet", role="developer",
                started_at=UTC_T0,
            )  # never finalized — simulates a crashed prior process

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
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)

        assert [e.kind for e in seen][:2] == ["work_item.recovery_required", "work_item.started"]
        assert seen[0].work_item_id == "wi-1"
        assert seen[0].project_id == "demo"
        assert seen[0].mvp_id == "mvp-1"
        # RunResult.events (the coarse historical tuple) is unaffected by
        # this new live-only event: still only the terminal outcome.
        assert [e.kind for e in result.events] == ["work_item.completed"]


class TestEngineEventLayering:
    """P18-01: EngineEvent lives in a neutral module so MVPManager never
    has to import the orchestrator.engine façade to construct one."""

    def test_engine_event_historical_import_path_still_works(self) -> None:
        from orchestrator.engine import EngineEvent as FromEngine
        from orchestrator.engine_events import EngineEvent as FromNeutralModule

        assert FromEngine is FromNeutralModule  # exactly one public type, never two

    def test_mvp_manager_does_not_import_the_engine_facade(self) -> None:
        import ast
        from pathlib import Path

        import orchestrator.mvp_manager as mvp_manager_module

        source = Path(mvp_manager_module.__file__).read_text()
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "orchestrator.engine" not in imported_modules
        assert "orchestrator.engine_events" in imported_modules


class TestWorkerRegistryInjection:
    """Engine/library boundary: a caller-constructed ``WorkerRegistry`` can
    be injected directly into ``OrchestratorEngine``, entirely independent
    of any ``workers.registry`` path in ``aido.yaml`` — see
    ``project_config.py``'s module docstring."""

    def test_injected_registry_is_used_without_a_workers_section(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, include_workers_section=False)
        assert not (tmp_path / "workers.yaml").exists()
        engine = OrchestratorEngine.open(
            str(config_path), worker_registry=_worker_registry("alice", "bob"),
            provider_adapters={"anthropic": _NeverCalledAdapter()},
        )
        workers = engine.workers()
        assert {w.worker_id for w in workers} == {"alice", "bob"}

    def test_injected_registry_reaches_validate_and_probe(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, include_workers_section=False)
        engine = OrchestratorEngine.open(
            str(config_path), worker_registry=_worker_registry("alice"),
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
        )
        snapshot = engine.validate()
        assert snapshot.enabled_worker_count == 1
        assert snapshot.providers == ("anthropic",)
        probes = engine.probe_workers()
        assert [p.provider for p in probes] == ["anthropic"]

    def test_project_without_workers_section_is_independent_of_any_registry_path(
        self, tmp_path: Path,
    ) -> None:
        config_path = _write_config(tmp_path, include_workers_section=False)
        config = ProjectConfig.load(config_path)
        assert config.workers_registry_path is None
        with pytest.raises(NoWorkerRegistryConfiguredError):
            config.load_worker_registry()

    def test_engine_without_injection_and_without_workers_section_fails_closed(
        self, tmp_path: Path,
    ) -> None:
        config_path = _write_config(tmp_path, include_workers_section=False)
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={})
        with pytest.raises(EngineConfigError):
            engine.workers()

    def test_legacy_workers_section_still_works_when_no_registry_is_injected(
        self, tmp_path: Path,
    ) -> None:
        config_path = _write_config(tmp_path)  # includes workers: registry: workers.yaml
        engine = OrchestratorEngine.open(str(config_path), provider_adapters={"anthropic": _NeverCalledAdapter()})
        assert {w.worker_id for w in engine.workers()} == {"alice", "bob"}

    def test_two_projects_share_one_injected_registry(self, tmp_path: Path) -> None:
        registry = _worker_registry("alice")
        config_a = _write_config(
            tmp_path / "a", project_id="proj-a", state_dir="state", include_workers_section=False,
        )
        config_b = _write_config(
            tmp_path / "b", project_id="proj-b", state_dir="state", include_workers_section=False,
        )
        engine_a = OrchestratorEngine.open(
            str(config_a), worker_registry=registry, provider_adapters={"anthropic": _NeverCalledAdapter()},
        )
        engine_b = OrchestratorEngine.open(
            str(config_b), worker_registry=registry, provider_adapters={"anthropic": _NeverCalledAdapter()},
        )
        assert engine_a.validate().project_id == "proj-a"
        assert engine_b.validate().project_id == "proj-b"
        assert {w.worker_id for w in engine_a.workers()} == {"alice"}
        assert {w.worker_id for w in engine_b.workers()} == {"alice"}

    def test_run_completes_workitem_flow_using_only_an_injected_registry(self, tmp_path: Path) -> None:
        """Selection is unchanged end to end: DEV A != DEV B still holds
        with two injected workers, and no workers.yaml is ever written or
        read — the exact same WorkItem Flow WorkerSelector already runs,
        never a second, façade-reimplemented selection."""
        config_path = _write_config(tmp_path, include_workers_section=False)
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        engine = OrchestratorEngine.open(
            str(config_path), worker_registry=_worker_registry("alice", "bob"),
            provider_adapters={"anthropic": _FakeAdapter(available=True)}, subprocess_runner=runner,
        )
        result = engine.run()
        assert result.all_terminal is True
        assert result.work_items[0].status == "completed"
        assert not (tmp_path / "workers.yaml").exists()


class TestEngineLibraryBoundaryIntegration:
    """§11: one offline integration test proving a caller can build its
    own WorkerRegistry + its own typed project plan (``ProjectConfig``'s
    own plain constructor — never a second, parallel "plan" type, REUSE
    FIRST) and open a valid OrchestratorEngine runtime from them, without
    ``aido.yaml``/``workers.yaml`` ever being read from disk."""

    def test_registry_and_plan_built_entirely_in_python_produce_a_valid_runtime(
        self, tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "proj"
        _init_git_repo(workspace)
        state_dir = tmp_path / "state"

        config = ProjectConfig(
            schema_version=1,
            project=ProjectIdentity(id="demo", name="Demo", workspace=workspace, state_dir=state_dir),
            execution=ExecutionConfig(permission_mode=ExecutionPermissionMode.STANDARD),
            git=GitConfig(base_branch="main"),
            mvp=MVPConfig(id="mvp-1", objective="Ship it", acceptance_criteria=()),
            work_items=(
                WorkItemConfig(
                    id="wi-1", title="Do the thing", required_capabilities=("development",),
                    dependencies=(), acceptance_criteria=(),
                ),
            ),
            qa_commands=(
                ValidationCommand(
                    validation_id="qa-1", kind=ValidationKind.UNIT_TEST,
                    argv=(sys.executable, "-c", "pass"), timeout_seconds=300.0, required=True,
                ),
            ),
            source_path=tmp_path / "in-memory-plan",
        )
        assert config.workers_registry_path is None

        registry = _worker_registry("alice", "bob")
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        engine = OrchestratorEngine(
            config, worker_registry=registry,
            provider_adapters={"anthropic": _FakeAdapter(available=True)}, subprocess_runner=runner,
        )

        snapshot = engine.validate()
        assert snapshot.project_id == "demo"
        assert snapshot.enabled_worker_count == 2

        result = engine.run()
        assert result.all_terminal is True
        assert result.work_items[0].status == "completed"
        assert not (tmp_path / "workers.yaml").exists()
        assert not (tmp_path / "aido.yaml").exists()


def _claude_ndjson(text: str) -> str:
    """One public assistant text record, as a Claude worker emits it."""
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text.rstrip("\n")}]}}) + "\n"


class _StreamingRalphRunner(_ScriptedRalphRunner):
    """Same as ``_ScriptedRalphRunner`` but opts into progressive
    observation (``on_output``) and replays scripted ``output`` chunks."""

    async def __call__(self, args, cwd, timeout, *, on_output=None, on_heartbeat=None):
        for item in self._steps[0].get("output", ()):
            if item[0] == "heartbeat":
                on_heartbeat(item[1])
            elif item[0] == "stdout":
                # Scripted plain text stands for one public Claude text block.
                on_output("stdout", _claude_ndjson(item[1]))
            else:
                on_output(*item)
        return await super().__call__(args, cwd, timeout)


_MAX_ITERATIONS_TERMINATE = (
    "## Reason\nmax_iterations\n\n## Status\nLoop stopped.\n\n"
    "## Summary\n- Iterations: 5\n- Duration: 1m\n- Exit code: 2"
)


def _open_engine(tmp_path: Path, steps: list[dict]) -> OrchestratorEngine:
    return OrchestratorEngine.open(
        str(_write_config(tmp_path)), provider_adapters={"anthropic": _FakeAdapter(available=True)},
        subprocess_runner=_StreamingRalphRunner(steps),
    )


class TestExecutionOutputAndDiagnostics:
    """P21-02: ``execution.output`` live events and typed failure diagnostics."""

    def test_execution_output_reaches_on_event_with_real_identity(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "work.completed", "mutate": _commit_action("f.py", "x = 1\n", "DEV A"),
             "output": [("stdout", "hello\n"), ("stderr", "warn\n")]},
            {"topic": "work.completed"},
        ])
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)

        outputs = [e for e in seen if e.kind == "execution.output"]
        # Claude stderr is never published (P21.1).
        assert [(e.payload["stream"], e.payload["text"]) for e in outputs] == [("stdout", "hello\n")]
        first = outputs[0]
        assert first.phase == "dev_a"
        assert first.execution_id is not None
        assert first.worker_display_name in {"Alice", "Bob"}
        assert (first.provider, first.backend) == ("anthropic", "claude_code")
        assert (first.profile_id, first.model) == ("standard", "sonnet")
        assert (first.project_id, first.mvp_id, first.work_item_id) == ("demo", "mvp-1", "wi-1")
        # Output comes between dev_a.started and dev_a.completed, and
        # never enters RunResult.events nor diagnostics on success.
        kinds = [e.kind for e in seen]
        assert kinds.index("dev_a.started") < kinds.index("execution.output") < kinds.index("dev_a.completed")
        assert all(e.kind != "execution.output" for e in result.events)
        assert result.diagnostics == ()

    def test_heartbeat_event_carries_identity_and_elapsed_only_then_output_resumes(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "work.completed", "mutate": _commit_action("f.py", "x = 1\n", "DEV A"),
             "output": [("heartbeat", 15.25), ("stdout", "back\n")]},
            {"topic": "work.completed"},
        ])
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)

        live = [e for e in seen if e.kind.startswith("execution.")]
        assert [e.kind for e in live] == ["execution.heartbeat", "execution.output"]
        beat = live[0]
        assert beat.payload == {"elapsed_seconds": 15.25}
        assert beat.phase == "dev_a" and beat.execution_id is not None
        assert (beat.provider, beat.backend, beat.model) == ("anthropic", "claude_code", "sonnet")
        assert beat.execution_id == live[1].execution_id
        assert all(not e.kind.startswith("execution.") for e in result.events)
        assert result.work_items[0].status == "completed"

    def test_failing_heartbeat_callback_is_counted_not_fatal(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "loop.terminate", "payload": _MAX_ITERATIONS_TERMINATE, "exit_code": 2,
             "output": [("heartbeat", 1.0)]},
        ])

        def _on_event(event: EngineEvent) -> None:
            if event.kind == "execution.heartbeat":
                raise RuntimeError("renderer bug")

        (diag,) = engine.run(on_event=_on_event).diagnostics
        assert diag.output_delivery_failures == 1
        assert diag.business_verdict == "absent"

    def test_non_live_run_result_explains_max_iterations_failure(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "loop.terminate", "payload": _MAX_ITERATIONS_TERMINATE, "exit_code": 2,
             "stdout": _claude_ndjson("backend said something").encode(), "stderr": b"line 1\nraw stderr\n"},
        ])
        result = engine.run()

        assert result.work_items[0].status == "failed"
        (diag,) = result.diagnostics
        assert diag.phase == "dev_a"
        assert diag.business_verdict == "absent"
        assert diag.exit_code == 2
        assert diag.ralph_termination_reason == "max_iterations"
        assert diag.ralph_iterations == 5
        assert diag.last_output_stream == "stdout"
        assert diag.last_output == "backend said something"
        assert diag.summary == (
            "Ralph terminated with max_iterations after 5 iterations; "
            "no work.completed/work.failed event; exit_code=2."
        )
        assert "new governed WorkItem" in diag.next_action
        assert "provider" not in diag.summary.lower()
        assert (diag.model, diag.provider, diag.backend) == ("sonnet", "anthropic", "claude_code")

    def test_untrusted_termination_reason_never_enters_public_diagnostic(self, tmp_path: Path) -> None:
        sentinel = "PRIVATE_REASONING_SENTINEL"
        engine = _open_engine(tmp_path, [{
            "topic": "loop.terminate", "payload": f"## Reason\n{sentinel}\n\n- Iterations: 5",
            "exit_code": 2, "stderr": sentinel.encode(),
        }])
        (diag,) = engine.run().diagnostics
        assert diag.ralph_termination_reason is None
        assert sentinel not in repr(diag)

    def test_unknown_termination_payload_is_reported_as_unknown(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "loop.terminate", "payload": "garbled", "exit_code": 1},
        ])
        (diag,) = engine.run().diagnostics
        assert diag.ralph_termination_reason is None
        assert diag.business_verdict == "absent"
        assert diag.summary.startswith("Ralph termination reason unknown;")

    def test_timeout_diagnostic_retains_observed_output_without_live_callback(self, tmp_path: Path) -> None:
        from orchestrator.ralph_execution_engine import RalphTimeoutError

        class TimingOutRunner:
            async def __call__(self, args, cwd, timeout, *, on_output=None):
                on_output("stdout", _claude_ndjson("worker progress before timeout"))
                raise RalphTimeoutError("timed out")

        engine = OrchestratorEngine.open(
            str(_write_config(tmp_path)),
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=TimingOutRunner(),
        )
        (diag,) = engine.run().diagnostics
        assert diag.execution_status == "interrupted"
        assert diag.business_verdict == "absent"
        assert diag.last_output == "worker progress before timeout"
        assert diag.last_output_stream == "stdout"

    def test_explicit_work_failed_keeps_business_verdict_failed(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [{"topic": "work.failed", "exit_code": 0}])
        (diag,) = engine.run().diagnostics
        assert diag.business_verdict == "failed"
        assert diag.exit_code == 0

    def test_failing_output_callback_never_kills_worker(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "work.completed", "mutate": _commit_action("f.py", "x = 1\n", "DEV A"),
             "output": [("stdout", "a\n"), ("stdout", "b\n")]},
            {"topic": "work.completed"},
        ])
        seen: list[str] = []

        def _on_event(event: EngineEvent) -> None:
            seen.append(event.kind)
            if event.kind == "execution.output":
                raise RuntimeError("renderer bug")

        result = engine.run(on_event=_on_event)
        assert result.work_items[0].status == "completed"
        assert seen.count("execution.output") == 2

    def test_failing_output_callback_is_counted_in_diagnostic_not_blamed(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "loop.terminate", "payload": _MAX_ITERATIONS_TERMINATE, "exit_code": 2,
             "output": [("stdout", "a\n")]},
        ])

        def _on_event(event: EngineEvent) -> None:
            if event.kind == "execution.output":
                raise RuntimeError("renderer bug")

        (diag,) = engine.run(on_event=_on_event).diagnostics
        assert diag.output_delivery_failures == 1
        # Type name only: an exception message may echo output text.
        assert diag.last_output_delivery_error == "RuntimeError"
        assert diag.business_verdict == "absent"

    def test_transition_callback_exception_still_propagates(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [{"topic": "work.completed"}])

        def _on_event(event: EngineEvent) -> None:
            if event.kind == "dev_a.started":
                raise RuntimeError("transition renderer bug")

        with pytest.raises(RuntimeError, match="transition renderer bug"):
            engine.run(on_event=_on_event)


class TestBoundedLiveOutput:
    """P21-04: bounded public ``execution.output`` flow, single truncation signal."""

    @staticmethod
    def _steps(output: list, *, topic: str = "work.completed", **extra) -> list[dict]:
        return [
            {"topic": topic, "mutate": _commit_action("f.py", "x = 1\n", "DEV A"), "output": output, **extra},
            {"topic": "work.completed"},
        ]

    def test_normal_flow_is_untouched(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, self._steps([("stdout", f"l{i}\n") for i in range(50)]))
        seen: list[EngineEvent] = []
        engine.run(on_event=seen.append)
        assert sum(e.kind == "execution.output" for e in seen) == 50
        assert all(e.kind != "execution.output_truncated" for e in seen)

    def test_event_burst_is_truncated_once(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, self._steps([("stdout", "x\n")] * 300))
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)
        assert result.work_items[0].status == "completed"
        assert sum(e.kind == "execution.output" for e in seen) == 100
        (signal,) = [e for e in seen if e.kind == "execution.output_truncated"]
        assert signal.payload == {"reason": "max_event_rate", "delivered_events": 100, "delivered_chars": 200}
        assert signal.phase == "dev_a" and signal.execution_id is not None
        kinds = [e.kind for e in seen]
        assert kinds.index("execution.output_truncated") < kinds.index("dev_a.completed")

    def test_total_event_budget_is_shared_across_streams(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestrator import mvp_manager

        # Keep the rate gate out of this case so the lifetime event gate is exercised.
        monkeypatch.setattr(mvp_manager, "_MAX_LIVE_OUTPUT_EVENTS_PER_SECOND", 1_000)
        output = [("stdout", "x") for i in range(502)]
        engine = _open_engine(tmp_path, self._steps(output))
        seen: list[EngineEvent] = []
        assert engine.run(on_event=seen.append).work_items[0].status == "completed"
        delivered = [e for e in seen if e.kind == "execution.output"]
        assert len(delivered) == 500
        assert {e.payload["stream"] for e in delivered} == {"stdout"}
        (signal,) = [e for e in seen if e.kind == "execution.output_truncated"]
        assert signal.payload == {"reason": "max_events", "delivered_events": 500, "delivered_chars": 1000}

    def test_volume_overflow_cuts_at_char_budget_with_single_signal(self, tmp_path: Path) -> None:
        big = "y" * 30_000
        engine = _open_engine(tmp_path, self._steps([("stdout", big)] * 4 + [("stdout", "tail\n")]))
        seen: list[EngineEvent] = []
        engine.run(on_event=seen.append)
        outputs = [e.payload["text"] for e in seen if e.kind == "execution.output"]
        assert sum(map(len, outputs)) == 64_000
        signals = [e for e in seen if e.kind == "execution.output_truncated"]
        assert len(signals) == 1 and signals[0].payload["reason"] == "max_chars"

    def test_timeout_diagnostic_keeps_last_output_after_live_truncation(self, tmp_path: Path) -> None:
        from orchestrator.ralph_execution_engine import RalphTimeoutError

        class NoisyTimeoutRunner:
            async def __call__(self, args, cwd, timeout, *, on_output=None):
                for _ in range(300):
                    on_output("stdout", _claude_ndjson("noise"))
                on_output("stdout", _claude_ndjson("final words"))
                raise RalphTimeoutError("timed out")

        engine = OrchestratorEngine.open(
            str(_write_config(tmp_path)), provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=NoisyTimeoutRunner(),
        )
        seen: list[EngineEvent] = []
        (diag,) = engine.run(on_event=seen.append).diagnostics
        assert diag.execution_status == "interrupted"
        assert diag.last_output.endswith("noise\nfinal words") and diag.last_output_stream == "stdout"
        assert sum(e.kind == "execution.output_truncated" for e in seen) == 1
        assert all("final words" not in e.payload.get("text", "") for e in seen if e.kind == "execution.output")

    def test_final_capture_survives_live_truncation(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, [
            {"topic": "work.failed", "output": [("stdout", "noise\n")] * 101 +
             [("stdout", "late diagnostic\n")], "stdout": _claude_ndjson("late diagnostic").encode()},
        ])
        seen: list[EngineEvent] = []
        result = engine.run(on_event=seen.append)
        (diag,) = result.diagnostics
        assert diag.last_output == "late diagnostic"
        assert diag.last_output_stream == "stdout"
        assert sum(e.kind == "execution.output_truncated" for e in seen) == 1
        assert all(e.payload.get("text") != "late diagnostic\n" for e in seen if e.kind == "execution.output")

    def test_failing_callback_on_truncation_signal_is_not_fatal(self, tmp_path: Path) -> None:
        engine = _open_engine(tmp_path, self._steps([("stdout", "x\n")] * 150))

        def _on_event(event: EngineEvent) -> None:
            if event.kind == "execution.output_truncated":
                raise RuntimeError("renderer bug")

        assert engine.run(on_event=_on_event).work_items[0].status == "completed"
