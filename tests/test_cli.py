"""Tests for the public `aido` CLI (P1).

Offline only: real Git repos/worker registries under pytest's
``tmp_path``, fake provider adapters (never real Claude/Codex/Vibe
probes), a scripted fake Ralph subprocess runner (never real Ralph). The
one real subprocess besides local ``git`` is each test's own trivial,
fast, portable QA command (e.g. ``python -c "pass"``) — deterministic QA
running a real command is the product's own design, never an LLM call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from orchestrator import cli
from orchestrator.execution_store import ExecutionStatus
from orchestrator.project_config import ProjectConfig
from orchestrator.project_state import WorkItemStatus
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.contracts import ProviderAvailability, ProviderState, UnavailabilityReason

UTC_T0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
UTC_T1 = UTC_T0 + timedelta(hours=4)


def _invoke(argv: list[str], **extra) -> int:
    """Exercises the exact same dispatch ``main()`` uses
    (``parser.parse_args`` -> ``args.func(args)``), with room to inject
    the smallest test seams ``ProjectRuntime.open`` offers
    (``provider_adapters``/``subprocess_runner``) — production ``aido``
    invocations never set these."""
    parser = cli._build_parser()
    args = parser.parse_args(argv)
    for key, value in extra.items():
        setattr(args, key, value)
    return args.func(args)


class _FakeAdapter(ProviderAdapter):
    def __init__(self, *, available: bool = True, reset_at: datetime | None = None) -> None:
        self.probe_calls = 0
        self._available = available
        self._reset_at = reset_at

    async def probe(self) -> ProviderState:
        self.probe_calls += 1
        from orchestrator.providers.contracts import QuotaWindow

        windows = ()
        if not self._available and self._reset_at is not None:
            windows = (
                QuotaWindow(
                    window_type="session", source="fake", observed_at=UTC_T0,
                    reset_at=self._reset_at, utilization=1.0,
                ),
            )
        return ProviderState(
            provider="fake",
            availability=ProviderAvailability(
                available=self._available, observed_at=UTC_T0,
                reason=None if self._available else UnavailabilityReason.QUOTA_EXHAUSTED,
            ),
            observed_at=UTC_T0, quota_windows=windows,
        )


class _NeverCalledAdapter(ProviderAdapter):
    async def probe(self) -> ProviderState:
        raise AssertionError("provider probe must never be called by this command")


class _ScriptedRalphRunner:
    """Same shape/pattern as the one already used in
    tests/integration/test_cross_worker_resume_e2e.py — genuinely mutates
    the workspace and writes real Ralph event files, so the scenario
    stays concrete, never a real Ralph/Claude/Codex/Vibe subprocess."""

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
        (ralph_dir / "current-loop-id").write_text(step.get("loop_id", "cli-e2e-loop"))
        events_filename = f"events-{len(self.calls)}.jsonl"
        (ralph_dir / "current-events").write_text(f".ralph/{events_filename}")
        line = json.dumps({"topic": step["topic"], "ts": UTC_T0.isoformat(), "payload": step.get("payload")})
        (ralph_dir / events_filename).write_text(line + "\n")
        return step.get("exit_code", 0), step.get("stdout", b""), step.get("stderr", b"")


def _init_git_repo_with_pytest_marker(path: Path) -> Path:
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
    workspace_dir: str = "proj",
) -> Path:
    _init_git_repo_with_pytest_marker(tmp_path / workspace_dir)
    (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
    config_path = tmp_path / "aido.yaml"
    config_path.write_text(
        dedent(
            f"""
            schema_version: 1
            project:
              id: demo
              name: Demo
              workspace: {workspace_dir}
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


# --- entrypoint / parsing ----------------------------------------------------


