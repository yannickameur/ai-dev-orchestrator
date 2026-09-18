"""Tests for ProjectConfig — public project configuration foundation (P12).

All tests are offline: real files under pytest's ``tmp_path`` (a real Git
repo for the workspace, a real worker registry YAML file), no network, no
subprocess beyond local ``git init``, no Claude/Codex/Ralph/Vibe
invocation anywhere in this file, and no developer-home assumptions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.project_config import (
    DuplicateWorkItemError,
    InvalidProjectConfigError,
    ProjectConfig,
    UnknownWorkItemDependencyError,
    UnsupportedSchemaVersionError,
    WorkItemDependencyCycleError,
)
from orchestrator.validation import ValidationKind
from orchestrator.worker_registry import WorkerRegistryError

VALID_REGISTRY = dedent(
    """
    workers:
      - worker_id: alice
        display_name: Alice
        provider: anthropic
        backend: claude_code
        capabilities: [development]
        profiles:
          standard:
            quality_tier: STANDARD
            model: sonnet
    """
)


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


def _write(path: Path, text: str) -> Path:
    path.write_text(dedent(text))
    return path


def _minimal_project(
    tmp_path: Path, *, workspace_dir: str = "proj", registry_file: str = "workers.yaml",
    extra_top: str = "", permission_mode: str = "standard",
) -> Path:
    _init_git_repo(tmp_path / workspace_dir)
    _write(tmp_path / registry_file, VALID_REGISTRY)
    config_text = f"""
    schema_version: 1
    project:
      id: demo
      name: Demo Project
      workspace: {workspace_dir}
    workers:
      registry: {registry_file}
    execution:
      permission_mode: {permission_mode}
    mvp:
      id: mvp-1
      objective: Ship the thing
      acceptance_criteria:
        - it works
    work_items:
      - id: wi-1
        title: Do the thing
        required_capabilities: [development]
    qa:
      - id: qa-1
        kind: unit_test
        argv: ["pytest", "-q"]
    {extra_top}
    """
    return _write(tmp_path / "aido.yaml", config_text)


class TestValidConfigs:
    def test_minimal_config_loads(self, tmp_path: Path) -> None:
        path = _minimal_project(tmp_path)
        config = ProjectConfig.load(path)
        assert config.schema_version == 1
        assert config.project.id == "demo"
        assert config.project.name == "Demo Project"
        assert config.project.workspace == (tmp_path / "proj").resolve()
        assert config.execution.permission_mode is ExecutionPermissionMode.STANDARD
        assert config.git.base_branch == "main"
        assert config.mvp.id == "mvp-1"
        assert len(config.work_items) == 1
        assert config.work_items[0].id == "wi-1"
        assert len(config.qa_commands) == 1
        assert config.qa_commands[0].kind is ValidationKind.UNIT_TEST
        assert config.qa_commands[0].argv == ("pytest", "-q")

    def test_complete_config_with_dependencies_and_multiple_work_items(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project:
              id: demo
              name: Demo Project
              workspace: proj
              state_dir: state
            workers:
              registry: workers.yaml
            execution:
              permission_mode: unrestricted
            git:
              base_branch: develop
            mvp:
              id: mvp-1
              objective: Ship the thing
              acceptance_criteria: [it works, it is tested]
            work_items:
              - id: wi-1
                title: First
                required_capabilities: [development]
              - id: wi-2
                title: Second
                dependencies: [wi-1]
                acceptance_criteria: [depends on wi-1]
            qa:
              - id: qa-1
                kind: unit_test
                argv: ["pytest"]
                timeout_seconds: 60
                required: true
              - id: qa-2
                kind: lint
                argv: ["ruff", "check"]
                required: false
            """,
        )
        config = ProjectConfig.load(tmp_path / "aido.yaml")
        assert config.project.state_dir == (tmp_path / "state").resolve()
        assert config.execution.permission_mode is ExecutionPermissionMode.UNRESTRICTED
        assert config.git.base_branch == "develop"
        assert [wi.id for wi in config.work_items] == ["wi-1", "wi-2"]
        assert config.work_items[1].dependencies == ("wi-1",)
        assert len(config.qa_commands) == 2
        assert config.qa_commands[1].required is False

    def test_relative_path_resolution_is_relative_to_config_file_not_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        subdir = tmp_path / "somewhere"
        subdir.mkdir()
        monkeypatch.chdir(subdir)  # cwd is NOT tmp_path
        path = _minimal_project(tmp_path)
        config = ProjectConfig.load(path)
        assert config.project.workspace == (tmp_path / "proj").resolve()
        assert config.workers_registry_path == (tmp_path / "workers.yaml").resolve()

    def test_state_dir_expansion_with_tilde(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project:
              id: demo
              name: Demo
              workspace: proj
              state_dir: "~/aido-state/demo"
            workers:
              registry: workers.yaml
            execution:
              permission_mode: standard
            mvp:
              id: mvp-1
              objective: x
            work_items: []
            qa: []
            """,
        )
        config = ProjectConfig.load(tmp_path / "aido.yaml")
        assert config.project.state_dir == fake_home / "aido-state" / "demo"

    def test_deterministic_default_state_dir_when_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        path = _minimal_project(tmp_path)
        config = ProjectConfig.load(path)
        assert config.project.state_dir == (
            fake_home / ".local" / "state" / "ai-dev-orchestrator" / "projects" / "demo"
        )

    def test_no_credentials_expected_or_stored(self, tmp_path: Path) -> None:
        path = _minimal_project(tmp_path)
        config = ProjectConfig.load(path)
        # Nothing in the public surface exposes anything credential-shaped.
        assert not hasattr(config.project, "api_key")
        assert not hasattr(config.execution, "token")


class TestInvalidConfigs:
    def test_unsupported_schema_version_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 2
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(UnsupportedSchemaVersionError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_missing_required_field_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_unknown_permission_value_fails(self, tmp_path: Path) -> None:
        path = _minimal_project(tmp_path, permission_mode="yolo")
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(path)

    def test_unknown_top_level_key_fails(self, tmp_path: Path) -> None:
        path = _minimal_project(tmp_path, extra_top="bogus_top_level: true")
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(path)

    def test_unknown_nested_key_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj, bogus_field: 1}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_duplicate_work_item_id_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items:
              - {id: wi-1, title: A}
              - {id: wi-1, title: B}
            qa: []
            """,
        )
        with pytest.raises(DuplicateWorkItemError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_unknown_dependency_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items:
              - {id: wi-1, title: A, dependencies: [wi-missing]}
            qa: []
            """,
        )
        with pytest.raises(UnknownWorkItemDependencyError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_dependency_cycle_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items:
              - {id: wi-1, title: A, dependencies: [wi-2]}
              - {id: wi-2, title: B, dependencies: [wi-1]}
            qa: []
            """,
        )
        with pytest.raises(WorkItemDependencyCycleError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_malformed_qa_command_empty_argv_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa:
              - {id: qa-1, kind: unit_test, argv: []}
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_invalid_qa_kind_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa:
              - {id: qa-1, kind: not_a_real_kind, argv: ["pytest"]}
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_missing_workspace_fails(self, tmp_path: Path) -> None:
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: does-not-exist}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_workspace_not_a_git_repository_fails(self, tmp_path: Path) -> None:
        (tmp_path / "proj").mkdir()  # real directory, but never `git init`
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_missing_worker_registry_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: does-not-exist.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_malformed_worker_registry_fails(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", "workers:\n  - worker_id: alice\n")  # missing required fields
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(WorkerRegistryError):
            ProjectConfig.load(tmp_path / "aido.yaml")

    def test_secret_like_field_rejected(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(tmp_path / "workers.yaml", VALID_REGISTRY)
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers: {registry: workers.yaml}
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            api_key: sk-not-allowed-here
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")


class TestPublicExample:
    def test_examples_aido_yaml_loads_successfully(self) -> None:
        """The tracked public example (examples/aido.yaml) must always be
        a real, currently-loadable config — not just illustrative prose
        that has silently rotted out of sync with the schema."""
        repo_root = Path(__file__).resolve().parent.parent
        config = ProjectConfig.load(repo_root / "examples" / "aido.yaml")
        assert config.execution.permission_mode is ExecutionPermissionMode.STANDARD
        assert config.workers_registry_path == (repo_root / "config" / "workers.yaml").resolve()
        registry = config.load_worker_registry()
        assert len(registry.all_workers()) >= 1


class TestWorkerRegistryReference:
    def test_load_worker_registry_returns_real_registry(self, tmp_path: Path) -> None:
        path = _minimal_project(tmp_path)
        config = ProjectConfig.load(path)
        registry = config.load_worker_registry()
        assert [w.worker_id for w in registry.all_workers()] == ["alice"]

    def test_workers_section_is_a_reference_never_inline_worker_definitions(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path / "proj")
        _write(
            tmp_path / "aido.yaml",
            """
            schema_version: 1
            project: {id: demo, name: Demo, workspace: proj}
            workers:
              - worker_id: alice
                display_name: Alice
            execution: {permission_mode: standard}
            mvp: {id: mvp-1, objective: x}
            work_items: []
            qa: []
            """,
        )
        with pytest.raises(InvalidProjectConfigError):
            ProjectConfig.load(tmp_path / "aido.yaml")
