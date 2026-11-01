"""Packaging contract for the ai-dev-orchestrator distribution.

Guards against regressions in WI: rename the PyPI distribution name away
from the third-party-owned "orchestrator" while keeping the importable
package name ("orchestrator") unchanged.

Since the engine/library boundary (P13.5) and AIDO Code's own product
cutover (that project's M1.4/M8), this distribution no longer installs
the "aido" console script itself — AIDO Code is now its sole owner (see
``README.md``/``ROADMAP.md``, "engine/library boundary"). The legacy
``orchestrator.cli`` module remains importable (existing tests still
call ``cli.main()`` directly), it is simply never installed as a
console script by a normal ``pip install ai-dev-orchestrator``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal, regex-based reads of pyproject.toml rather than tomllib: this
# repo supports Python 3.10, where tomllib doesn't exist yet (3.11+ only)
# and adding a tomli backport dependency just for these checks isn't
# worth it for the handful of scalar fields below.


def _pyproject_text() -> str:
    return (REPO_ROOT / "pyproject.toml").read_text()


def test_distribution_name_and_version():
    text = _pyproject_text()
    assert re.search(r'(?m)^name\s*=\s*"ai-dev-orchestrator"\s*$', text)
    assert re.search(r'(?m)^version\s*=\s*"0\.2\.2"\s*$', text)


def test_no_dependency_on_third_party_orchestrator_package():
    text = _pyproject_text()
    deps_match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
    assert deps_match, "expected a top-level [project] dependencies list"
    for dep in re.findall(r'"([^"]+)"', deps_match.group(1)):
        assert not dep.strip().lower().startswith("orchestrator"), (
            f"unexpected dependency on third-party 'orchestrator' package: {dep!r}"
        )


def test_no_product_console_script_declared():
    """AIDO Code, not this engine, owns the ``aido`` console script — a
    normal ``pip install ai-dev-orchestrator`` must never re-claim it."""
    text = _pyproject_text()
    assert "[project.scripts]" not in text
    assert not re.search(r'(?m)^aido\s*=', text)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("dist")
    # No --no-build-isolation: the CI environment isn't guaranteed to have
    # "wheel" preinstalled, so let pip create its own isolated build env
    # per build-system.requires (setuptools), same as a real `pip install`.
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO_ROOT), "--no-deps", "-w", str(out_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def test_wheel_builds_with_correct_metadata(built_wheel):
    wheel_path = built_wheel
    assert wheel_path.name.startswith("ai_dev_orchestrator-0.2.2-")

    with zipfile.ZipFile(wheel_path) as z:
        metadata_name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = z.read(metadata_name).decode()

        # No console script is declared any more (AIDO Code owns "aido") —
        # the wheel may omit entry_points.txt entirely, or ship an empty
        # one; either way "aido" itself must never appear in it.
        entry_points_names = [n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt")]
        entry_points = z.read(entry_points_names[0]).decode() if entry_points_names else ""

    assert "Name: ai-dev-orchestrator" in metadata
    assert "Version: 0.2.2" in metadata
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:"):
            assert not line[len("Requires-Dist:") :].strip().lower().startswith("orchestrator")

    assert "aido" not in entry_points


def test_wheel_import_path_unchanged(built_wheel, tmp_path):
    wheel_path = built_wheel
    install_dir = tmp_path / "installed"
    install_dir.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            str(wheel_path),
            "--no-deps",
            "--target",
            str(install_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from orchestrator.engine import OrchestratorEngine; print('OK')",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(install_dir), "PATH": "/usr/bin:/bin"},
    )
    assert result.stdout.strip() == "OK"


@pytest.mark.parametrize("backend", ["gravity", "vibe"])
@pytest.mark.parametrize("permission_mode", [None, "standard", "unrestricted"])
def test_wheel_bridges_run_without_execute_permission(built_wheel, tmp_path, backend, permission_mode):
    """Launch the generated custom command from a wheel with 0644 bridges.

    Provider CLIs are local stubs; no real Ralph or provider is invoked.
    """
    installed = tmp_path / "site packages"
    with zipfile.ZipFile(built_wheel) as wheel:
        wheel.extractall(installed)
    for bridge in (installed / "orchestrator").glob("*_ralph_bridge.py"):
        bridge.chmod(0o644)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    provider = binaries / ("agy" if backend == "gravity" else "vibe")
    provider.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'model': os.getenv('VIBE_ACTIVE_MODEL'), 'cwd': os.getcwd()}))\n"
    )
    provider.chmod(0o755)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(installed), PATH=str(binaries) + os.pathsep + env["PATH"])
    env.pop("VIBE_ACTIVE_MODEL", None)
    result = subprocess.run(
        [sys.executable, "-c", """
import json, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
import yaml
from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.ralph_execution_engine import _write_runtime_config

backend, mode = sys.argv[1:]
request = SimpleNamespace(worker=SimpleNamespace(backend=backend), model='test-model',
    reasoning_effort=None, instructions='Corriger le café.\\nLOOP_COMPLETE', timeout_seconds=30)
mode = ExecutionPermissionMode(mode) if mode else None
config_path, hats_path, prompt_path = _write_runtime_config(Path.cwd(), request, mode)
cli = yaml.safe_load(config_path.read_text())['cli']
assert hats_path is None
run = subprocess.run([cli['command'], *cli['args'],
    'Please read and execute the task in ' + str(prompt_path)], capture_output=True, text=True)
assert run.returncode == 0, run.stderr
assert cli['command'] == sys.executable
print(run.stdout, end='')
""", backend, permission_mode or ""],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    prompt = "Corriger le café.\nLOOP_COMPLETE"
    if backend == "gravity":
        expected = ["-p", prompt, "--mode=accept-edits"]
        if permission_mode == "unrestricted":
            expected += ["--dangerously-skip-permissions"]
        expected += ["--model", "test-model"]
        assert observed["model"] is None
    else:
        permissions = ["--agent", "ask"] if permission_mode == "standard" else ["--auto-approve"]
        expected = ["-p", prompt, "--trust", *permissions, "--output", "json"]
        assert observed["model"] == "test-model"
    assert observed["args"] == expected
    assert observed["cwd"] == str(tmp_path)


def test_v0_1_1_tag_unchanged():
    # Locks the historical release tag to its release commit. The SHA was
    # updated once, deliberately, for the 2026-10-10 commit-message history
    # rewrite (same tree ca64c4a); never update it to match any other
    # moved/re-tagged v0.1.1.
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "v0.1.1"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if verify.returncode != 0:
        pytest.skip("v0.1.1 tag not present in this checkout (shallow/tagless clone)")

    result = subprocess.run(
        ["git", "rev-list", "-n1", "v0.1.1"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.stdout.strip() == "1200321d1089b30558fb708d9359e7b5028733cc"
