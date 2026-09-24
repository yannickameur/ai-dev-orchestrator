"""Offline project bootstrap: real local Git, no runtime/provider execution."""
import subprocess
from pathlib import Path

import pytest

from orchestrator import cli, project_runtime
from orchestrator.project_config import ProjectConfig


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Bootstrap Test", "GIT_COMMITTER_NAME": "Bootstrap Test",
        "GIT_AUTHOR_EMAIL": "bootstrap@example.invalid",
        "GIT_COMMITTER_EMAIL": "bootstrap@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("init must not resolve providers, construct runtime, or launch workers")

    monkeypatch.setattr(project_runtime, "resolve_provider_adapters", forbidden)
    monkeypatch.setattr(cli.ProjectRuntime, "open", forbidden)
    monkeypatch.setattr(cli.OrchestratorEngine, "__init__", forbidden)
    monkeypatch.setattr(project_runtime.RalphExecutionEngine, "__init__", forbidden)


def git(project, *args):
    return subprocess.run(["git", *args], cwd=project, check=True,
                          capture_output=True, text=True).stdout.strip()


def assert_scaffold(project):
    assert {"README.md", "ROADMAP.md", "aido.yaml", ".gitignore"} <= {
        p.name for p in project.iterdir()
    }
    assert (project / "README.md").read_text().startswith(f"# {project.name}\n")
    assert (project / "ROADMAP.md").read_text().startswith(f"# ROADMAP — {project.name}\n")
    assert 'workspace: "."' in (project / "aido.yaml").read_text()
    assert '/.ralph/' in (project / '.gitignore').read_text()


@pytest.mark.parametrize("existing_empty", [False, True])
def test_bootstrap_real_git(tmp_path, capsys, existing_empty):
    project = tmp_path / "roadmaplab"
    if existing_empty:
        project.mkdir()
    assert cli.main(["init", str(tmp_path), "roadmaplab"]) == 0
    assert_scaffold(project)
    assert (project / ".git").is_dir()
    assert git(project, "branch", "--show-current") == "main"
    assert git(project, "log", "-1", "--format=%s") == "Initialize AIDO project"
    assert git(project, "rev-list", "--count", "HEAD") == "1"
    assert git(project, "status", "--porcelain") == ""
    assert set(git(project, "ls-files").splitlines()) == {
        "README.md", "ROADMAP.md", "aido.yaml", ".gitignore"
    }
    assert git(project, "remote") == ""
    config = ProjectConfig.load(project / "aido.yaml")
    assert config.project.workspace == project.resolve()
    assert config.project.id == "roadmaplab"
    assert config.project.name == "roadmaplab"
    assert not config.project.state_dir.exists()
    assert config.workers_registry_path == cli._user_worker_registry_path().resolve()
    assert cli.main(["validate", str(project / "aido.yaml")]) == 0
    out = capsys.readouterr().out
    for text in ("Git: initialized on branch main", "Initial commit: created", "Edit README.md",
                 "Edit ROADMAP.md", "Edit aido.yaml", "aido validate", "aido run",
                 'git commit -m "Define initial project"', "No development has been started."):
        assert text in out


def test_nonempty_target_is_untouched(tmp_path, capsys):
    project = tmp_path / "roadmaplab"
    project.mkdir()
    (project / "keep").write_bytes(b"unchanged\x00")
    assert cli.main(["init", str(tmp_path), "roadmaplab"]) == 1
    assert list(project.iterdir()) == [project / "keep"]
    assert (project / "keep").read_bytes() == b"unchanged\x00"
    assert not cli._user_worker_registry_path().exists()
    err = capsys.readouterr().err
    assert "already exists and is not empty" in err
    assert "No file was modified" in err


def assert_recovery(out, project):
    assert str(project) in out
    for command in ('git init -b main', 'git add .', 'git commit -m "Initialize AIDO project"'):
        assert command in out
    assert "aido validate" in out
    assert "No development has been started." in out


def test_git_absent_keeps_scaffold(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["init", ".", "roadmaplab"]) == 1
    project = tmp_path / "roadmaplab"
    assert_scaffold(project)
    assert not (project / ".git").exists()
    out = capsys.readouterr().out
    assert "WARNING: Git is not installed or is not available in PATH." in out
    assert "AIDO requires a Git repository" in out
    assert_recovery(out, project)


@pytest.mark.parametrize("step", ["init", "add", "commit"])
def test_git_failure_keeps_scaffold(tmp_path, monkeypatch, capsys, step):
    original = subprocess.run
    calls = []

    def fail_step(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == step:
            raise subprocess.CalledProcessError(1, argv, stderr="Author identity unknown\nuser.email")
        return original(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fail_step)
    assert cli.main(["init", ".", "roadmaplab"]) == 1
    assert calls == ["init", "add", "commit"][:["init", "add", "commit"].index(step) + 1]
    assert_scaffold(tmp_path / "roadmaplab")
    captured = capsys.readouterr()
    assert f"Git step '{step}' failed" in captured.err
    assert "Configure your Git identity" in captured.err
    assert "Traceback" not in captured.err
    assert_recovery(captured.out, tmp_path / "roadmaplab")


@pytest.mark.parametrize("name", ["../evil", "/tmp/evil", "a/b", r"a\b", ".", "..", "", "a\nb"])
def test_rejects_path_names_before_any_write(tmp_path, capsys, name):
    parent = tmp_path / "parent"
    before = set(tmp_path.rglob("*"))
    assert cli.main(["init", str(parent), name]) == 1
    assert set(tmp_path.rglob("*")) == before
    assert "No file was modified" in capsys.readouterr().err


def test_rejects_symlink_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "roadmaplab").symlink_to(outside, target_is_directory=True)
    assert cli.main(["init", str(parent), "roadmaplab"]) == 1
    assert list(outside.iterdir()) == []
    assert not cli._user_worker_registry_path().exists()


def test_expands_parent_and_preserves_human_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["init", "~/projects", "Roadmap Lab"]) == 0
    project = tmp_path / "projects" / "Roadmap Lab"
    config = ProjectConfig.load(project / "aido.yaml")
    assert config.project.name == "Roadmap Lab"
    assert config.project.id == "roadmap-lab"
    assert config.project.workspace == project


@pytest.mark.parametrize("option", ["--workspace", "--project-name"])
def test_rejects_conflicting_legacy_options(tmp_path, option):
    assert cli.main(["init", ".", "roadmaplab", option, "elsewhere"]) == 1
    assert not (tmp_path / "roadmaplab").exists()


def test_reuses_user_registry(tmp_path):
    registry = cli._user_worker_registry_path()
    registry.parent.mkdir(parents=True)
    content = cli._default_worker_registry_text() + "\n# user customization\n"
    registry.write_text(content)
    assert cli.main(["init", ".", "roadmaplab"]) == 0
    assert registry.read_text() == content
    assert not (tmp_path / "roadmaplab" / "workers.yaml").exists()


def test_missing_git_guidance_reuses_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["init", ".", "roadmaplab"]) == 1
    capsys.readouterr()
    assert cli.main(["validate", str(tmp_path / "roadmaplab" / "aido.yaml")]) == 1
    captured = capsys.readouterr()
    assert "not initialized as a Git repository" in captured.err
    assert 'git commit -m "Initialize AIDO project"' in captured.out
    assert "Then retry:" in captured.out


def test_legacy_no_argument_only_writes_config(tmp_path):
    assert cli.main(["init"]) == 0
    assert (tmp_path / "aido.yaml").is_file()
    for filename in ("README.md", "ROADMAP.md", ".gitignore", ".git"):
        assert not (tmp_path / filename).exists()
