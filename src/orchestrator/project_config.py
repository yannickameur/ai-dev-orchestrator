"""project_config — public project configuration foundation (P12).

Loads/validates a single ``aido.yaml`` file (schema v1) describing an
external project AIDO governs: identity, workspace, worker registry
reference, execution permission policy, Git base branch, the current
MVP, its WorkItems, and its deterministic QA commands.

This eliminates the two current architectural problems P12 is meant to
fix (ROADMAP.md §13): (A) external projects were only describable/wired
through a custom Python harness (e.g. ``scripts/run_external_project_pilot.py``);
(B) worker execution permission mode depended entirely on machine-local
CLI configuration rather than an explicit, project-declared policy.

REUSE FIRST — this module invents nothing that already exists elsewhere:

- QA commands are exactly ``orchestrator.validation.ValidationCommand``/
  ``ValidationKind`` — never a second, parallel "QA command" shape.
- The execution permission mode is exactly
  ``orchestrator.execution_policy.ExecutionPermissionMode``.
- The worker pool is never redefined here — ``workers.registry`` is a
  path to an existing ``orchestrator.worker_registry.WorkerRegistry``-
  shaped YAML file (typically the project's own ``config/workers.yaml``),
  loaded and validated eagerly at ``ProjectConfig.load()`` time. Schema v1
  deliberately supports exactly one such reference — no per-project copy
  of the worker pool, ever.

Schema v1 is deliberately minimal (KISS/YAGNI): exactly one configured
MVP. Multi-MVP configuration is not built here.

FAIL-CLOSED: unknown top-level/nested keys, an unsupported
``schema_version``, a malformed section, a duplicate WorkItem id, an
unknown WorkItem dependency, a dependency cycle, an invalid QA command,
a missing/non-Git workspace, or an unreadable/invalid worker registry
all raise before a single field is trusted — never a partially-loaded
config.

NO SECRETS — same guard-rail as ``worker_registry.py``: a handful of
obviously-wrong key name substrings (``api_key``, ``token``, ``secret``,
``password``, ``credential``, ...) anywhere in the document are rejected.
Provider authentication stays entirely with the provider CLI/environment.

Path semantics: every relative path in ``aido.yaml`` (``project.workspace``,
``project.state_dir``, ``workers.registry``) resolves relative to the
*directory containing that ``aido.yaml`` file*, never the caller's current
working directory. ``~`` is expanded. If ``project.state_dir`` is omitted,
it defaults deterministically to
``~/.local/state/ai-dev-orchestrator/projects/<project-id>/`` — the same
convention already used elsewhere in this project (ROADMAP.md §8) — never
``/tmp``.

This module does not yet wire a ``ProjectConfig`` into
``ProjectStateStore``/``MVPManager``/``RalphExecutionEngine`` — that is
P1's job (the public CLI), the next approved WorkItem. P12 provides the
clean, typed, reusable ``ProjectConfig.load(path)`` API that P1 (and any
other future caller) consumes; it never adds an ``[project.scripts]``
entry point itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.validation import ValidationCommand, ValidationKind
from orchestrator.worker_registry import WorkerRegistry, WorkerRegistryError

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CONFIG_FILENAME = "aido.yaml"

_FORBIDDEN_KEY_SUBSTRINGS = ("api_key", "apikey", "token", "secret", "password", "passwd", "credential")

_TOP_LEVEL_KEYS = frozenset({"schema_version", "project", "workers", "execution", "git", "mvp", "work_items", "qa"})
_PROJECT_KEYS = frozenset({"id", "name", "workspace", "state_dir"})
_WORKERS_KEYS = frozenset({"registry"})
_EXECUTION_KEYS = frozenset({"permission_mode"})
_GIT_KEYS = frozenset({"base_branch"})
_MVP_KEYS = frozenset({"id", "objective", "acceptance_criteria"})
_WORK_ITEM_KEYS = frozenset({"id", "title", "required_capabilities", "dependencies", "acceptance_criteria"})
_QA_KEYS = frozenset({"id", "kind", "argv", "timeout_seconds", "required"})

_DEFAULT_BASE_BRANCH = "main"


class ProjectConfigError(Exception):
    """Base for ProjectConfig domain errors."""


class InvalidProjectConfigError(ProjectConfigError):
    """Raised for any structurally invalid ``aido.yaml`` content."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid project configuration: {detail}")


class UnsupportedSchemaVersionError(ProjectConfigError):
    def __init__(self, version: Any) -> None:
        super().__init__(
            f"unsupported schema_version {version!r} — this version of AIDO only "
            f"supports {SUPPORTED_SCHEMA_VERSION!r}"
        )
        self.version = version