class TestEntrypoint:
    def test_pyproject_entry_point_matches_real_main(self) -> None:
        # No TOML parser dependency (this project has none, and
        # ``tomllib`` is 3.11+ only while this project supports 3.10+) —
        # a plain text check of the exact declared line is sufficient and
        # fully portable.
        repo_root = Path(__file__).resolve().parent.parent
        text = (repo_root / "pyproject.toml").read_text()
        assert 'aido = "orchestrator.cli:main"' in text
        assert callable(cli.main)

    def test_top_level_help(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == 0
        assert "init" in capsys.readouterr().out

    @pytest.mark.parametrize("command", ["init", "validate", "run", "status"])
    def test_subcommand_help(self, command: str, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main([command, "--help"])
        assert exc.value.code == 0
        assert f"aido {command}" in capsys.readouterr().out

    def test_unknown_command_is_a_clean_usage_error(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["bogus"])
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


# --- aido init ---------------------------------------------------------------


class TestInit:
    def test_creates_schema_v1_config(self, tmp_path: Path) -> None:
        _init_git_repo_with_pytest_marker(tmp_path)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        exit_code = _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        assert exit_code == 0
        config = ProjectConfig.load(config_path)
        assert config.schema_version == 1

    def test_defaults_permission_to_standard(self, tmp_path: Path) -> None:
        _init_git_repo_with_pytest_marker(tmp_path)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        from orchestrator.execution_policy import ExecutionPermissionMode

        config = ProjectConfig.load(config_path)
        assert config.execution.permission_mode is ExecutionPermissionMode.STANDARD

    def test_explicit_unrestricted_is_written_only_when_requested(self, tmp_path: Path) -> None:
        _init_git_repo_with_pytest_marker(tmp_path)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        _invoke(
            ["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml"),
             "--permission-mode", "unrestricted"],
        )
        from orchestrator.execution_policy import ExecutionPermissionMode

        config = ProjectConfig.load(config_path)
        assert config.execution.permission_mode is ExecutionPermissionMode.UNRESTRICTED

    def test_writes_relative_workspace_when_practical(self, tmp_path: Path) -> None:
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        text = config_path.read_text()
        assert 'workspace: "."' in text

    def test_references_provided_worker_registry_never_inlines(self, tmp_path: Path) -> None:
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        text = config_path.read_text()
        assert "worker_id:" not in text
        assert "registry:" in text

    def test_does_not_write_credentials(self, tmp_path: Path) -> None:
        # Explicit --project-id/--project-name so the generated file's
        # content is never derived from this test's own tmp_path name
        # (pytest names it after the test function, which could
        # coincidentally contain one of the substrings checked below).
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        _invoke(
            ["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml"),
             "--project-id", "demo", "--project-name", "Demo"],
        )
        text = config_path.read_text().lower()
        for bad in ("api_key", "token", "secret", "password", "credential"):
            assert bad not in text

    def test_refuses_overwrite(self, tmp_path: Path) -> None:
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        config_path.write_text("already here\n")
        exit_code = _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        assert exit_code != 0
        assert config_path.read_text() == "already here\n"

    def test_generated_config_loads_after_filling_workspace_and_workitem(self, tmp_path: Path) -> None:
        """The shipped template's shape is a real, loadable schema v1
        config once workspace exists as a Git repo — proving it is not
        just illustrative prose that has rotted."""
        workspace = tmp_path / "proj"
        _init_git_repo_with_pytest_marker(workspace)
        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = workspace / "aido.yaml"
        exit_code = _invoke(
            ["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")],
        )
        assert exit_code == 0
        config = ProjectConfig.load(config_path)
        assert config.project.workspace == workspace.resolve()

    def test_no_provider_calls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from orchestrator.providers import claude_code_adapter, codex_adapter, mistral_vibe_adapter

        for module, cls_name in (
            (claude_code_adapter, "ClaudeCodeAdapter"),
            (codex_adapter, "CodexAdapter"),
            (mistral_vibe_adapter, "MistralVibeAdapter"),
        ):
            monkeypatch.setattr(getattr(module, cls_name), "probe", _NeverCalledAdapter.probe)

        (tmp_path / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        config_path = tmp_path / "aido.yaml"
        exit_code = _invoke(["init", str(config_path), "--workers-registry", str(tmp_path / "workers.yaml")])
        assert exit_code == 0  # would have raised via the monkeypatched probe() if ever called


# --- aido validate -------------------------------------------------------


class TestValidate:
    def test_valid_config_exits_zero_and_prints_facts(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["validate", str(config_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "project: demo" in out
        assert "mvp: mvp-1" in out
        assert "work_items: 1" in out
        assert "qa_commands: 1" in out
        assert "enabled_workers: 2" in out
        assert "providers: anthropic" in out
        assert "base_branch: main" in out
        assert "aido validate: OK" in out

    def test_standard_permission_printed(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path, permission_mode="standard")
        _invoke(["validate", str(config_path)])
        assert "permission_mode: STANDARD" in capsys.readouterr().out

    def test_unrestricted_permission_printed(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path, permission_mode="unrestricted")
        _invoke(["validate", str(config_path)])
        assert "permission_mode: UNRESTRICTED" in capsys.readouterr().out

    def test_invalid_config_is_a_clean_failure_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = tmp_path / "aido.yaml"
        config_path.write_text("schema_version: 999\n")
        exit_code = _invoke(["validate", str(config_path)])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "invalid project configuration" in err

    def test_invalid_worker_registry_is_a_clean_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        (tmp_path / "workers.yaml").write_text("workers:\n  - worker_id: alice\n")  # missing required fields
        _init_git_repo_with_pytest_marker(tmp_path / "proj")
        config_path = tmp_path / "aido.yaml"
        config_path.write_text(
            dedent(
                """
                schema_version: 1
                project: {id: demo, name: Demo, workspace: proj, state_dir: state}
                workers: {registry: workers.yaml}
                execution: {permission_mode: standard}
                mvp: {id: mvp-1, objective: x}
                work_items: []
                qa: []
                """
            )
        )
        exit_code = _invoke(["validate", str(config_path)])
        assert exit_code == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_no_provider_calls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from orchestrator.providers import claude_code_adapter, codex_adapter, mistral_vibe_adapter

        for module, cls_name in (
            (claude_code_adapter, "ClaudeCodeAdapter"),
            (codex_adapter, "CodexAdapter"),
            (mistral_vibe_adapter, "MistralVibeAdapter"),
        ):
            monkeypatch.setattr(getattr(module, cls_name), "probe", _NeverCalledAdapter.probe)
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["validate", str(config_path)])
        assert exit_code == 0

    def test_no_ralph_execution(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)

        async def _never_called(*args, **kwargs):
            raise AssertionError("Ralph must never be launched by validate")

        exit_code = _invoke(["validate", str(config_path)])
        assert exit_code == 0  # nothing was ever wired to a subprocess runner at all


# --- aido status -----------------------------------------------------------


class TestStatus:
    def test_uninitialized_project_reports_not_initialized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["status", str(config_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "NOT_INITIALIZED" in out

    def test_initialized_project_reports_workitem_statuses(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "wi-1: ready" in out
        assert "ready: 1" in out

    def test_no_provider_calls(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})
        assert exit_code == 0  # would have raised via _NeverCalledAdapter.probe if ever called

    def test_no_state_mutation(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        db_path = config.project.state_dir / "project.sqlite3"
        before = db_path.read_bytes()
        _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})
        after = db_path.read_bytes()
        assert before == after


# --- aido run: end-to-end offline WorkItem Flow ---------------------------


def _commit_action(repo_cwd_relative_file: str, content: str, message: str):
    def _mutate(cwd: Path) -> None:
        (cwd / repo_cwd_relative_file).write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
        subprocess.run(
            ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=E2E", "commit", "-q", "-m", message],
            cwd=str(cwd), check=True,
        )
    return _mutate


class TestRunEndToEnd:
    def test_public_cli_run_drives_full_workitem_flow_to_completed(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)

        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},  # DEV B: no further change needed
            ]
        )
        exit_code = _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "wi-1 -> completed" in out
        assert len(runner.calls) == 2  # exactly DEV A + DEV B, no complexity estimation, no QA-as-Ralph-call

        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T1, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            wi = rt.project_store.get_work_item("wi-1")
            assert wi.status is WorkItemStatus.COMPLETED
            executions = rt.execution_store.list_for_task("wi-1")
            assert len(executions) == 2
            assert executions[0].worker_id != executions[1].worker_id  # DEV_B != DEV_A
            assert all(e.status is ExecutionStatus.SUCCEEDED for e in executions)
            assert all(e.permission_mode == "standard" for e in executions)  # P12 audit

    def test_rerun_after_completion_does_not_duplicate_or_rerun(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)

        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )

        # A brand-new "process" (fresh runtime), same config, same
        # persisted state_dir — the public run==resume path again.
        second_runner = _ScriptedRalphRunner([])  # must NOT be called at all
        exit_code = _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=second_runner,
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "nothing currently eligible/due" in out
        assert len(second_runner.calls) == 0

        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T1, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            assert len(rt.project_store.list_work_items("mvp-1")) == 1
            assert len(rt.execution_store.list_for_task("wi-1")) == 2  # never duplicated

    def test_unrestricted_permission_mode_reaches_execution_record(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, permission_mode="unrestricted")
        config = ProjectConfig.load(config_path)
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T1, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            executions = rt.execution_store.list_for_task("wi-1")
            assert all(e.permission_mode == "unrestricted" for e in executions)

    def test_unrestricted_prints_warning(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path, permission_mode="unrestricted")
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        out = capsys.readouterr().out
        assert "WARNING: project requests UNRESTRICTED worker execution permissions." in out

    def test_standard_prints_no_warning(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path, permission_mode="standard")
        runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A")},
                {"topic": "work.completed"},
            ]
        )
        _invoke(
            ["run", str(config_path)],
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=runner,
        )
        out = capsys.readouterr().out
        assert "WARNING" not in out


