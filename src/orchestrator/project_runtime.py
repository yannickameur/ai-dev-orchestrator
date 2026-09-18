"""project_runtime — composes a loaded ``ProjectConfig`` into the real
stores/services/``MVPManager`` WorkItem Flow needs (P1).

Centralizes exactly the wiring previously done by hand in
``scripts/run_external_project_pilot.py`` (evidence this shape works, not
a second architecture) — so the public ``aido`` CLI, and any future
caller, never repeats it.

This is a COMPOSITION layer only, never a second orchestrator:

- ``MVPManager`` remains the WorkItem orchestration owner.
- ``WorkerSelector`` remains the worker-selection owner.
- ``RalphExecutionEngine`` remains the fine-grained execution owner.
- ``GitGovernanceService`` remains the Git owner.
- ``InternalQAEngine`` remains the deterministic QA owner.

``ProjectRuntime`` only builds real instances of each from a
``ProjectConfig`` and hands them to a real ``MVPManager`` — it contains no
WorkItem-scheduling logic of its own (``MVPManager.run_next_work_item``
stays authoritative).

Provider composition (§7): the provider adapter for each provider actually
required by the configured, *enabled* workers is resolved from a small,
explicit table (``anthropic`` -> ``ClaudeCodeAdapter``, ``openai`` ->
``CodexAdapter``, ``mistral`` -> ``MistralVibeAdapter``) — never generic
reflection/plugin discovery. An enabled worker on a provider with no known
adapter fails composition clearly, before any execution, rather than being
silently dropped. Provider expansion is P3's job, not P1's.

Execution permission policy (P12, §8): ``RalphExecutionEngine`` is always
constructed with ``permission_mode=config.execution.permission_mode`` —
never omitted for this path, so every execution launched through this
runtime is explicitly ``standard`` or ``unrestricted``, never inherited
from host-global CLI configuration.

Bootstrap (§11): ``ProjectRuntime.bootstrap()`` is the idempotent
``aido.yaml`` -> ``ProjectStateStore`` initialization — creates the
Project/MVP/WorkItems on first use, and on every subsequent use verifies
the persisted identity is still compatible with the current config,
failing closed (``ConfigRuntimeConflictError``) rather than silently
mutating history if it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from orchestrator.execution_store import ExecutionStore
from orchestrator.git_governance import GitGovernancePolicy, GitGovernanceService, GitWorkItemStore
from orchestrator.handoff import HandoffStore
from orchestrator.internal_qa_engine import InternalQAEngine
from orchestrator.mvp_manager import MVPManager
from orchestrator.project_config import ProjectConfig
from orchestrator.project_state import (
    ProjectStateStore,
    UnknownMVPError,
    UnknownProjectError,
)
from orchestrator.providers.adapter import ProviderAdapter
from orchestrator.providers.claude_code_adapter import ClaudeCodeAdapter
from orchestrator.providers.codex_adapter import CodexAdapter
from orchestrator.providers.mistral_vibe_adapter import MistralVibeAdapter
from orchestrator.qa import QAPolicy, QARunStore
from orchestrator.quota_manager import QuotaManager, QuotaPolicy
from orchestrator.ralph_execution_engine import RalphExecutionEngine
from orchestrator.validation import QualityGateRunner, ValidationStore
from orchestrator.wait import WaitStore
from orchestrator.worker_selector import WorkerSelector

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Current known provider mapping (§7) — deliberately small and explicit,
# never generic reflection/plugin discovery. New providers are P3's job.
_PROVIDER_ADAPTER_FACTORIES: dict[str, Callable[[], ProviderAdapter]] = {
    "anthropic": ClaudeCodeAdapter,
    "openai": CodexAdapter,
    "mistral": MistralVibeAdapter,
}

# Deterministic, stable filenames under ProjectConfig.project.state_dir —
# same naming already used by real pilot scripts, where reasonable.
STORE_FILENAMES = {
    "project": "project.sqlite3",
    "handoffs": "handoffs.sqlite3",
    "executions": "executions.sqlite3",
    "waits": "waits.sqlite3",
    "validation": "validation_qa.sqlite3",
    "qa_runs": "qa_runs.sqlite3",
    "git_governance": "git_governance.sqlite3",
}


class ProjectRuntimeError(Exception):
    """Base for ProjectRuntime domain errors."""


class UnsupportedProviderError(ProjectRuntimeError):
    """Raised when an enabled worker's provider has no known adapter."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"no provider adapter is known for {provider!r} (required by an "
            f"enabled worker) — currently supported providers: "
            f"{sorted(_PROVIDER_ADAPTER_FACTORIES)!r}. Adding a new provider "
            "adapter is future work (ROADMAP.md, P3), not part of P1."
        )
        self.provider = provider