class DuplicateWorkItemError(ProjectConfigError):
    def __init__(self, work_item_id: str) -> None:
        super().__init__(f"duplicate work_item id in aido.yaml: {work_item_id!r}")
        self.work_item_id = work_item_id


class UnknownWorkItemDependencyError(ProjectConfigError):
    def __init__(self, work_item_id: str, dependency_id: str) -> None:
        super().__init__(
            f"work_item {work_item_id!r} depends on unknown work_item {dependency_id!r}"
        )
        self.work_item_id = work_item_id
        self.dependency_id = dependency_id


class WorkItemDependencyCycleError(ProjectConfigError):
    def __init__(self, cycle: Sequence[str]) -> None:
        super().__init__(f"work_item dependency cycle detected: {' -> '.join(cycle)}")
        self.cycle = tuple(cycle)


def _reject_secrets(data: Any, *, path: str = "") -> None:
    """Same guard-rail as ``worker_registry._reject_secrets`` — deliberately
    simple substring matching, not a general secret scanner."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            key_str = str(key).lower()
            if any(bad in key_str for bad in _FORBIDDEN_KEY_SUBSTRINGS):
                raise InvalidProjectConfigError(
                    f"field {path + '.' + str(key) if path else key!r} looks like a secret "
                    "(api keys/tokens/credentials never belong in aido.yaml — "
                    "authentication stays with the provider CLI)"
                )
            _reject_secrets(value, path=f"{path}.{key}" if path else str(key))
    elif isinstance(data, (list, tuple)):
        for index, item in enumerate(data):
            _reject_secrets(item, path=f"{path}[{index}]")


def _require_mapping(data: Any, *, context: str) -> Mapping:
    if not isinstance(data, Mapping):
        raise InvalidProjectConfigError(f"{context} must be a mapping")
    return data


def _reject_unknown_keys(data: Mapping, allowed: frozenset[str], *, context: str) -> None:
    unknown = set(data.keys()) - allowed
    if unknown:
        raise InvalidProjectConfigError(
            f"{context}: unknown field(s) {sorted(unknown)!r} — allowed: {sorted(allowed)!r}"
        )


def _require_str(data: Mapping, key: str, *, context: str) -> str:
    if key not in data:
        raise InvalidProjectConfigError(f"{context}: missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise InvalidProjectConfigError(f"{context}: field {key!r} must be a non-empty string, got {value!r}")
    return value


def _optional_str_list(data: Mapping, key: str, *, context: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise InvalidProjectConfigError(f"{context}: field {key!r} must be a list of non-empty strings")
    return tuple(value)


def _resolve_path(raw: str, *, base_dir: Path) -> Path:
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded
    return (base_dir / expanded).resolve()


def _default_state_dir(project_id: str) -> Path:
    return Path.home() / ".local" / "state" / "ai-dev-orchestrator" / "projects" / project_id


def _is_inside_git_work_tree(path: Path) -> bool:
    """Read-only Git check (same pattern as
    ``ralph_execution_engine._git_head_sha``) — ``rev-parse
    --is-inside-work-tree`` correctly accepts any directory inside a
    working tree, not just a repository's literal root (a project's
    ``aido.yaml`` may reasonably live in a subdirectory of a larger
    repository)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


class ExecutionConfig:
    """Project-controlled worker execution permission policy (P12)."""

    __slots__ = ("permission_mode",)

    def __init__(self, *, permission_mode: ExecutionPermissionMode) -> None:
        self.permission_mode = permission_mode


class GitConfig:
    """Git governance policy inputs sourced from project configuration."""

    __slots__ = ("base_branch",)

    def __init__(self, *, base_branch: str) -> None:
        self.base_branch = base_branch


class MVPConfig:
    __slots__ = ("id", "objective", "acceptance_criteria")

    def __init__(self, *, id: str, objective: str, acceptance_criteria: tuple[str, ...]) -> None:
        self.id = id
        self.objective = objective
        self.acceptance_criteria = acceptance_criteria


class WorkItemConfig:
    __slots__ = ("id", "title", "required_capabilities", "dependencies", "acceptance_criteria")

    def __init__(
        self, *, id: str, title: str, required_capabilities: tuple[str, ...],
        dependencies: tuple[str, ...], acceptance_criteria: tuple[str, ...],
    ) -> None:
        self.id = id
        self.title = title
        self.required_capabilities = required_capabilities
        self.dependencies = dependencies
        self.acceptance_criteria = acceptance_criteria


