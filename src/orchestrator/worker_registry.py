"""WorkerRegistry — loads/validates Workers declared in ``config/workers.yaml``
(Slice 15).

Answers exactly one question: "what workers/profiles are configured, and
which of them are enabled?" It never selects a worker for a task (that is
``~orchestrator.worker_selector.WorkerSelector``'s job, unchanged), never
estimates complexity, never resolves an adaptive profile, and never
launches anything — pure configuration loading and validation.

WorkerRegistry != WorkerSelector:

- ``WorkerRegistry`` answers "what exists, and is it enabled?" (a static,
  offline fact about configuration).
- ``WorkerSelector`` answers "which one, right now?" (capability/
  governance/quota, Slice 4) — it is handed the registry's
  ``enabled_workers()`` and never imports this module.

FAIL-CLOSED CONFIGURATION: every structural invariant a ``Worker``/
``ExecutionProfile`` already enforces in ``__post_init__`` (non-empty
worker_id, at least one profile, duplicate profile_id, a resolvable
default_profile_id, a valid ``QualityTier``, ...) applies here unchanged —
this module adds only the registry-level invariants those types cannot
express themselves: duplicate ``worker_id`` across the whole file, and
that the raw YAML shape itself is well-formed before it ever reaches
``Worker``/``ExecutionProfile``. A malformed file never produces a partial
registry — the whole load fails.

NO SECRETS: ``config/workers.yaml`` declares agents/profiles only —
provider authentication remains entirely the Claude Code/Codex CLIs' own
concern (already logged in, outside this project). Loading rejects a
handful of obviously-wrong key names (``api_key``, ``token``, ``secret``,
``password``, ``credential``, ...) anywhere in the file, as a cheap
guard-rail — never a general-purpose secret scanner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from orchestrator.worker_selector import ExecutionProfile, QualityTier, UnknownWorkerError, Worker

_FORBIDDEN_KEY_SUBSTRINGS = ("api_key", "apikey", "token", "secret", "password", "passwd", "credential")


class WorkerRegistryError(Exception):
    """Base for WorkerRegistry domain errors."""


class InvalidWorkerConfigError(WorkerRegistryError):
    """Raised for any structurally invalid ``config/workers.yaml`` content."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid worker configuration: {detail}")


class DuplicateWorkerError(WorkerRegistryError):
    def __init__(self, worker_id: str) -> None:
        super().__init__(f"duplicate worker_id in registry: {worker_id!r}")
        self.worker_id = worker_id


def _reject_secrets(data: Any, *, path: str = "") -> None:
    """Recursively rejects keys that look like credentials.

    Deliberately simple substring matching — not a general secret
    scanner, just a guard-rail against the obvious mistake of pasting a
    provider API key into a config file meant to hold no secrets at all.
    """
    if isinstance(data, Mapping):
        for key, value in data.items():
            key_str = str(key).lower()
            if any(bad in key_str for bad in _FORBIDDEN_KEY_SUBSTRINGS):
                raise InvalidWorkerConfigError(
                    f"field {path + '.' + str(key) if path else key!r} looks like a secret "
                    "(api keys/tokens/credentials never belong in workers.yaml — "
                    "authentication stays with the provider CLI)"
                )
            _reject_secrets(value, path=f"{path}.{key}" if path else str(key))
    elif isinstance(data, (list, tuple)):
        for index, item in enumerate(data):
            _reject_secrets(item, path=f"{path}[{index}]")


