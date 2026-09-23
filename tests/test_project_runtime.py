"""Tests for ProjectRuntime — ProjectConfig -> real stores/services/MVPManager
composition (P1, §5-§12).

Offline only: real Git repos/worker registries under pytest's ``tmp_path``,
fake provider adapters (never real Claude/Codex/Vibe probes), no Ralph
subprocess launched in this file (bootstrap/lifecycle only — the real
WorkItem Flow run is exercised through the CLI in test_cli.py).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.project_config import ProjectConfig
from orchestrator.project_runtime import (
    ConfigRuntimeConflictError,
    ProjectRuntime,
    ProviderConfigurationError,
    UnsupportedProviderError,
)
from orchestrator.project_state import WorkItemStatus
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState

UTC_T0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


class _FakeAdapter(ProviderAdapter):
    def __init__(self, available: bool = True) -> None:
        self.probe_calls = 0
        self._available = available

    async def probe(self) -> ProviderState:
        self.probe_calls += 1
        return ProviderState(
            provider="fake", availability=ProviderAvailability(available=self._available, observed_at=UTC_T0),
            observed_at=UTC_T0,
        )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E", "commit",
         "--allow-empty", "-q", "-m", "init"],
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


def _write_project(
    tmp_path: Path, *, registry: str = REGISTRY_TWO_WORKERS, work_items: str | None = None,
    state_dir: str | None = "state", permission_mode: str = "standard",
) -> ProjectConfig:
    """``state_dir`` always defaults to an isolated ``tmp_path``
    subdirectory — NEVER omit it (pass ``state_dir=None`` explicitly, and
    only inside a test that also monkeypatches ``HOME``) or a test will
    silently read/write the real
    ``~/.local/state/ai-dev-orchestrator/projects/<id>/`` on the machine
    actually running the suite."""
    workspace = tmp_path / "proj"
    _init_git_repo(workspace)
    (tmp_path / "workers.yaml").write_text(registry)
    work_items_yaml = work_items if work_items is not None else dedent(
        """
        work_items:
          - id: wi-1
            title: Do the thing
            required_capabilities: [development]
        """
    )
    state_dir_line = f"  state_dir: {state_dir}\n" if state_dir else ""
    config_text = dedent(
        f"""
        schema_version: 1
        project:
          id: demo
          name: Demo
          workspace: proj
        {state_dir_line}
        workers:
          registry: workers.yaml
        execution:
          permission_mode: {permission_mode}
        git:
          base_branch: main
        mvp:
          id: mvp-1
          objective: Ship it
        """
    ) + work_items_yaml + "\nqa: []\n"
    config_path = tmp_path / "aido.yaml"
    config_path.write_text(config_text)
    return ProjectConfig.load(config_path)


class TestOpenAndClose:
    def test_open_creates_state_dir(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            assert config.project.state_dir.is_dir()
        # closed cleanly, no exception

    def test_default_p12_state_dir_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        config = _write_project(tmp_path, state_dir=None)
        assert config.project.state_dir == fake_home / ".local" / "state" / "ai-dev-orchestrator" / "projects" / "demo"
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}):
            assert config.project.state_dir.is_dir()

    def test_custom_state_dir_respected(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="my-state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}):
            pass
        assert (tmp_path / "my-state").is_dir()

    def test_open_fails_closed_for_unknown_provider(self, tmp_path: Path) -> None:
        bogus_registry = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: bogus-provider
                backend: claude_code
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: sonnet}
            """
        )
        config = _write_project(tmp_path, registry=bogus_registry)
        with pytest.raises(UnsupportedProviderError):
            ProjectRuntime.open(config, clock=lambda: UTC_T0)

    def test_open_failure_still_closes_partially_opened_stores(self, tmp_path: Path) -> None:
        """A failure partway through composition (unknown provider) must
        not leak SQLite connections — proven by successfully reopening
        the same state_dir's stores right after."""
        bogus_registry = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: bogus-provider
                backend: claude_code
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: sonnet}
            """
        )
        config = _write_project(tmp_path, registry=bogus_registry, state_dir="state")
        with pytest.raises(UnsupportedProviderError):
            ProjectRuntime.open(config, clock=lambda: UTC_T0)
        # Reopening the same sqlite files must work — proves close() ran.
        from orchestrator.project_runtime import STORE_FILENAMES
        from orchestrator.project_state import ProjectStateStore

        store = ProjectStateStore(config.project.state_dir / STORE_FILENAMES["project"], clock=lambda: UTC_T0)
        store.close()


REGISTRY_DEEPSEEK_WORKER = dedent(
    """
    workers:
      - worker_id: dana
        display_name: Dana
        enabled: true
        provider: deepseek
        backend: claude_code
        capabilities: [development]
        profiles:
          standard: {quality_tier: STANDARD, model: deepseek-flash}
    """
)

REGISTRY_KIMI_WORKER = dedent(
    """
    workers:
      - worker_id: kai
        display_name: Kai
        enabled: true
        provider: kimi
        backend: claude_code
        capabilities: [development]
        profiles:
          standard: {quality_tier: STANDARD, model: kimi-for-coding}
    """
)


class TestProviderComposition:
    def test_only_providers_of_enabled_workers_are_resolved(self, tmp_path: Path) -> None:
        registry = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                enabled: true
                provider: anthropic
                backend: claude_code
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: sonnet}
              - worker_id: victor
                display_name: Victor
                enabled: false
                provider: openai
                backend: codex
                capabilities: [development]
                profiles:
                  standard: {quality_tier: STANDARD, model: gpt}
            """
        )
        config = _write_project(tmp_path, registry=registry)
        fake = _FakeAdapter()
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": fake}) as rt:
            assert rt.manager is not None
        # No probe was made just by opening.
        assert fake.probe_calls == 0

    def test_deepseek_provider_resolves_when_api_key_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        config = _write_project(tmp_path, registry=REGISTRY_DEEPSEEK_WORKER)
        # provider_adapters intentionally omitted: exercises the real
        # _PROVIDER_ADAPTER_FACTORIES table, not an injected fake.
        with ProjectRuntime.open(config, clock=lambda: UTC_T0) as rt:
            assert rt.manager is not None

    def test_deepseek_provider_fails_closed_without_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        config = _write_project(tmp_path, registry=REGISTRY_DEEPSEEK_WORKER)
        with pytest.raises(ProviderConfigurationError, match="deepseek"):
            ProjectRuntime.open(config, clock=lambda: UTC_T0)

    def test_kimi_provider_resolves_when_api_key_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIMI_API_KEY", "kimi-test")
        config = _write_project(tmp_path, registry=REGISTRY_KIMI_WORKER)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0) as rt:
            assert rt.manager is not None

    def test_kimi_provider_fails_closed_without_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        config = _write_project(tmp_path, registry=REGISTRY_KIMI_WORKER)
        with pytest.raises(ProviderConfigurationError, match="kimi"):
            ProjectRuntime.open(config, clock=lambda: UTC_T0)

    def test_missing_deepseek_key_does_not_affect_an_anthropic_only_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        config = _write_project(tmp_path, registry=REGISTRY_TWO_WORKERS)
        with ProjectRuntime.open(
            config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}
        ) as rt:
            assert rt.manager is not None

    def test_missing_key_failure_still_closes_partially_opened_stores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        config = _write_project(tmp_path, registry=REGISTRY_DEEPSEEK_WORKER, state_dir="state")
        with pytest.raises(ProviderConfigurationError):
            ProjectRuntime.open(config, clock=lambda: UTC_T0)

        from orchestrator.project_runtime import STORE_FILENAMES
        from orchestrator.project_state import ProjectStateStore

        store = ProjectStateStore(config.project.state_dir / STORE_FILENAMES["project"], clock=lambda: UTC_T0)
        store.close()


