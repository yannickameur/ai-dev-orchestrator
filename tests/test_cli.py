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


def _snapshot_tree(root: Path) -> dict[str, tuple[bytes, int, int]]:
    """Recursive relative file list + bytes + size + mtime_ns for every
    regular file under ``root`` — the whole-state-tree contract §7 needs,
    not just one database."""
    if not root.exists():
        return {}
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            snapshot[str(path.relative_to(root))] = (path.read_bytes(), stat.st_size, stat.st_mtime_ns)
    return snapshot


class TestStatus:
    def test_uninitialized_project_reports_not_initialized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["status", str(config_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "NOT_INITIALIZED" in out

    def test_uninitialized_status_does_not_create_state_dir(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        assert not config.project.state_dir.exists()

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})

        assert exit_code == 0
        assert not config.project.state_dir.exists()  # still does not exist at all

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

    def test_status_never_instantiates_any_provider_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Structural proof, not just behavioral: patch every real adapter
        class's ``__init__`` to raise if ever constructed at all."""
        from orchestrator.providers import claude_code_adapter, codex_adapter, mistral_vibe_adapter

        def _boom(self, *a, **k):
            raise AssertionError("no provider adapter may ever be instantiated by aido status")

        for module, cls_name in (
            (claude_code_adapter, "ClaudeCodeAdapter"),
            (codex_adapter, "CodexAdapter"),
            (mistral_vibe_adapter, "MistralVibeAdapter"),
        ):
            monkeypatch.setattr(getattr(module, cls_name), "__init__", _boom)

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        # Bootstrap via explicit fakes (never the real adapters patched above).
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        exit_code = _invoke(["status", str(config_path)])
        assert exit_code == 0  # would have raised via the patched __init__ if status ever built a real adapter

    def test_status_never_instantiates_ralph_execution_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Structural proof: patch RalphExecutionEngine.__init__ itself to
        raise if ever constructed — status must never even build one,
        let alone launch a subprocess through it."""
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        # Bootstrap FIRST, with the real (unpatched) engine — only `aido
        # run`'s own composition legitimately builds one.
        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        from orchestrator import ralph_execution_engine as ree_module

        def _boom(self, *a, **k):
            raise AssertionError("RalphExecutionEngine must never be constructed by aido status")

        monkeypatch.setattr(ree_module.RalphExecutionEngine, "__init__", _boom)

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})
        assert exit_code == 0  # would have raised via the patched __init__ if status ever built one

    def test_whole_state_tree_byte_identical_before_and_after(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        before = _snapshot_tree(config.project.state_dir)
        assert before  # sanity: something real was actually created by bootstrap

        _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})

        after = _snapshot_tree(config.project.state_dir)
        assert before == after

    def test_missing_unrelated_store_db_is_not_created_by_status(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        validation_db = config.project.state_dir / "validation_qa.sqlite3"
        assert validation_db.is_file()
        validation_db.unlink()
        assert not validation_db.exists()

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})

        assert exit_code == 0
        assert not validation_db.exists()  # status never recreates it

    def test_legacy_pre_p12_execution_db_is_readable_without_migration(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        import sqlite3

        config_path = _write_config(tmp_path)
        config = ProjectConfig.load(config_path)
        from orchestrator.project_runtime import ProjectRuntime

        with ProjectRuntime.open(config, clock=lambda: UTC_T0, provider_adapters={"anthropic": _FakeAdapter()}) as rt:
            rt.bootstrap()

        # Overwrite the (empty, freshly-created) executions DB with a
        # hand-built pre-P12 schema — no permission_mode column at all —
        # populated with one real historical row.
        execution_db = config.project.state_dir / "executions.sqlite3"
        execution_db.unlink()
        legacy_conn = sqlite3.connect(str(execution_db))
        legacy_conn.execute(
            """
            CREATE TABLE executions (
                execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                provider TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
                reasoning_effort TEXT, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                exit_code INTEGER, provider_session_id TEXT, ralph_loop_id TEXT,
                git_sha_before TEXT, git_sha_after TEXT
            )
            """
        )
        legacy_conn.execute(
            "INSERT INTO executions "
            "(execution_id, task_id, worker_id, provider, backend, model, role, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-exec", "wi-1", "alice", "anthropic", "claude_code", "sonnet", "developer",
             UTC_T0.isoformat(), "succeeded"),
        )
        legacy_conn.commit()
        legacy_conn.close()

        before_bytes = execution_db.read_bytes()
        before_schema = _table_columns(execution_db, "executions")

        exit_code = _invoke(["status", str(config_path)], provider_adapters={"anthropic": _NeverCalledAdapter()})
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "permission_mode=unknown" in out  # never fabricated as standard/unrestricted

        after_bytes = execution_db.read_bytes()
        after_schema = _table_columns(execution_db, "executions")
        assert before_bytes == after_bytes  # byte-identical: no migration ever ran
        assert before_schema == after_schema
        assert "permission_mode" not in after_schema  # schema genuinely never gained the column


def _table_columns(db_path: Path, table: str) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


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


class _QuotaFakeAdapter(ProviderAdapter):
    """Fully controllable fake for `aido status --probe` rendering tests —
    unlike `_FakeAdapter`, lets a test specify exact QuotaWindow/
    ResetCredit values (including deliberately unknown ones)."""

    def __init__(self, *, provider: str, quota_windows=(), reset_credits=(), available: bool = True) -> None:
        self.probe_calls = 0
        self._provider = provider
        self._quota_windows = quota_windows
        self._reset_credits = reset_credits
        self._available = available

    async def probe(self) -> ProviderState:
        self.probe_calls += 1
        return ProviderState(
            provider=self._provider,
            availability=ProviderAvailability(available=self._available, observed_at=UTC_T0),
            observed_at=UTC_T0, quota_windows=self._quota_windows, reset_credits=self._reset_credits,
        )


class TestStatusWorkers:
    """P13.3 (see ROADMAP.md): `aido status` (no --probe) shows every
    configured worker with its display name/model, still zero provider
    calls."""

    def test_workers_section_shows_display_names_and_models(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["status", str(config_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Workers:" in out
        assert "alice — Alice" in out
        assert "bob — Bob" in out
        assert "model=sonnet" in out
        assert "provider=anthropic" in out
        assert "backend=claude_code" in out

    def test_no_probe_line_without_probe_flag(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        _invoke(["status", str(config_path)])
        out = capsys.readouterr().out
        assert "probe=" not in out
        assert "Provider quotas:" not in out

    def test_workers_shown_even_when_not_initialized(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        config_path = _write_config(tmp_path)
        exit_code = _invoke(["status", str(config_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "NOT_INITIALIZED" in out
        assert "Workers:" in out  # still shown, even before any aido run

    def test_status_without_probe_never_instantiates_any_provider_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestrator.providers import claude_code_adapter, codex_adapter, mistral_vibe_adapter

        def _boom(self, *a, **k):
            raise AssertionError("no provider adapter may ever be instantiated by aido status (no --probe)")

        for module, cls_name in (
            (claude_code_adapter, "ClaudeCodeAdapter"),
            (codex_adapter, "CodexAdapter"),
            (mistral_vibe_adapter, "MistralVibeAdapter"),
        ):
            monkeypatch.setattr(getattr(module, cls_name), "__init__", _boom)

        config_path = _write_config(tmp_path)
        exit_code = _invoke(["status", str(config_path)])
        assert exit_code == 0


class TestStatusProbe:
    """P13.3: `aido status --probe` performs one real, explicit probe and
    renders utilization/remaining/reset/reset-credits, provider-grouped,
    never per-worker duplicated, unknown staying unknown."""

    def test_probe_renders_utilization_remaining_and_reset(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        from orchestrator.providers.contracts import QuotaWindow

        config_path = _write_config(tmp_path)
        adapter = _QuotaFakeAdapter(
            provider="anthropic",
            quota_windows=(
                QuotaWindow(
                    window_type="five_hour", source="fake", observed_at=UTC_T0,
                    utilization=0.37, reset_at=UTC_T1,
                ),
            ),
        )
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert adapter.probe_calls == 1
        assert "Provider quotas:" in out
        assert "anthropic:" in out
        assert "five_hour:" in out
        assert "used: 37%" in out
        assert "remaining: 63%" in out
        assert UTC_T1.isoformat() in out

    def test_probe_unknown_utilization_stays_unknown_never_100_percent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        from orchestrator.providers.contracts import QuotaWindow

        config_path = _write_config(tmp_path)
        adapter = _QuotaFakeAdapter(
            provider="anthropic",
            quota_windows=(
                QuotaWindow(window_type="five_hour", source="fake", observed_at=UTC_T0),  # utilization/reset unknown
            ),
        )
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "used: unknown" in out
        assert "remaining: unknown" in out
        assert "reset: unknown" in out
        assert "used: 100%" not in out
        assert "remaining: 100%" not in out

    def test_probe_no_quota_windows_at_all_reports_unknown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = _write_config(tmp_path)
        adapter = _QuotaFakeAdapter(provider="anthropic")  # no quota_windows: e.g. Vibe/Mistral shape
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "anthropic:" in out
        assert "quota: unknown" in out

    def test_probe_reset_credits_rendered_never_consumed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        from orchestrator.providers.contracts import ResetCredit, ResetCreditStatus

        config_path = _write_config(tmp_path)
        adapter = _QuotaFakeAdapter(
            provider="anthropic",
            reset_credits=(ResetCredit(title="Full reset", status=ResetCreditStatus.AVAILABLE, available_count=3),),
        )
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "reset credits:" in out
        assert "Full reset: available (available: 3)" in out
        assert adapter.probe_calls == 1  # probed (described), never a second call that would imply consumption

    def test_shared_provider_quota_rendered_once_not_per_worker(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        # REGISTRY_TWO_WORKERS (alice + bob) both use "anthropic" —
        # exactly the shared-provider case.
        from orchestrator.providers.contracts import QuotaWindow

        config_path = _write_config(tmp_path)
        adapter = _QuotaFakeAdapter(
            provider="anthropic",
            quota_windows=(
                QuotaWindow(window_type="five_hour", source="fake", observed_at=UTC_T0, utilization=0.5),
            ),
        )
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert out.count("anthropic:") == 1  # one provider quota block, not two (per alice/bob)
        assert adapter.probe_calls == 1  # probed once, not once per worker
        # Both workers still each show their own probe=available line.
        assert out.count("probe=available") == 2

    def test_disabled_worker_shows_probe_disabled_not_unknown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        config_path = tmp_path / "aido.yaml"
        _init_git_repo_with_pytest_marker(tmp_path / "proj")
        (tmp_path / "workers.yaml").write_text(
            dedent(
                """
                workers:
                  - worker_id: dana
                    display_name: Dana
                    provider: deepseek
                    backend: claude_code
                    enabled: false
                    capabilities: [development]
                    profiles:
                      standard: {quality_tier: STANDARD, model: deepseek-flash}
                  - worker_id: alice
                    display_name: Alice
                    provider: anthropic
                    backend: claude_code
                    capabilities: [development]
                    profiles:
                      standard: {quality_tier: STANDARD, model: sonnet}
                """
            )
        )
        config_path.write_text(
            dedent(
                f"""
                schema_version: 1
                project:
                  id: demo
                  name: Demo
                  workspace: proj
                  state_dir: state
                workers:
                  registry: workers.yaml
                execution:
                  permission_mode: standard
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
        adapter = _QuotaFakeAdapter(provider="anthropic")
        exit_code = _invoke(["status", str(config_path), "--probe"], provider_adapters={"anthropic": adapter})
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "probe=disabled" in out
        # deepseek was never probed at all (dana is disabled).
        assert "deepseek:" not in out


class TestStandaloneWorkerRegistry:
    """P13.3 (see ROADMAP.md): `aido init` works with no
    `--workers-registry` and no ai-dev-orchestrator source checkout in
    sight — the packaged default registry template is materialized into
    a real, editable user config instead."""

    def test_init_without_registry_or_cwd_config_creates_user_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "proj"
        _init_git_repo_with_pytest_marker(workspace)
        monkeypatch.chdir(workspace)  # no config/workers.yaml here
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

        config_path = tmp_path / "aido.yaml"
        exit_code = _invoke(["init", str(config_path), "--workspace", str(workspace)])
        assert exit_code == 0

        user_registry = tmp_path / "xdg-config" / "ai-dev-orchestrator" / "workers.yaml"
        assert user_registry.is_file()
        from orchestrator.worker_registry import WorkerRegistry

        registry = WorkerRegistry.load(user_registry)
        assert registry.all_workers()  # parses, non-empty

        config = ProjectConfig.load(config_path)
        assert config.load_worker_registry().all_workers()  # generated aido.yaml actually resolves it

    def test_init_never_overwrites_existing_user_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "proj"
        _init_git_repo_with_pytest_marker(workspace)
        monkeypatch.chdir(workspace)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

        user_registry = tmp_path / "xdg-config" / "ai-dev-orchestrator" / "workers.yaml"
        user_registry.parent.mkdir(parents=True)
        user_registry.write_text(REGISTRY_TWO_WORKERS)  # a pre-existing user file

        exit_code = _invoke(["init", str(tmp_path / "aido.yaml")])
        assert exit_code == 0
        assert user_registry.read_text() == REGISTRY_TWO_WORKERS  # untouched

    def test_init_still_prefers_cwd_config_workers_yaml_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Today's dev-checkout convenience (e.g. this very repository)
        is unaffected by the new standalone fallback."""
        workspace = tmp_path / "proj"
        _init_git_repo_with_pytest_marker(workspace)
        (workspace / "config").mkdir()
        (workspace / "config" / "workers.yaml").write_text(REGISTRY_TWO_WORKERS)
        monkeypatch.chdir(workspace)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

        exit_code = _invoke(["init", str(tmp_path / "aido.yaml")])
        assert exit_code == 0
        assert not (tmp_path / "xdg-config").exists()  # standalone path never even touched