def _require_str(data: Mapping, key: str, *, context: str) -> str:
    if key not in data:
        raise InvalidWorkerConfigError(f"{context}: missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise InvalidWorkerConfigError(f"{context}: field {key!r} must be a non-empty string, got {value!r}")
    return value


def _parse_quality_tier(value: Any, *, context: str) -> QualityTier:
    if isinstance(value, str):
        try:
            return QualityTier[value.strip().upper()]
        except KeyError:
            raise InvalidWorkerConfigError(
                f"{context}: invalid quality_tier {value!r} — must be one of "
                f"{[t.name for t in QualityTier]!r}"
            ) from None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return QualityTier(value)
        except ValueError:
            raise InvalidWorkerConfigError(
                f"{context}: invalid quality_tier {value!r} — must be one of "
                f"{[t.value for t in QualityTier]!r}"
            ) from None
    raise InvalidWorkerConfigError(f"{context}: quality_tier must be a string or int, got {value!r}")


def _parse_profile(profile_id: Any, data: Any, *, worker_id: str) -> ExecutionProfile:
    context = f"worker {worker_id!r}, profile {profile_id!r}"
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise InvalidWorkerConfigError(f"worker {worker_id!r}: profile id must be a non-empty string")
    if not isinstance(data, Mapping):
        raise InvalidWorkerConfigError(f"{context}: profile must be a mapping")
    quality_tier = _parse_quality_tier(data.get("quality_tier"), context=context)
    model = _require_str(data, "model", context=context)
    reasoning_effort = data.get("reasoning_effort")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
    ):
        raise InvalidWorkerConfigError(
            f"{context}: reasoning_effort must be a non-empty string or null, got {reasoning_effort!r}"
        )
    cost_rank = data.get("cost_rank", 0)
    if not isinstance(cost_rank, int) or isinstance(cost_rank, bool) or cost_rank < 0:
        raise InvalidWorkerConfigError(f"{context}: cost_rank must be a non-negative int, got {cost_rank!r}")
    try:
        return ExecutionProfile(
            profile_id=profile_id, quality_tier=quality_tier, model=model,
            reasoning_effort=reasoning_effort, cost_rank=cost_rank,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidWorkerConfigError(f"{context}: {exc}") from exc


def _parse_worker(data: Any) -> Worker:
    if not isinstance(data, Mapping):
        raise InvalidWorkerConfigError("each worker entry must be a mapping")

    worker_id = _require_str(data, "worker_id", context="worker")
    context = f"worker {worker_id!r}"
    display_name = _require_str(data, "display_name", context=context)
    provider = _require_str(data, "provider", context=context)
    backend = _require_str(data, "backend", context=context)

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise InvalidWorkerConfigError(f"{context}: capabilities must be a list of strings")

    priority = data.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise InvalidWorkerConfigError(f"{context}: priority must be an int, got {priority!r}")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidWorkerConfigError(f"{context}: enabled must be a bool, got {enabled!r}")

    profiles_data = data.get("profiles")
    if not isinstance(profiles_data, Mapping) or not profiles_data:
        raise InvalidWorkerConfigError(f"{context}: must declare at least one profile under 'profiles'")
    profiles = tuple(
        _parse_profile(profile_id, profile_data, worker_id=worker_id)
        for profile_id, profile_data in profiles_data.items()
    )

    default_profile_id = data.get("default_profile_id")
    if default_profile_id is not None and not isinstance(default_profile_id, str):
        raise InvalidWorkerConfigError(f"{context}: default_profile_id must be a string or null")

    estimator_profile_id = data.get("estimator_profile_id")
    if estimator_profile_id is not None and not isinstance(estimator_profile_id, str):
        raise InvalidWorkerConfigError(f"{context}: estimator_profile_id must be a string or null")

    try:
        return Worker(
            worker_id=worker_id, display_name=display_name, provider=provider, backend=backend,
            capabilities=frozenset(capabilities), priority=priority, enabled=enabled,
            profiles=profiles, default_profile_id=default_profile_id,
            estimator_profile_id=estimator_profile_id,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidWorkerConfigError(f"{context}: {exc}") from exc


def _parse_workers_document(document: Any) -> tuple[Worker, ...]:
    if not isinstance(document, Mapping) or "workers" not in document:
        raise InvalidWorkerConfigError("top-level document must be a mapping with a 'workers' key")
    _reject_secrets(document)
    entries = document["workers"]
    if not isinstance(entries, list):
        raise InvalidWorkerConfigError("'workers' must be a list")

    workers: list[Worker] = []
    seen_ids: set[str] = set()
    for entry in entries:
        worker = _parse_worker(entry)
        if worker.worker_id in seen_ids:
            raise DuplicateWorkerError(worker.worker_id)
        seen_ids.add(worker.worker_id)
        workers.append(worker)
    return tuple(workers)


class WorkerRegistry:
    """In-memory, offline registry of configured Workers.

    Never selects a worker itself — see module docstring. Constructing it
    directly with an explicit ``Sequence[Worker]`` (e.g. from tests, or a
    caller that already has ``Worker`` objects) is just as supported as
    ``WorkerRegistry.load(...)`` from a YAML file.
    """

    def __init__(self, workers: Sequence[Worker]) -> None:
        by_id: dict[str, Worker] = {}
        for worker in workers:
            if worker.worker_id in by_id:
                raise DuplicateWorkerError(worker.worker_id)
            by_id[worker.worker_id] = worker
        self._workers = tuple(workers)
        self._by_id = by_id

    @classmethod
    def load(cls, path: str | Path) -> "WorkerRegistry":
        """Loads and validates a ``config/workers.yaml``-shaped file.

        Fail-closed: any structural problem (missing field, duplicate
        worker_id/profile_id, invalid quality_tier, a field that looks
        like a secret, ...) raises before a single ``Worker`` is
        returned — never a partially-loaded registry.
        """
        text = Path(path).read_text()
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise InvalidWorkerConfigError(f"invalid YAML: {exc}") from exc
        workers = _parse_workers_document(document)
        return cls(workers)

    def all_workers(self) -> tuple[Worker, ...]:
        """Every configured worker, enabled or not — for inspection/audit."""
        return self._workers

    def enabled_workers(self) -> tuple[Worker, ...]:
        """Only the workers eligible to be handed to a WorkerSelector."""
        return tuple(w for w in self._workers if w.enabled)

    def get(self, worker_id: str) -> Worker:
        worker = self._by_id.get(worker_id)
        if worker is None:
            raise UnknownWorkerError(worker_id)
        return worker

    def get_profile(self, worker_id: str, profile_id: str | None = None) -> ExecutionProfile:
        """Convenience: ``registry.get(worker_id).profile(profile_id)``."""
        return self.get(worker_id).profile(profile_id)
