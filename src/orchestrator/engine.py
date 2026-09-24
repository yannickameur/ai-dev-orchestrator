"""OrchestratorEngine: the public façade a frontend (AIDO Code, or any
other future caller) needs to drive a project, without ever constructing
``ProjectRuntime``, ``MVPManager``, ``WorkerSelector``, ``QuotaManager``,
a ``ProviderAdapter``, ``GitGovernanceService``, ``InternalQAEngine``, or
reading a Store/SQLite file directly (P13, ROADMAP.md).

REUSE FIRST: every method below delegates to an existing, already-tested
primitive; nothing here re-implements selection, quota, QA, or Git logic.

- ``.validate()``/``.workers()``: ``ProjectConfig`` + ``WorkerRegistry``,
  read-only, exactly what ``aido validate`` already does.
- ``.status()``: ``ProjectStatusReader``, P1's own strictly read-only
  status path (no state_dir/SQLite creation, no migration, no provider
  call).
- ``.probe_workers()``: ``project_runtime.resolve_provider_adapters`` plus
  a plain, ephemeral ``QuotaManager`` (no SQLite at all).
- ``.run()``: ``ProjectRuntime.open()``/``.bootstrap()`` plus
  ``MVPManager.run_next_work_item()``, the exact loop ``aido run`` already
  runs.

A frontend never picks a worker and never decides whether a WorkItem is
done. It never reads persisted state itself either: every answer comes
back as one of the small, frozen, JSON-serializable snapshot types below
(plain ``str``/``int``/``bool``/tuple fields only, never a live Store/
connection object, never an internal dataclass from another module).

This façade holds no persistent connection between calls: every method
opens and closes whatever it needs for the duration of that one call,
exactly like ``aido status``/``aido validate``/``aido run`` already do.
``.close()`` exists for API symmetry and safe ``with``/``finally`` usage,
not because there is a live resource to release.

Engine/library boundary: the ``WorkerRegistry`` this façade uses is
injected by the caller (``worker_registry=`` on ``__init__``/``.open()``)
rather than owned by ``ProjectConfig``/``aido.yaml``. ``WorkerSelector``
remains the sole owner of *which* worker gets picked — injection only
supplies *what's available*, never a caller-side selection decision. A
``None`` falls back to the legacy, file-based ``ProjectConfig.
load_worker_registry()`` path for a config that still declares a
``workers:`` section (see ``project_config.py``'s own module docstring).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.project_config import ProjectConfig, ProjectConfigError
from orchestrator.project_runtime import (
    ConfigRuntimeConflictError,
    ProjectRuntime,
    ProjectRuntimeError,
    ProjectStatusReader,
    resolve_provider_adapters,
)
from orchestrator.project_state import UnknownMVPError, UnknownProjectError, WorkItem, WorkItemStatus
from orchestrator.quota_manager import ProviderProbeError, QuotaManager, QuotaPolicy
from orchestrator.worker_registry import UnknownWorkerError, WorkerRegistry, WorkerRegistryError

DEFAULT_MAX_CYCLES = 50
# A probe is always an explicit, on-demand real read: state_ttl exists only
# because QuotaPolicy requires a positive value, not because a cached
# result is ever considered "fresh enough" here. Every probe_workers()
# call builds its own QuotaManager from scratch.
_PROBE_STATE_TTL = timedelta(seconds=1)

# Same three terminal WorkItemStatus values `aido run` already uses to
# decide when a project is done: a plain frozenset literal, not logic
# imported from the CLI.
_TERMINAL_STATUSES = frozenset(
    {WorkItemStatus.COMPLETED, WorkItemStatus.FAILED, WorkItemStatus.BLOCKED}
)


class EngineError(Exception):
    """Base for OrchestratorEngine domain errors. Wraps an existing typed
    error from ProjectConfig/ProjectRuntime with a stable, frontend-facing
    type: never a second, parallel error taxonomy, and never a bare
    traceback of an internal exception type a frontend was never meant to
    import."""


class EngineConfigError(EngineError):
    """The ``aido.yaml`` or its referenced worker registry is invalid, or a
    configured provider could not be resolved/configured (e.g. a missing
    API key for an enabled DeepSeek/Kimi worker)."""


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """Static configuration facts. Never a provider call, never a Store
    read; exactly what ``aido validate`` already prints."""

    project_id: str
    name: str
    workspace: str
    state_dir: str
    mvp_id: str
    work_item_count: int
    qa_command_count: int
    enabled_worker_count: int
    providers: tuple[str, ...]
    permission_mode: str
    base_branch: str


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    """One configured worker's static identity. Never a provider probe;
    see ``ProviderSnapshot``/``probe_workers()`` for that."""

    worker_id: str
    display_name: str
    enabled: bool
    provider: str
    backend: str
    capabilities: tuple[str, ...]
    priority: int
    default_profile_id: str | None
    model: str | None


@dataclass(frozen=True, slots=True)
class QuotaWindowSnapshot:
    """One observed quota window (``probe_workers()`` only) — a thin,
    serializable projection of ``providers.contracts.QuotaWindow``, never
    the contract type itself. ``window_type`` is the provider's own opaque
    label (e.g. Codex's ``primary_5h``/``secondary_7d``, Claude's
    ``five_hour``/``seven_day``); this type never assumes a fixed set.
    ``utilization``/``remaining`` are ``None`` when genuinely unknown —
    never coerced to a fabricated 0%/100%. ``remaining`` is computed
    (``1.0 - utilization``) only when ``utilization`` is known."""

    window_type: str
    utilization: float | None
    remaining: float | None
    reset_at: str | None
    source: str


@dataclass(frozen=True, slots=True)
class ResetCreditSnapshot:
    """One observed, purely descriptive reset credit (``probe_workers()``
    only) — a thin, serializable projection of
    ``providers.contracts.ResetCredit``. Nothing in this façade can
    consume one; ``auto_consume`` stays pinned to ``False`` upstream and
    is intentionally not even exposed here (nothing downstream needs it
    to render a description)."""

    title: str
    status: str
    available_count: int | None


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """One provider's real, freshly-probed availability (``probe_workers()``
    only). ``reason`` is one of ``UnavailabilityReason``'s own values,
    ``"available"``, or ``"probe_error: ..."``, never a fabricated quota
    percentage or reset time; an unknown state stays reported as unknown.

    ``quota_windows``/``reset_credits`` are provider-level facts (never
    per-worker — two workers sharing one provider share the exact same
    quota, see ``ROADMAP.md``): empty tuples by default, so any existing
    caller ignoring them is unaffected. ``reset_at`` is kept for backward
    compatibility (the reset timestamps of every quota window, flattened);
    a new caller should prefer ``quota_windows`` for the full picture."""

    provider: str
    available: bool
    reason: str
    reset_at: tuple[str, ...] = ()
    quota_windows: tuple[QuotaWindowSnapshot, ...] = ()
    reset_credits: tuple[ResetCreditSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """The last recorded execution for one WorkItem: a thin, serializable
    projection of ``ExecutionRecord``, never the record itself."""

    execution_id: str
    worker_id: str
    #: The worker's ``display_name`` resolved from the *current* worker
    #: registry — ``None`` if that ``worker_id`` no longer exists there
    #: (e.g. removed from ``config/workers.yaml`` since this execution
    #: ran). Never fabricated/guessed; a stale/missing registry entry is
    #: reported honestly as unknown, never silently dropped or invented.
    worker_display_name: str | None
    provider: str
    status: str
    permission_mode: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class WaitSnapshot:
    """The latest durable wait for one WorkItem, if any."""

    wait_id: str
    phase: str
    eligible_at: str
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkItemSnapshot:
    work_item_id: str
    status: str
    blocked_reason: str | None
    last_execution: ExecutionSnapshot | None = None
    wait: WaitSnapshot | None = None


@dataclass(frozen=True, slots=True)
class MVPStatusSnapshot:
    mvp_id: str
    #: ``None`` means the MVP is configured in aido.yaml but has never
    #: actually been created in persisted state yet; never fabricated.
    status: str | None


@dataclass(frozen=True, slots=True)
class ProjectStatusSnapshot:
    """The full read-only status of a project's persisted state.

    ``initialized=False`` mirrors ``aido status``'s own ``NOT_INITIALIZED``:
    nothing has ever been bootstrapped for this ``aido.yaml`` yet, and this
    call created nothing itself while checking that."""

    initialized: bool
    project_id: str
    project_name: str | None
    mvp: MVPStatusSnapshot | None
    work_items: tuple[WorkItemSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """The smallest structured event contract a frontend needs to render a
    timeline (§10, ROADMAP.md P13). A frontend must never parse stdout to
    reconstruct engine state.

    ``kind`` is currently one of ``"work_item.<status>"`` (e.g.
    ``"work_item.completed"``, ``"work_item.waiting"``,
    ``"work_item.blocked"``), emitted once per ``run_next_work_item()``
    call inside ``.run()``: the actual granularity ``MVPManager`` exposes
    today. Finer sub-steps (``dev_a.running``, ``dev_a.completed``,
    ``dev_b.running``, ``qa.running``, ``merge.completed``, ...) are NOT
    emitted anywhere in this codebase yet: ``MVPManager._execute_work_item``
    runs DEV A/DEV B/QA/merge synchronously inside one call, with no event
    bus. Exposing that finer timeline is real, separate engine work for a
    future increment; this type is the formalized contract that work will
    fill in, not a claim that it already exists.
    """

    kind: str
    timestamp: str
    project_id: str
    mvp_id: str
    work_item_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunResult:
    """What happened over one ``.run()`` call: mirrors ``aido run``'s own
    "=== summary ===" block, plus the coarse events observed along the way."""

    cycles_run: int
    all_terminal: bool
    reached_max_cycles: bool
    work_items: tuple[WorkItemSnapshot, ...]
    events: tuple[EngineEvent, ...] = ()


class OrchestratorEngine:
    """The one public entry point a frontend needs. Construct via ``.open()``."""

    def __init__(
        self, config: ProjectConfig, *,
        worker_registry: WorkerRegistry | None = None,
        provider_adapters: dict[str, Any] | None = None,
        subprocess_runner: object | None = None,
    ) -> None:
        self._config = config
        # The modern injection seam (engine/library boundary): the
        # embedding application (AIDO Code, or any future caller)
        # constructs/owns its WorkerRegistry and hands it here directly —
        # never loaded from a `workers.registry` path in the project's own
        # configuration when set. `None` falls back to the legacy,
        # file-based `config.load_worker_registry()` path (see
        # `_load_worker_registry()`), for a `ProjectConfig` that still
        # carries a `workers:` section.
        self._worker_registry = worker_registry
        # Test-only seams, the same shape/purpose as
        # ``ProjectRuntime.open()``'s own ``provider_adapters``/
        # ``subprocess_runner``: production callers never set these. A
        # test passes fakes here instead of a real Claude/Codex/Vibe/
        # DeepSeek/Kimi probe or a real ``ralph`` subprocess.
        self._provider_adapters = provider_adapters
        self._subprocess_runner = subprocess_runner

    @classmethod
    def open(
        cls, config_path: str, *,
        worker_registry: WorkerRegistry | None = None,
        provider_adapters: dict[str, Any] | None = None,
        subprocess_runner: object | None = None,
    ) -> "OrchestratorEngine":
        """Loads and validates ``aido.yaml`` eagerly, read-only: no
        ``state_dir``/SQLite/provider call. Raises ``EngineConfigError`` on
        any structural problem, never a bare ``ProjectConfigError``/
        ``WorkerRegistryError`` a frontend was never meant to import.

        ``worker_registry``, when given, is used for every worker-facing
        call (``.workers()``, ``.validate()``, ``.probe_workers()``,
        ``.run()``, worker display-name resolution in ``.status()``)
        instead of a legacy ``workers.registry`` path read from
        ``aido.yaml`` — the modern engine boundary never requires
        ``config_path`` to reference one at all (see ``ProjectConfig``'s
        own module docstring)."""
        try:
            config = ProjectConfig.load(config_path)
        except (ProjectConfigError, WorkerRegistryError) as exc:
            raise EngineConfigError(str(exc)) from exc
        return cls(
            config, worker_registry=worker_registry,
            provider_adapters=provider_adapters, subprocess_runner=subprocess_runner,
        )

    def validate(self) -> ProjectSnapshot:
        """Loads/validates the configuration and its worker registry; no
        side effect. Exactly what ``aido validate`` already checks."""
        registry = self._load_worker_registry()
        enabled = registry.enabled_workers()
        cfg = self._config
        return ProjectSnapshot(
            project_id=cfg.project.id,
            name=cfg.project.name,
            workspace=str(cfg.project.workspace),
            state_dir=str(cfg.project.state_dir),
            mvp_id=cfg.mvp.id,
            work_item_count=len(cfg.work_items),
            qa_command_count=len(cfg.qa_commands),
            enabled_worker_count=len(enabled),
            providers=tuple(sorted({w.provider for w in enabled})),
            permission_mode=cfg.execution.permission_mode.value,
            base_branch=cfg.git.base_branch,
        )

    def workers(self) -> tuple[WorkerSnapshot, ...]:
        """Static worker inspection: every configured worker (enabled or
        not), no provider probe. See ``probe_workers()`` for real
        availability."""
        registry = self._load_worker_registry()
        return tuple(
            WorkerSnapshot(
                worker_id=w.worker_id,
                display_name=w.display_name,
                enabled=w.enabled,
                provider=w.provider,
                backend=w.backend,
                capabilities=tuple(sorted(w.capabilities)),
                priority=w.priority,
                default_profile_id=w.default_profile_id,
                model=w.profile().model,
            )
            for w in registry.all_workers()
        )

    def probe_workers(self) -> tuple[ProviderSnapshot, ...]:
        """Explicit, real provider probe for every provider an *enabled*
        worker actually uses. Never launches a worker/Ralph execution,
        never touches SQLite. An unavailable/unrecognized state is
        reported honestly (``"unknown"``/``"probe_error: ..."``), never
        guessed as available or fabricated as a quota percentage."""
        registry = self._load_worker_registry()
        providers = {w.provider for w in registry.enabled_workers()}
        if not providers:
            return ()
        if self._provider_adapters is not None:
            adapters = {p: a for p, a in self._provider_adapters.items() if p in providers}
        else:
            try:
                adapters = resolve_provider_adapters(providers)
            except ProjectRuntimeError as exc:
                raise EngineConfigError(str(exc)) from exc

        quota_manager = QuotaManager(adapters, QuotaPolicy(state_ttl=_PROBE_STATE_TTL))
        snapshots = []
        for provider in sorted(providers):
            try:
                state = asyncio.run(quota_manager.refresh(provider))
            except ProviderProbeError as exc:
                snapshots.append(
                    ProviderSnapshot(provider=provider, available=False, reason=f"probe_error: {exc}")
                )
                continue
            reason = (
                "available"
                if state.availability.available
                else (state.availability.reason.value if state.availability.reason else "unknown")
            )
            reset_at = tuple(w.reset_at.isoformat() for w in state.quota_windows if w.reset_at is not None)
            quota_windows = tuple(
                QuotaWindowSnapshot(
                    window_type=w.window_type,
                    utilization=w.utilization,
                    remaining=(1.0 - w.utilization) if w.utilization is not None else None,
                    reset_at=w.reset_at.isoformat() if w.reset_at is not None else None,
                    source=w.source,
                )
                for w in state.quota_windows
            )
            reset_credits = tuple(
                ResetCreditSnapshot(
                    title=c.title, status=c.status.value, available_count=c.available_count,
                )
                for c in state.reset_credits
            )
            snapshots.append(
                ProviderSnapshot(
                    provider=provider, available=state.availability.available, reason=reason,
                    reset_at=reset_at, quota_windows=quota_windows, reset_credits=reset_credits,
                )
            )
        return tuple(snapshots)

    def status(self) -> ProjectStatusSnapshot:
        """Strictly read-only: never creates ``state_dir``, never runs
        ``CREATE TABLE``/a migration, never writes a row, never calls a
        provider. Delegates entirely to ``ProjectStatusReader``."""
        reader = ProjectStatusReader.open(self._config)
        if reader is None:
            return ProjectStatusSnapshot(
                initialized=False, project_id=self._config.project.id, project_name=None, mvp=None,
            )
        try:
            return self._read_status(reader)
        finally:
            reader.close()

    def _read_status(self, reader: ProjectStatusReader) -> ProjectStatusSnapshot:
        cfg = self._config
        try:
            project = reader.project_store.get_project(cfg.project.id)
        except UnknownProjectError:
            return ProjectStatusSnapshot(
                initialized=False, project_id=cfg.project.id, project_name=None, mvp=None,
            )

        try:
            mvp = reader.project_store.get_mvp(cfg.mvp.id)
        except UnknownMVPError:
            return ProjectStatusSnapshot(
                initialized=True, project_id=project.project_id, project_name=project.name,
                mvp=MVPStatusSnapshot(mvp_id=cfg.mvp.id, status=None),
            )

        # Loaded once per status() call, not per WorkItem: a stale worker
        # registry read failure must never break the rest of the status
        # (it only degrades display-name resolution to "unknown" below).
        try:
            registry = self._load_worker_registry()
        except EngineConfigError:
            registry = None

        work_items = tuple(
            self._work_item_snapshot(reader, wi, registry)
            for wi in sorted(reader.project_store.list_work_items(cfg.mvp.id), key=lambda w: w.work_item_id)
        )
        return ProjectStatusSnapshot(
            initialized=True, project_id=project.project_id, project_name=project.name,
            mvp=MVPStatusSnapshot(mvp_id=mvp.mvp_id, status=mvp.status.value),
            work_items=work_items,
        )

    def _work_item_snapshot(
        self, reader: ProjectStatusReader, wi: WorkItem, registry: WorkerRegistry | None,
    ) -> WorkItemSnapshot:
        executions = reader.list_executions_for_work_item(wi.work_item_id)
        last_execution = None
        if executions:
            last = executions[-1]
            display_name = None
            if registry is not None:
                try:
                    display_name = registry.get(last.worker_id).display_name
                except UnknownWorkerError:
                    display_name = None
            last_execution = ExecutionSnapshot(
                execution_id=last.execution_id, worker_id=last.worker_id,
                worker_display_name=display_name, provider=last.provider,
                status=last.status.value, permission_mode=last.permission_mode,
                started_at=last.started_at.isoformat(),
                finished_at=last.finished_at.isoformat() if last.finished_at else None,
            )

        item_waits = [w for w in reader.list_waits_for_mvp(self._config.mvp.id) if w.work_item_id == wi.work_item_id]
        wait_snapshot = None
        if item_waits:
            latest = max(item_waits, key=lambda w: w.created_at)
            wait_snapshot = WaitSnapshot(
                wait_id=latest.wait_id, phase=latest.phase.value,
                eligible_at=latest.eligible_at.isoformat(), providers=latest.providers,
            )

        return WorkItemSnapshot(
            work_item_id=wi.work_item_id, status=wi.status.value, blocked_reason=wi.blocked_reason,
            last_execution=last_execution, wait=wait_snapshot,
        )

    def run(self, *, max_cycles: int = DEFAULT_MAX_CYCLES) -> RunResult:
        """Starts or resumes the project: bootstrap (idempotent) then drive
        ``MVPManager.run_next_work_item``, the exact ``aido run`` loop,
        never a second selection/QA/merge implementation. A frontend only
        ever asks to "advance the project"; every decision (WAITING,
        RECOVERY_REQUIRED, which worker, PASS/FAIL, merge) is this
        engine's alone."""
        try:
            runtime = ProjectRuntime.open(
                self._config,
                worker_registry=self._worker_registry,
                provider_adapters=self._provider_adapters,
                subprocess_runner=self._subprocess_runner,
            )
        except ProjectRuntimeError as exc:
            raise EngineError(str(exc)) from exc

        try:
            try:
                runtime.bootstrap()
            except ConfigRuntimeConflictError as exc:
                raise EngineError(str(exc)) from exc
            return self._drive(runtime, max_cycles=max_cycles)
        finally:
            runtime.close()

    def _drive(self, runtime: ProjectRuntime, *, max_cycles: int) -> RunResult:
        mvp_id = self._config.mvp.id
        events: list[EngineEvent] = []
        cycles_run = 0
        reached_max_cycles = False

        for cycle in range(1, max_cycles + 1):
            cycles_run = cycle
            result = asyncio.run(runtime.manager.run_next_work_item(mvp_id))
            if result is None:
                break
            events.append(self._event_from_result(result))
            if all(wi.status in _TERMINAL_STATUSES for wi in runtime.project_store.list_work_items(mvp_id)):
                break
        else:
            reached_max_cycles = True

        final_items = sorted(runtime.project_store.list_work_items(mvp_id), key=lambda w: w.work_item_id)
        work_items = tuple(
            WorkItemSnapshot(work_item_id=wi.work_item_id, status=wi.status.value, blocked_reason=wi.blocked_reason)
            for wi in final_items
        )
        return RunResult(
            cycles_run=cycles_run,
            all_terminal=all(wi.status in _TERMINAL_STATUSES for wi in final_items),
            reached_max_cycles=reached_max_cycles,
            work_items=work_items,
            events=tuple(events),
        )

    def _event_from_result(self, result: Any) -> EngineEvent:
        wi = result.work_item
        payload: dict[str, Any] = {}
        if result.wait is not None:
            payload["wait_eligible_at"] = result.wait.eligible_at.isoformat()
            payload["wait_providers"] = list(result.wait.providers)
        return EngineEvent(
            kind=f"work_item.{wi.status.value}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            project_id=self._config.project.id, mvp_id=self._config.mvp.id,
            work_item_id=wi.work_item_id, payload=payload,
        )

    def _load_worker_registry(self) -> WorkerRegistry:
        if self._worker_registry is not None:
            return self._worker_registry
        try:
            return self._config.load_worker_registry()
        except WorkerRegistryError as exc:
            raise EngineConfigError(str(exc)) from exc

    def close(self) -> None:
        """No-op: see module docstring, nothing above holds a resource
        between calls. Safe to call always, including from ``with``."""
        return None

    def __enter__(self) -> "OrchestratorEngine":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
