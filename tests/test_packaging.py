"""Packaging contract for the ai-dev-orchestrator distribution.

Guards against regressions in WI: rename the PyPI distribution name away
from the third-party-owned "orchestrator" while keeping the importable
package name ("orchestrator") and the "aido" console script unchanged.
"""

from __future__ import annotations

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
    assert re.search(r'(?m)^version\s*=\s*"0\.1\.2"\s*$', text)


def test_no_dependency_on_third_party_orchestrator_package():
    text = _pyproject_text()
    deps_match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
    assert deps_match, "expected a top-level [project] dependencies list"
    for dep in re.findall(r'"([^"]+)"', deps_match.group(1)):
        assert not dep.strip().lower().startswith("orchestrator"), (
            f"unexpected dependency on third-party 'orchestrator' package: {dep!r}"
        )


def test_console_script_still_maps_to_orchestrator_cli():
    text = _pyproject_text()
    assert re.search(r'(?m)^aido\s*=\s*"orchestrator\.cli:main"\s*$', text)


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
    assert wheel_path.name.startswith("ai_dev_orchestrator-0.1.2-")

    with zipfile.ZipFile(wheel_path) as z:
        metadata_name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = z.read(metadata_name).decode()

        entry_points_name = next(
            n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt")
        )
        entry_points = z.read(entry_points_name).decode()

    assert "Name: ai-dev-orchestrator" in metadata
    assert "Version: 0.1.2" in metadata
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:"):
            assert not line[len("Requires-Dist:") :].strip().lower().startswith("orchestrator")

    assert "aido = orchestrator.cli:main" in entry_points


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


def test_v0_1_1_tag_unchanged():
    # Locks the historical release tag to the commit it pointed to before
    # this packaging change; this expected SHA must never be updated to
    # match a moved/re-tagged v0.1.1.
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
    assert result.stdout.strip() == "544102e3687e7d155311d72374f83527ccf45a3a"