class TestRunWaitingAndResume:
    def test_provider_unavailable_then_available_resumes_via_new_execution(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        reset_at = UTC_T0 + timedelta(minutes=30)

        # First "process": provider unavailable with a real reset_at-shaped
        # fake state -> WorkItem WAITING. Registry only has anthropic
        # workers, so an unavailable anthropic makes nothing eligible.
        first_runner = _ScriptedRalphRunner([])  # must not be called: no eligible worker at all
        exit_code = _invoke(
            ["run", str(config_path)],
            clock=lambda: UTC_T0,
            provider_adapters={"anthropic": _FakeAdapter(available=False, reset_at=reset_at)},
            subprocess_runner=first_runner,
        )
        out = capsys.readouterr().out
        assert exit_code == 0  # WAITING is not a CLI failure
        assert "wi-1 -> waiting" in out
        assert len(first_runner.calls) == 0

        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            assert rt.project_store.get_work_item("wi-1").status is WorkItemStatus.WAITING

        # Second "process": provider now available, same state_dir — the
        # public run path resumes through a NEW execution, never reusing
        # anything from the first attempt (no conversational memory).
        second_runner = _ScriptedRalphRunner(
            [
                {"topic": "work.completed", "mutate": _commit_action("feature.py", "x = 1\n", "DEV A (resumed)")},
                {"topic": "work.completed"},
            ]
        )
        exit_code = _invoke(
            ["run", str(config_path)],
            clock=lambda: UTC_T1,
            provider_adapters={"anthropic": _FakeAdapter(available=True)},
            subprocess_runner=second_runner,
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "wi-1 -> completed" in out
        assert len(second_runner.calls) == 2

        with ProjectRuntime.open(config, clock=lambda: UTC_T1, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            wi = rt.project_store.get_work_item("wi-1")
            assert wi.status is WorkItemStatus.COMPLETED