class ConfigRuntimeConflictError(ProjectRuntimeError):
    """Raised when persisted runtime state conflicts with the current
    aido.yaml — P1 fails closed rather than silently mutating a
    historical Project/MVP/WorkItem definition."""


def _resolve_provider_adapters(providers: set[str]) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for provider in sorted(providers):
        factory = _PROVIDER_ADAPTER_FACTORIES.get(provider)
        if factory is None:
            raise UnsupportedProviderError(provider)
        adapters[provider] = factory()
    return adapters


@dataclass
class ProjectRuntime:
    """A composed, ready-to-use runtime for one ``ProjectConfig``."""

    config: ProjectConfig
    project_store: ProjectStateStore
    handoff_store: HandoffStore
    execution_store: ExecutionStore
    wait_store: WaitStore
    validation_store: ValidationStore
    qa_run_store: QARunStore
    git_store: GitWorkItemStore
    manager: MVPManager

    @classmethod
    def open(
        cls, config: ProjectConfig, *, clock: Clock | None = None,
        provider_adapters: dict[str, ProviderAdapter] | None = None,
        subprocess_runner: object | None = None,
    ) -> "ProjectRuntime":
        """Builds every store/service/``MVPManager`` for ``config``. Does
        NOT bootstrap Project/MVP/WorkItems — call ``.bootstrap()``
        explicitly (kept separate so a read-only caller, e.g. ``aido
        status``, can open a runtime without ever creating rows).

        ``provider_adapters``/``subprocess_runner`` are the smallest test
        injection seams this layer offers (§22) — production callers (the
        CLI) never pass them, so real provider adapters and the real
        ``ralph`` subprocess boundary are used. Tests pass fakes here to
        exercise the full public composition path offline, never a real
        Claude/Codex/Vibe/Ralph invocation."""
        clock = clock or _utcnow
        state_dir = config.project.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)

        project_store = ProjectStateStore(state_dir / STORE_FILENAMES["project"], clock=clock)
        handoff_store = HandoffStore(state_dir / STORE_FILENAMES["handoffs"], clock=clock)
        execution_store = ExecutionStore(state_dir / STORE_FILENAMES["executions"], clock=clock)
        wait_store = WaitStore(state_dir / STORE_FILENAMES["waits"], clock=clock)
        validation_store = ValidationStore(state_dir / STORE_FILENAMES["validation"], clock=clock)
        qa_run_store = QARunStore(state_dir / STORE_FILENAMES["qa_runs"], clock=clock)
        git_store = GitWorkItemStore(state_dir / STORE_FILENAMES["git_governance"], clock=clock)

        try:
            registry = config.load_worker_registry()
            enabled_workers = list(registry.enabled_workers())
            adapters = (
                provider_adapters if provider_adapters is not None
                else _resolve_provider_adapters({w.provider for w in enabled_workers})
            )

            quota_manager = QuotaManager(adapters, QuotaPolicy(state_ttl=timedelta(minutes=2)), clock=clock)
            worker_selector = WorkerSelector(enabled_workers, quota_manager)
            # P12: the project's execution permission policy always reaches
            # RalphExecutionEngine explicitly — never omitted for this path,
            # never inherited from host-global CLI configuration.
            execution_engine = RalphExecutionEngine(
                execution_store, clock=clock, permission_mode=config.execution.permission_mode,
                subprocess_runner=subprocess_runner,
            )
            gate_runner = QualityGateRunner(validation_store, clock=clock)
            qa_engine = InternalQAEngine(validation_store=validation_store, gate_runner=gate_runner, clock=clock)
            validation_store.set_project_commands(config.project.id, list(config.qa_commands))

            # Same proven WorkItem Flow-compatible policy already used by
            # scripts/run_external_project_pilot.py: require_review/
            # require_required_gates default True in GitGovernancePolicy,
            # but WorkItem Flow wires neither a quality_gate_runner nor a
            # review_store into MVPManager (the QA phase's own commands ARE
            # the gate; DEV B's corrective review replaces independent
            # review) — left at their default, merge eligibility could
            # never be satisfied.
            git_service = GitGovernanceService(
                git_store,
                policy=GitGovernancePolicy(
                    auto_merge=True, base_branch=config.git.base_branch,
                    require_review=False, require_required_gates=False,
                ),
                clock=clock,
            )

            manager = MVPManager(
                project_store, handoff_store, worker_selector, execution_engine,
                wait_store=wait_store, execution_store=execution_store, git_governance_service=git_service,
                qa_engine=qa_engine, qa_policy=QAPolicy(), qa_run_store=qa_run_store,
                clock=clock,
            )
        except Exception:
            for store in (project_store, handoff_store, execution_store, wait_store, validation_store, qa_run_store, git_store):
                store.close()
            raise

        return cls(
            config=config, project_store=project_store, handoff_store=handoff_store,
            execution_store=execution_store, wait_store=wait_store, validation_store=validation_store,
            qa_run_store=qa_run_store, git_store=git_store, manager=manager,
        )

    def bootstrap(self) -> None:
        """Idempotent ``aido.yaml`` -> ``ProjectStateStore`` initialization
        (§11). First call creates the Project/current MVP/WorkItems.
        Every call (including the first) verifies persisted identity
        stays compatible with the current config and fails closed
        (``ConfigRuntimeConflictError``) on a material conflict — never a
        silent mutation of historical/runtime definitions."""
        cfg = self.config

        try:
            project = self.project_store.get_project(cfg.project.id)
        except UnknownProjectError:
            project = self.project_store.create_project(
                project_id=cfg.project.id, name=cfg.project.name, workspace=cfg.project.workspace,
            )
        else:
            if project.workspace.resolve() != cfg.project.workspace.resolve():
                raise ConfigRuntimeConflictError(
                    f"persisted project {cfg.project.id!r} has workspace "
                    f"{str(project.workspace)!r}, but aido.yaml now says "
                    f"{str(cfg.project.workspace)!r} — refusing to silently "
                    "change a persisted project's workspace."
                )

        try:
            mvp = self.project_store.get_mvp(cfg.mvp.id)
        except UnknownMVPError:
            mvp = self.project_store.create_mvp(
                mvp_id=cfg.mvp.id, project_id=cfg.project.id, objective=cfg.mvp.objective,
                acceptance_criteria=cfg.mvp.acceptance_criteria,
            )
        else:
            if mvp.project_id != cfg.project.id:
                raise ConfigRuntimeConflictError(
                    f"persisted MVP {cfg.mvp.id!r} belongs to project "
                    f"{mvp.project_id!r}, but aido.yaml now says "
                    f"{cfg.project.id!r} — refusing to silently reassign it."
                )

        if project.current_mvp_id != cfg.mvp.id:
            self.project_store.set_current_mvp(cfg.project.id, cfg.mvp.id)

        existing_ids = {wi.work_item_id for wi in self.project_store.list_work_items(cfg.mvp.id)}
        for wi_config in cfg.work_items:
            if wi_config.id in existing_ids:
                existing = self.project_store.get_work_item(wi_config.id)
                if (
                    existing.title != wi_config.title
                    or existing.required_capabilities != frozenset(wi_config.required_capabilities)
                    or existing.dependencies != frozenset(wi_config.dependencies)
                ):
                    raise ConfigRuntimeConflictError(
                        f"persisted WorkItem {wi_config.id!r} no longer matches "
                        "aido.yaml's definition of it (title/required_capabilities/"
                        "dependencies changed) — refusing to silently redefine a "
                        "persisted WorkItem. A future roadmap/application mechanism "
                        "may govern config evolution; for now, edit this WorkItem's "
                        "id instead, or resolve the conflict manually."
                    )
                continue
            self.project_store.create_work_item(
                work_item_id=wi_config.id, mvp_id=cfg.mvp.id, title=wi_config.title,
                required_capabilities=wi_config.required_capabilities,
                dependencies=wi_config.dependencies, acceptance_criteria=wi_config.acceptance_criteria,
            )

        self.project_store.refresh_readiness(cfg.mvp.id)

    def close(self) -> None:
        """Closes every SQLite connection this runtime owns."""
        self.project_store.close()
        self.handoff_store.close()
        self.execution_store.close()
        self.wait_store.close()
        self.validation_store.close()
        self.qa_run_store.close()
        self.git_store.close()

    def __enter__(self) -> "ProjectRuntime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