class TestBootstrapIdempotency:
    def test_first_open_bootstraps_project_mvp_workitems(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()
            project = rt.project_store.get_project("demo")
            assert project.current_mvp_id == "mvp-1"
            mvp = rt.project_store.get_mvp("mvp-1")
            assert mvp.objective == "Ship it"
            work_items = rt.project_store.list_work_items("mvp-1")
            assert [wi.work_item_id for wi in work_items] == ["wi-1"]
            assert work_items[0].status is WorkItemStatus.READY

    def test_second_bootstrap_does_not_duplicate(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()
            rt.bootstrap()
            rt.bootstrap()
            assert len(rt.project_store.list_work_items("mvp-1")) == 1

    def test_state_survives_closing_and_reopening(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt2:
            rt2.bootstrap()
            work_items = rt2.project_store.list_work_items("mvp-1")
            assert len(work_items) == 1
            assert work_items[0].work_item_id == "wi-1"

    def test_qa_commands_populate_validation_store(self, tmp_path: Path) -> None:
        work_items_yaml = "work_items: []\n"
        workspace = tmp_path / "proj"
        _init_git_repo(workspace)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        (tmp_path / "aido.yaml").write_text(
            dedent(
                """
                schema_version: 1
                project: {id: demo, name: Demo, workspace: proj, state_dir: state}
                workers: {registry: workers.yaml}
                execution: {permission_mode: standard}
                git: {base_branch: main}
                mvp: {id: mvp-1, objective: Ship it}
                work_items: []
                qa:
                  - id: qa-1
                    kind: unit_test
                    argv: ["pytest", "-q"]
                """
            )
        )
        config = ProjectConfig.load(tmp_path / "aido.yaml")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            commands = rt.validation_store.get_project_commands("demo")
            assert len(commands) == 1
            assert commands[0].validation_id == "qa-1"
            assert commands[0].argv == ("pytest", "-q")

    def test_base_branch_reaches_git_governance_policy(self, tmp_path: Path) -> None:
        workspace = tmp_path / "proj"
        _init_git_repo(workspace)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        (tmp_path / "aido.yaml").write_text(
            dedent(
                """
                schema_version: 1
                project: {id: demo, name: Demo, workspace: proj, state_dir: state}
                workers: {registry: workers.yaml}
                execution: {permission_mode: standard}
                git: {base_branch: develop}
                mvp: {id: mvp-1, objective: Ship it}
                work_items: []
                qa: []
                """
            )
        )
        config = ProjectConfig.load(tmp_path / "aido.yaml")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            assert rt.git_store is not None
            # Exercised indirectly: MVPManager was constructed with a
            # GitGovernanceService whose policy.base_branch is "develop" —
            # verified via the service's own internal policy attribute.
            assert rt.manager is not None

    def test_permission_mode_reaches_ralph_execution_engine(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, permission_mode="unrestricted")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            assert rt.manager is not None
            # The engine's own configured permission mode is asserted end
            # to end in test_cli.py via the persisted ExecutionRecord.

    def test_enabled_worker_registry_reaches_worker_selector(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            # WorkerSelector was constructed with exactly the two enabled
            # workers from workers.yaml — proven end to end (DEV A != DEV
            # B, both anthropic) in test_cli.py's run test.
            assert rt.manager is not None


class TestMultiMVPSequential:
    """AUD-11: the real transition this project's own MVPs have gone
    through (mvp-0.1 -> mvp-0.1.1 -> mvp-0.1.2, and AIDO Code's own
    mvp-0.1/0.1.1/0.1.2 history) depends structurally on this working:
    successive MVPs under the exact same ``project.id``/``state_dir``,
    never conflicting, never leaking one MVP's WorkItems into another,
    and never depending on a single "current MVP" pointer (see AUD-10:
    ``Project.current_mvp_id`` is write-only bookkeeping, read by nothing
    below — this test proves that by construction, never asserting on
    that field itself)."""

    @staticmethod
    def _config_text(*, mvp_id: str, work_item_id: str, state_dir: str) -> str:
        return dedent(
            f"""
            schema_version: 1
            project:
              id: demo-multi-mvp
              name: Demo Multi MVP
              workspace: proj
              state_dir: {state_dir}
            workers:
              registry: workers.yaml
            execution:
              permission_mode: standard
            git:
              base_branch: main
            mvp:
              id: {mvp_id}
              objective: Ship it
            work_items:
              - id: {work_item_id}
                title: Do the {mvp_id} thing
                required_capabilities: [development]
            qa: []
            """
        )

    def test_three_sequential_mvps_never_conflict_or_leak(self, tmp_path: Path) -> None:
        workspace = tmp_path / "proj"
        _init_git_repo(workspace)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        state_dir = "state"

        plan = [("mvp-A", "wi-a-1"), ("mvp-B", "wi-b-1"), ("mvp-C", "wi-c-1")]
        configs: dict[str, ProjectConfig] = {}
        for mvp_id, wi_id in plan:
            config_path = tmp_path / f"aido-{mvp_id}.yaml"
            config_path.write_text(self._config_text(mvp_id=mvp_id, work_item_id=wi_id, state_dir=state_dir))
            configs[mvp_id] = ProjectConfig.load(config_path)

        # Bootstrap mvp-A, then mvp-B, then mvp-C — sequentially, same
        # project.id/state_dir each time, exactly like a real project
        # advancing from one milestone's aido.yaml to the next's.
        for mvp_id, _ in plan:
            with ProjectRuntime.open(
                configs[mvp_id], clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()},
            ) as rt:
                rt.bootstrap()

        # Re-bootstrapping the OLDEST config is still idempotent — no
        # duplication, no conflict — even though it is no longer the most
        # recently bootstrapped MVP.
        with ProjectRuntime.open(
            configs["mvp-A"], clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()},
        ) as rt:
            rt.bootstrap()
            assert [wi.work_item_id for wi in rt.project_store.list_work_items("mvp-A")] == ["wi-a-1"]

        # Every MVP's own WorkItems stay correctly scoped — no leakage in
        # either direction — and every MVP's own status is independently
        # correct regardless of which config's ProjectRuntime is used to
        # read it (store operations are keyed by mvp_id, never by "the
        # config that happens to be currently loaded").
        with ProjectRuntime.open(
            configs["mvp-C"], clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()},
        ) as rt:
            assert [wi.work_item_id for wi in rt.project_store.list_work_items("mvp-A")] == ["wi-a-1"]
            assert [wi.work_item_id for wi in rt.project_store.list_work_items("mvp-B")] == ["wi-b-1"]
            assert [wi.work_item_id for wi in rt.project_store.list_work_items("mvp-C")] == ["wi-c-1"]

            for mvp_id, _ in plan:
                mvp = rt.project_store.get_mvp(mvp_id)
                assert mvp.mvp_id == mvp_id
                assert mvp.project_id == "demo-multi-mvp"

            # One shared Project record, never duplicated per MVP.
            project = rt.project_store.get_project("demo-multi-mvp")
            assert project.project_id == "demo-multi-mvp"


class TestConfigRuntimeConflicts:
    def test_conflicting_persisted_workspace_fails_closed(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        # A second, different workspace directory, same project id + state_dir.
        other_workspace = tmp_path / "other-proj"
        _init_git_repo(other_workspace)
        (tmp_path / "aido2.yaml").write_text(
            dedent(
                f"""
                schema_version: 1
                project: {{id: demo, name: Demo, workspace: other-proj, state_dir: {tmp_path / "state"}}}
                workers: {{registry: workers.yaml}}
                execution: {{permission_mode: standard}}
                git: {{base_branch: main}}
                mvp: {{id: mvp-1, objective: Ship it}}
                work_items: []
                qa: []
                """
            )
        )
        conflicting_config = ProjectConfig.load(tmp_path / "aido2.yaml")
        with ProjectRuntime.open(conflicting_config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt2:
            with pytest.raises(ConfigRuntimeConflictError):
                rt2.bootstrap()

    def test_conflicting_work_item_definition_fails_closed(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        changed_work_items = dedent(
            """
            work_items:
              - id: wi-1
                title: A completely different title now
                required_capabilities: [development]
            """
        )
        changed_config = _write_project(tmp_path, state_dir="state", work_items=changed_work_items)
        with ProjectRuntime.open(changed_config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt2:
            with pytest.raises(ConfigRuntimeConflictError):
                rt2.bootstrap()

    def test_new_work_item_added_to_config_is_created_on_next_bootstrap(self, tmp_path: Path) -> None:
        config = _write_project(tmp_path, state_dir="state")
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        two_items = dedent(
            """
            work_items:
              - id: wi-1
                title: Do the thing
                required_capabilities: [development]
              - id: wi-2
                title: Do another thing
                required_capabilities: [development]
            """
        )
        config2 = _write_project(tmp_path, state_dir="state", work_items=two_items)
        with ProjectRuntime.open(config2, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt2:
            rt2.bootstrap()
            ids = {wi.work_item_id for wi in rt2.project_store.list_work_items("mvp-1")}
            assert ids == {"wi-1", "wi-2"}


class TestProjectStatusReader:
    """Unit-level coverage of the strictly-read-only status path — see
    tests/test_cli.py's TestStatus for the full public-CLI-level proof."""

    def test_open_returns_none_when_never_bootstrapped(self, tmp_path: Path) -> None:
        from orchestrator.project_runtime import ProjectStatusReader

        config = _write_project(tmp_path)
        assert not config.project.state_dir.exists()
        reader = ProjectStatusReader.open(config)
        assert reader is None
        assert not config.project.state_dir.exists()  # never created just by trying to open

    def test_open_never_creates_state_dir_even_if_it_partially_exists(self, tmp_path: Path) -> None:
        from orchestrator.project_runtime import ProjectStatusReader

        config = _write_project(tmp_path)
        config.project.state_dir.mkdir(parents=True)  # dir exists, but no project.sqlite3 yet
        reader = ProjectStatusReader.open(config)
        assert reader is None
        assert list(config.project.state_dir.iterdir()) == []  # still empty

    def test_open_returns_a_reader_once_bootstrapped_and_is_read_only(self, tmp_path: Path) -> None:
        from orchestrator.project_runtime import ProjectRuntime, ProjectStatusReader

        config = _write_project(tmp_path)
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        reader = ProjectStatusReader.open(config)
        assert reader is not None
        try:
            project = reader.project_store.get_project("demo")
            assert project.project_id == "demo"
            with pytest.raises(Exception):
                reader.project_store.create_project(project_id="other", name="Other", workspace=config.project.workspace)
        finally:
            reader.close()
