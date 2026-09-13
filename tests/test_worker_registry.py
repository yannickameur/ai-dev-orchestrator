"""Tests for WorkerRegistry — configurable Worker Registry + Execution
Profiles (Phase 1 / Slice 15).

All tests are offline: real YAML files under pytest's ``tmp_path``, no
network, no subprocess, no Claude/Codex/Ralph invocation anywhere in this
file, and no complexity estimation / quota consumption of any kind
(that's Slices 16/17 — deliberately out of scope here).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from orchestrator.worker_registry import (
    DuplicateWorkerError,
    InvalidWorkerConfigError,
    WorkerRegistry,
)
from orchestrator.worker_selector import QualityTier, UnknownWorkerError, Worker

VALID_CONFIG = dedent(
    """
    workers:
      - worker_id: alice
        display_name: Alice
        enabled: true
        provider: anthropic
        backend: claude_code
        priority: 100
        capabilities: [development, code_review]
        default_profile_id: standard
        profiles:
          economy:
            quality_tier: SIMPLE
            model: haiku
          standard:
            quality_tier: STANDARD
            model: sonnet

      - worker_id: victor
        display_name: Victor
        enabled: false
        provider: openai
        backend: codex
        priority: 90
        capabilities: [development, code_review]
        profiles:
          standard:
            quality_tier: STANDARD
            model: gpt-5.6-terra
            reasoning_effort: medium
    """
)


def _write(tmp_path: Path, text: str, name: str = "workers.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


class TestLoadValidConfig:
    def test_loads_multiple_workers(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert {w.worker_id for w in registry.all_workers()} == {"alice", "victor"}

    def test_multiple_profiles_per_worker(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        alice = registry.get("alice")
        assert {p.profile_id for p in alice.profiles} == {"economy", "standard"}

    def test_quality_tier_parsed_from_string(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        alice = registry.get("alice")
        assert alice.profile("economy").quality_tier is QualityTier.SIMPLE
        assert alice.profile("standard").quality_tier is QualityTier.STANDARD

    def test_enabled_worker_is_exposed(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert "alice" in {w.worker_id for w in registry.enabled_workers()}

    def test_disabled_worker_is_not_active(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert "victor" not in {w.worker_id for w in registry.enabled_workers()}

    def test_disabled_worker_is_still_known(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("victor").worker_id == "victor"
        assert "victor" in {w.worker_id for w in registry.all_workers()}

    def test_get_worker_by_id(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("alice").display_name == "Alice"

    def test_unknown_worker_id_raises(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        with pytest.raises(UnknownWorkerError):
            registry.get("nope")

    def test_get_profile_convenience(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get_profile("alice", "economy").model == "haiku"

    def test_profiles_are_immutable(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        profile = registry.get("alice").profile("standard")
        with pytest.raises(AttributeError):
            profile.model = "changed"

    def test_worker_id_unique_within_a_valid_file(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert len(registry.all_workers()) == len({w.worker_id for w in registry.all_workers()})

    def test_capabilities_conserved(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("alice").capabilities == frozenset({"development", "code_review"})

    def test_priority_conserved(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("alice").priority == 100
        assert registry.get("victor").priority == 90

    def test_reasoning_effort_none_supported(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("alice").profile("standard").reasoning_effort is None

    def test_reasoning_effort_value_supported(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("victor").profile("standard").reasoning_effort == "medium"

    def test_config_readable_again_after_process_reload(self, tmp_path: Path) -> None:
        path = _write(tmp_path, VALID_CONFIG)
        first = WorkerRegistry.load(path)
        second = WorkerRegistry.load(path)  # simulates a fresh process re-reading the same file
        assert {w.worker_id for w in first.all_workers()} == {w.worker_id for w in second.all_workers()}

    def test_no_model_hardcoded_in_the_engine(self, tmp_path: Path) -> None:
        # The registry must be the only source of model names — never a
        # fallback/default baked into WorkerRegistry itself.
        import inspect

        from orchestrator import worker_registry as worker_registry_module

        source = inspect.getsource(worker_registry_module)
        for forbidden in ("sonnet", "haiku", "gpt-5", "claude-", "gemini"):
            assert forbidden not in source

    def test_default_profile_id_explicit_is_honored(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.get("alice").default_profile_id == "standard"
        assert registry.get("alice").profile().model == "sonnet"

    def test_single_profile_worker_default_is_auto_resolved(self, tmp_path: Path) -> None:
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        # victor only declares one profile and sets no explicit default.
        assert registry.get("victor").default_profile_id == "standard"


class TestWorkerRegistryDirectConstruction:
    def test_accepts_a_plain_sequence_of_workers(self) -> None:
        worker = Worker.with_single_profile(
            worker_id="w1", display_name="W", provider="anthropic", backend="claude_code", model="sonnet",
        )
        registry = WorkerRegistry([worker])
        assert registry.get("w1") is worker

    def test_duplicate_worker_id_rejected_on_direct_construction(self) -> None:
        worker = Worker.with_single_profile(
            worker_id="w1", display_name="W", provider="anthropic", backend="claude_code", model="sonnet",
        )
        with pytest.raises(DuplicateWorkerError):
            WorkerRegistry([worker, worker])


class TestFailClosedValidation:
    def test_duplicate_worker_id_in_file_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: A1
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
              - worker_id: alice
                display_name: A2
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(DuplicateWorkerError):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_duplicate_profile_id_rejected(self, tmp_path: Path) -> None:
        # YAML mappings can't literally duplicate a key, so this is
        # exercised at the Worker level via WorkerRegistry's own parsing
        # path (a worker whose 'profiles' key collides after case-folding
        # is out of scope) — direct construction covers the real
        # duplicate-profile_id invariant already enforced by Worker itself
        # and reused unchanged by the registry.
        from orchestrator.worker_selector import ExecutionProfile

        with pytest.raises(ValueError, match="duplicate profile_id"):
            Worker(
                worker_id="w1", display_name="W", provider="anthropic", backend="claude_code",
                profiles=(
                    ExecutionProfile(profile_id="p", quality_tier=QualityTier.STANDARD, model="m1"),
                    ExecutionProfile(profile_id="p", quality_tier=QualityTier.STANDARD, model="m2"),
                ),
                default_profile_id="p",
            )

    def test_worker_without_profiles_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                profiles: {}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="profile"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_missing_backend_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="backend"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_missing_provider_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="provider"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_missing_model_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="model"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_invalid_quality_tier_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: NOT_A_TIER, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="quality_tier"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_empty_capability_string_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                capabilities: [""]
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_reasoning_effort_wrong_type_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD, model: sonnet, reasoning_effort: 3}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="reasoning_effort"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_invalid_priority_type_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                priority: "high"
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="priority"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_empty_worker_id_rejected(self, tmp_path: Path) -> None:
        text = dedent(
            """
            workers:
              - worker_id: ""
                display_name: Alice
                provider: anthropic
                backend: claude_code
                profiles: {standard: {quality_tier: STANDARD, model: sonnet}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidWorkerConfigError):
            WorkerRegistry.load(_write(tmp_path, "workers: [this is not: valid: yaml: at all"))

    def test_missing_workers_key_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidWorkerConfigError):
            WorkerRegistry.load(_write(tmp_path, "not_workers: []\n"))


