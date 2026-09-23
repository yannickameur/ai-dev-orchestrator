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


def _load_default_registry() -> WorkerRegistry:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_default_registry_text())
        path = Path(f.name)
    try:
        return WorkerRegistry.load(path)
    finally:
        path.unlink()


def test_bob_oscar_milo_display_names_updated_worker_ids_unchanged():
    """Maintainer decision, pre-M2: display names change, worker_ids
    (the stable technical identity) never do."""
    registry = _load_default_registry()
    expected = {"bob": "Lydie", "oscar": "Yannick", "milo": "Nathaniel"}
    for worker_id, display_name in expected.items():
        worker = registry.get(worker_id)
        assert worker.worker_id == worker_id
        assert worker.display_name == display_name


def test_other_workers_display_names_unaffected():
    registry = _load_default_registry()
    unaffected = {"alice": "Alice", "victor": "Victor", "juno": "Juno", "dana": "Dana", "kai": "Kai"}
    for worker_id, display_name in unaffected.items():
        assert registry.get(worker_id).display_name == display_name


def test_renamed_workers_git_identity_uses_new_display_name():
    """P13.2's worker Git identity (author/committer) is derived from
    display_name + worker_id — never a provider/vendor name, and the
    stable technical email must never change just because the human
    label did."""
    from orchestrator.ralph_execution_engine import _worker_git_identity_env

    registry = _load_default_registry()
    expected = {
        "bob": ("Lydie", "bob@workers.ai-dev-orchestrator.local"),
        "oscar": ("Yannick", "oscar@workers.ai-dev-orchestrator.local"),
        "milo": ("Nathaniel", "milo@workers.ai-dev-orchestrator.local"),
    }
    for worker_id, (name, email) in expected.items():
        env = _worker_git_identity_env(registry.get(worker_id))
        assert env["GIT_AUTHOR_NAME"] == name
        assert env["GIT_COMMITTER_NAME"] == name
        assert env["GIT_AUTHOR_EMAIL"] == email
        assert env["GIT_COMMITTER_EMAIL"] == email


def test_oscar_worker_never_uses_the_maintainer_email():
    """Distinguishes the "Yannick" worker from the human maintainer
    Yannick Ameur: the worker's Git identity must stay the stable,
    clearly-non-human technical email, never yannick.ameur@gmail.com."""
    from orchestrator.ralph_execution_engine import _worker_git_identity_env

    registry = _load_default_registry()
    env = _worker_git_identity_env(registry.get("oscar"))
    assert env["GIT_AUTHOR_EMAIL"] == "oscar@workers.ai-dev-orchestrator.local"
    assert env["GIT_COMMITTER_EMAIL"] == "oscar@workers.ai-dev-orchestrator.local"
    assert "yannick.ameur@gmail.com" not in env["GIT_AUTHOR_EMAIL"]
    assert "yannick.ameur@gmail.com" not in env["GIT_COMMITTER_EMAIL"]
    assert "gmail.com" not in env["GIT_AUTHOR_EMAIL"]