class ProjectIdentity:
    __slots__ = ("id", "name", "workspace", "state_dir")

    def __init__(self, *, id: str, name: str, workspace: Path, state_dir: Path) -> None:
        self.id = id
        self.name = name
        self.workspace = workspace
        self.state_dir = state_dir


def _parse_permission_mode(value: Any, *, context: str) -> ExecutionPermissionMode:
    try:
        return ExecutionPermissionMode.parse(value)
    except ValueError as exc:
        raise InvalidProjectConfigError(f"{context}: {exc}") from exc


def _parse_qa_command(data: Any, *, index: int) -> ValidationCommand:
    context = f"qa[{index}]"
    mapping = _require_mapping(data, context=context)
    _reject_unknown_keys(mapping, _QA_KEYS, context=context)
    command_id = _require_str(mapping, "id", context=context)
    kind_raw = _require_str(mapping, "kind", context=context)
    try:
        kind = ValidationKind(kind_raw)
    except ValueError:
        raise InvalidProjectConfigError(
            f"{context}: invalid kind {kind_raw!r} — must be one of "
            f"{[k.value for k in ValidationKind]!r}"
        ) from None
    argv_raw = mapping.get("argv")
    if not isinstance(argv_raw, list):
        raise InvalidProjectConfigError(f"{context}: 'argv' must be a list of non-empty strings, never a shell string")
    timeout_seconds = mapping.get("timeout_seconds", 300.0)
    required = mapping.get("required", True)
    if not isinstance(required, bool):
        raise InvalidProjectConfigError(f"{context}: 'required' must be a bool, got {required!r}")
    try:
        return ValidationCommand(
            validation_id=command_id, kind=kind, argv=tuple(argv_raw),
            timeout_seconds=timeout_seconds, required=required,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidProjectConfigError(f"{context}: {exc}") from exc


def _parse_work_item(data: Any, *, index: int) -> WorkItemConfig:
    context = f"work_items[{index}]"
    mapping = _require_mapping(data, context=context)
    _reject_unknown_keys(mapping, _WORK_ITEM_KEYS, context=context)
    return WorkItemConfig(
        id=_require_str(mapping, "id", context=context),
        title=_require_str(mapping, "title", context=context),
        required_capabilities=_optional_str_list(mapping, "required_capabilities", context=context),
        dependencies=_optional_str_list(mapping, "dependencies", context=context),
        acceptance_criteria=_optional_str_list(mapping, "acceptance_criteria", context=context),
    )


def _validate_work_items(items: Sequence[WorkItemConfig]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise DuplicateWorkItemError(item.id)
        seen.add(item.id)

    for item in items:
        for dependency_id in item.dependencies:
            if dependency_id not in seen:
                raise UnknownWorkItemDependencyError(item.id, dependency_id)

    by_id = {item.id: item for item in items}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {item.id: WHITE for item in items}
    path: list[str] = []

    def visit(work_item_id: str) -> None:
        color[work_item_id] = GRAY
        path.append(work_item_id)
        for dependency_id in by_id[work_item_id].dependencies:
            if color[dependency_id] == GRAY:
                cycle_start = path.index(dependency_id)
                raise WorkItemDependencyCycleError(path[cycle_start:] + [dependency_id])
            if color[dependency_id] == WHITE:
                visit(dependency_id)
        path.pop()
        color[work_item_id] = BLACK

    for item in items:
        if color[item.id] == WHITE:
            visit(item.id)


class ProjectConfig:
    """A fully loaded, validated ``aido.yaml`` (schema v1)."""

    __slots__ = (
        "schema_version", "project", "workers_registry_path", "execution",
        "git", "mvp", "work_items", "qa_commands", "source_path",
    )

    def __init__(
        self, *, schema_version: int, project: ProjectIdentity, workers_registry_path: Path,
        execution: ExecutionConfig, git: GitConfig, mvp: MVPConfig,
        work_items: tuple[WorkItemConfig, ...], qa_commands: tuple[ValidationCommand, ...],
        source_path: Path,
    ) -> None:
        self.schema_version = schema_version
        self.project = project
        self.workers_registry_path = workers_registry_path
        self.execution = execution
        self.git = git
        self.mvp = mvp
        self.work_items = work_items
        self.qa_commands = qa_commands
        self.source_path = source_path

    @classmethod
    def load(cls, path: str | Path) -> "ProjectConfig":
        """Fail-closed: any structural problem raises before a single
        field is trusted — never a partially-loaded config. Eagerly loads
        and validates the referenced worker registry too (a bad registry
        reference fails config loading itself, not just a later step)."""
        source_path = Path(path).expanduser().resolve()
        try:
            text = source_path.read_text()
        except OSError as exc:
            raise InvalidProjectConfigError(f"could not read {source_path}: {exc}") from exc
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise InvalidProjectConfigError(f"invalid YAML: {exc}") from exc

        top = _require_mapping(document, context="aido.yaml")
        _reject_unknown_keys(top, _TOP_LEVEL_KEYS, context="aido.yaml")
        _reject_secrets(top)

        schema_version = top.get("schema_version")
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(schema_version)

        base_dir = source_path.parent

        project_data = _require_mapping(top.get("project"), context="project")
        _reject_unknown_keys(project_data, _PROJECT_KEYS, context="project")
        project_id = _require_str(project_data, "id", context="project")
        project_name = _require_str(project_data, "name", context="project")
        workspace_raw = _require_str(project_data, "workspace", context="project")
        workspace = _resolve_path(workspace_raw, base_dir=base_dir)
        if not workspace.is_dir():
            raise InvalidProjectConfigError(f"project.workspace {str(workspace)!r} is not an existing directory")
        if not _is_inside_git_work_tree(workspace):
            raise InvalidProjectConfigError(
                f"project.workspace {str(workspace)!r} is not inside a Git working tree"
            )
        state_dir_raw = project_data.get("state_dir")
        state_dir = (
            _resolve_path(state_dir_raw, base_dir=base_dir)
            if state_dir_raw is not None
            else _default_state_dir(project_id)
        )
        project = ProjectIdentity(id=project_id, name=project_name, workspace=workspace, state_dir=state_dir)

        workers_data = _require_mapping(top.get("workers"), context="workers")
        _reject_unknown_keys(workers_data, _WORKERS_KEYS, context="workers")
        registry_raw = _require_str(workers_data, "registry", context="workers")
        workers_registry_path = _resolve_path(registry_raw, base_dir=base_dir)
        try:
            WorkerRegistry.load(workers_registry_path)
        except OSError as exc:
            raise InvalidProjectConfigError(f"workers.registry {str(workers_registry_path)!r}: {exc}") from exc
        except WorkerRegistryError:
            raise  # already a clear, typed domain error — never double-wrapped

        execution_data = _require_mapping(top.get("execution"), context="execution")
        _reject_unknown_keys(execution_data, _EXECUTION_KEYS, context="execution")
        permission_mode = _parse_permission_mode(
            execution_data.get("permission_mode"), context="execution.permission_mode"
        )
        execution = ExecutionConfig(permission_mode=permission_mode)

        git_data = top.get("git", {})
        git_data = _require_mapping(git_data, context="git")
        _reject_unknown_keys(git_data, _GIT_KEYS, context="git")
        base_branch = git_data.get("base_branch", _DEFAULT_BASE_BRANCH)
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise InvalidProjectConfigError(f"git.base_branch must be a non-empty string, got {base_branch!r}")
        git = GitConfig(base_branch=base_branch)

        mvp_data = _require_mapping(top.get("mvp"), context="mvp")
        _reject_unknown_keys(mvp_data, _MVP_KEYS, context="mvp")
        mvp = MVPConfig(
            id=_require_str(mvp_data, "id", context="mvp"),
            objective=_require_str(mvp_data, "objective", context="mvp"),
            acceptance_criteria=_optional_str_list(mvp_data, "acceptance_criteria", context="mvp"),
        )

        work_items_raw = top.get("work_items", [])
        if not isinstance(work_items_raw, list):
            raise InvalidProjectConfigError("'work_items' must be a list")
        work_items = tuple(_parse_work_item(entry, index=i) for i, entry in enumerate(work_items_raw))
        _validate_work_items(work_items)

        qa_raw = top.get("qa", [])
        if not isinstance(qa_raw, list):
            raise InvalidProjectConfigError("'qa' must be a list")
        qa_commands = tuple(_parse_qa_command(entry, index=i) for i, entry in enumerate(qa_raw))

        return cls(
            schema_version=schema_version, project=project, workers_registry_path=workers_registry_path,
            execution=execution, git=git, mvp=mvp, work_items=work_items, qa_commands=qa_commands,
            source_path=source_path,
        )

    def load_worker_registry(self) -> WorkerRegistry:
        """Convenience re-load — ``load()`` already validated this
        eagerly; a caller wanting the live ``WorkerRegistry`` object calls
        this explicitly rather than this class caching a stale one."""
        return WorkerRegistry.load(self.workers_registry_path)