class TestNoSecretsInConfig:
    @pytest.mark.parametrize("bad_key", ["api_key", "apiKey", "token", "secret", "password", "credential"])
    def test_secret_like_field_is_rejected(self, tmp_path: Path, bad_key: str) -> None:
        text = dedent(
            f"""
            workers:
              - worker_id: alice
                display_name: Alice
                provider: anthropic
                backend: claude_code
                {bad_key}: "whatever"
                profiles: {{standard: {{quality_tier: STANDARD, model: sonnet}}}}
            """
        )
        with pytest.raises(InvalidWorkerConfigError, match="secret"):
            WorkerRegistry.load(_write(tmp_path, text))

    def test_valid_config_needs_no_secret_field(self, tmp_path: Path) -> None:
        # Sanity: the reference config file shipped with the project
        # declares no secret-like field anywhere.
        registry = WorkerRegistry.load(_write(tmp_path, VALID_CONFIG))
        assert registry.all_workers()  # loads fine without any credential


class TestShippedExampleConfig:
    def test_config_workers_yaml_loads(self) -> None:
        registry = WorkerRegistry.load(Path("config/workers.yaml"))
        assert {w.worker_id for w in registry.all_workers()} == {"alice", "victor"}

    def test_config_workers_yaml_workers_are_enabled(self) -> None:
        registry = WorkerRegistry.load(Path("config/workers.yaml"))
        assert len(registry.enabled_workers()) == 2

    def test_config_workers_yaml_has_estimator_profile(self) -> None:
        registry = WorkerRegistry.load(Path("config/workers.yaml"))
        for worker in registry.all_workers():
            assert worker.estimator_profile_id is not None
            worker.profile(worker.estimator_profile_id)  # must resolve without raising
