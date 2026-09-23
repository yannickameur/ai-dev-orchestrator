"""P13.3 (see ROADMAP.md): the packaged default worker registry
(`src/orchestrator/resources/default_workers.yaml`) is the canonical
source `aido init` materializes for a standalone (pip-install-only)
user. `config/workers.yaml` (this repository's own dev-loop convenience,
referenced by ../aido.example.yaml etc.) must stay byte-identical to it
— never two registries silently diverging.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from importlib.resources import files as resource_files
from pathlib import Path

from orchestrator.worker_registry import WorkerRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_registry_text() -> str:
    return resource_files("orchestrator.resources").joinpath("default_workers.yaml").read_text()


def test_matches_dev_convenience_config_workers_yaml_byte_for_byte():
    packaged = _default_registry_text()
    dev_copy = (REPO_ROOT / "config" / "workers.yaml").read_text()
    assert packaged == dev_copy


def test_parses_via_worker_registry():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_default_registry_text())
        path = Path(f.name)
    try:
        registry = WorkerRegistry.load(path)
        worker_ids = {w.worker_id for w in registry.all_workers()}
        assert worker_ids == {"alice", "bob", "victor", "oscar", "milo", "juno", "dana", "kai"}
        assert len(registry.enabled_workers()) == 6
    finally:
        path.unlink()


def test_deepseek_and_kimi_disabled_by_default():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_default_registry_text())
        path = Path(f.name)
    try:
        registry = WorkerRegistry.load(path)
        assert registry.get("dana").enabled is False
        assert registry.get("kai").enabled is False
    finally:
        path.unlink()


def test_no_secret_field_rejected_by_the_real_parser():
    # WorkerRegistry.load() already calls _reject_secrets() on every
    # field (see worker_registry.py) — test_parses_via_worker_registry
    # above already proves this passes. This test additionally rules out
    # any leaked-looking VALUE (a long random/base64/hex token), since
    # _reject_secrets() only rejects known secret-shaped *keys*, not an
    # accidentally-pasted value under an innocuous-looking key.
    import re

    text = _default_registry_text()
    assert not re.search(r"[A-Za-z0-9_\-]{32,}", text), (
        "packaged default registry must never contain a long, key/token-shaped value"
    )


def test_no_machine_specific_absolute_path():
    text = _default_registry_text()
    assert "/home/" not in text
    assert str(Path.home()) not in text


def test_victor_and_oscar_gpt6_model_mapping():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_default_registry_text())
        path = Path(f.name)
    try:
        registry = WorkerRegistry.load(path)
        for worker_id in ("victor", "oscar"):
            worker = registry.get(worker_id)
            profiles = {p.profile_id: p.model for p in worker.profiles}
            assert profiles["economy"] == "gpt-6-luna"
            assert profiles["standard"] == "gpt-6-sol"
            assert profiles["deep"] == "gpt-6-astra"
    finally:
        path.unlink()


def test_default_registry_resource_is_actually_included_in_the_built_wheel(tmp_path: Path):
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO_ROOT), "--no-deps", "-w", str(out_dir)],
        check=True, capture_output=True, text=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
        assert "orchestrator/resources/default_workers.yaml" in names
        packaged_in_wheel = z.read("orchestrator/resources/default_workers.yaml").decode()
    assert packaged_in_wheel == _default_registry_text()
