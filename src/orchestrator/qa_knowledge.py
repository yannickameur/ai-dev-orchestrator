"""Regression knowledge base — Git-durable, provider-independent QA
knowledge for a target repository's ``.qa/`` directory (Slice 22).

GIT VS SQLITE BOUNDARY (see ``docs/QA_STRATEGY.md`` §15.1,
``docs/QA_GOVERNANCE.md``): this module reads/writes **only**
``.qa/*.yaml`` in the *target* repository — durable product knowledge
that travels with the project (invariants, regression map, critical
paths, known-flaky tests). It never touches orchestrator SQLite
(``orchestrator.qa.QARunStore``, ``ValidationStore``, ...) — that stays
runtime/audit state, never product knowledge. This module has no
``sqlite3`` import at all, by design.

A project with no ``.qa/`` directory remains valid: every loader below
returns an empty result for a missing file, never an error, and this
module never auto-creates a single file unless a caller explicitly asks
it to write one.

``known-flaky.yaml`` is never a skip list: nothing in this module (or
anywhere else in this project) converts a FAIL into a PASS because a test
is listed there — see ``KnownFlakyEntry``.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from orchestrator.qa import TestImpactRequest, TestImpactResult

_INVARIANTS_FILE = "invariants.yaml"
_REGRESSION_MAP_FILE = "regression-map.yaml"
_CRITICAL_PATHS_FILE = "critical-paths.yaml"
_KNOWN_FLAKY_FILE = "known-flaky.yaml"


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _tuple_of_str(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings, got {value!r}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return items


class QAKnowledgeError(Exception):
    """Base for ``.qa/`` knowledge base domain errors."""


class InvalidQAKnowledgeFileError(QAKnowledgeError):
    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"invalid QA knowledge file {path}: {detail}")
        self.path = path
        self.detail = detail


class DuplicateInvariantIdError(QAKnowledgeError):
    def __init__(self, invariant_id: str) -> None:
        super().__init__(f"duplicate invariant_id in .qa/invariants.yaml: {invariant_id!r}")
        self.invariant_id = invariant_id


class DuplicateRegressionMapIdError(QAKnowledgeError):
    def __init__(self, entry_id: str) -> None:
        super().__init__(f"duplicate entry id in .qa/regression-map.yaml: {entry_id!r}")
        self.entry_id = entry_id


class DuplicateCriticalPathIdError(QAKnowledgeError):
    def __init__(self, path_id: str) -> None:
        super().__init__(f"duplicate path id in .qa/critical-paths.yaml: {path_id!r}")
        self.path_id = path_id


# --- schema dataclasses ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QAInvariant:
    """One durable QA invariant. ``ORCH-*`` is only ever an illustrative
    example id in this project's own docs — this schema never hardcodes
    or requires any particular id prefix."""

    invariant_id: str
    description: str
    criticality: str = "unknown"
    source: str = ""
    introduced_by_work_item: str = ""
    related_tests: tuple[str, ...] = ()
    active: bool = True

    def __post_init__(self) -> None:
        _require_non_empty_str(self.invariant_id, field_name="QAInvariant.invariant_id")
        _require_non_empty_str(self.description, field_name="QAInvariant.description")
        _require_non_empty_str(self.criticality, field_name="QAInvariant.criticality")
        object.__setattr__(self, "related_tests", _tuple_of_str(self.related_tests, field_name="QAInvariant.related_tests"))
        if not isinstance(self.active, bool):
            raise TypeError("QAInvariant.active must be a bool")


@dataclass(frozen=True, slots=True)
class RegressionMapEntry:
    """Links functional/technical paths to the tests/invariants that must
    be re-run when they change."""

    entry_id: str
    paths: tuple[str, ...]
    related_tests: tuple[str, ...] = ()
    invariant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.entry_id, field_name="RegressionMapEntry.entry_id")
        object.__setattr__(self, "paths", _tuple_of_str(self.paths, field_name="RegressionMapEntry.paths"))
        if not self.paths:
            raise ValueError("RegressionMapEntry.paths must be a non-empty sequence")
        object.__setattr__(self, "related_tests", _tuple_of_str(self.related_tests, field_name="RegressionMapEntry.related_tests"))
        object.__setattr__(self, "invariant_ids", _tuple_of_str(self.invariant_ids, field_name="RegressionMapEntry.invariant_ids"))


@dataclass(frozen=True, slots=True)
class CriticalPath:
    """A path/capability considered critical enough to impose specific
    required tests/invariants whenever it's touched."""

    path_id: str
    paths: tuple[str, ...]
    required_tests: tuple[str, ...] = ()
    required_invariant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.path_id, field_name="CriticalPath.path_id")
        object.__setattr__(self, "paths", _tuple_of_str(self.paths, field_name="CriticalPath.paths"))
        if not self.paths:
            raise ValueError("CriticalPath.paths must be a non-empty sequence")
        object.__setattr__(self, "required_tests", _tuple_of_str(self.required_tests, field_name="CriticalPath.required_tests"))
        object.__setattr__(
            self, "required_invariant_ids", _tuple_of_str(self.required_invariant_ids, field_name="CriticalPath.required_invariant_ids")
        )


@dataclass(frozen=True, slots=True)
class KnownFlakyEntry:
    """Documents a suspected-flaky test. **Never** a skip list — presence
    here never converts a FAIL into a PASS anywhere in this project; a
    critical flaky test stays fully visible."""

    test_id: str
    evidence: str
    suspected_cause: str = ""
    first_seen_at: str = ""
    status: str = "open"
    retry_policy: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.test_id, field_name="KnownFlakyEntry.test_id")
        _require_non_empty_str(self.evidence, field_name="KnownFlakyEntry.evidence")
        if self.retry_policy is not None:
            _require_non_empty_str(self.retry_policy, field_name="KnownFlakyEntry.retry_policy")


@dataclass(frozen=True, slots=True)
class QAKnowledgeBase:
    invariants: tuple[QAInvariant, ...] = ()
    regression_map: tuple[RegressionMapEntry, ...] = ()
    critical_paths: tuple[CriticalPath, ...] = ()
    known_flaky: tuple[KnownFlakyEntry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.invariants or self.regression_map or self.critical_paths or self.known_flaky)

    def active_invariants(self) -> tuple[QAInvariant, ...]:
        return tuple(i for i in self.invariants if i.active)

    def invariant(self, invariant_id: str) -> QAInvariant | None:
        for inv in self.invariants:
            if inv.invariant_id == invariant_id:
                return inv
        return None


# --- YAML I/O -----------------------------------------------------------


def _safe_load_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise InvalidQAKnowledgeFileError(path, str(exc)) from exc
    return data


def _atomic_write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(data, sort_keys=True, allow_unicode=True, default_flow_style=False)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_invariants(path: Path) -> tuple[QAInvariant, ...]:
    data = _safe_load_yaml(path)
    if data is None:
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("invariants"), list):
        raise InvalidQAKnowledgeFileError(path, "expected a mapping with an 'invariants' list")
    seen: set[str] = set()
    result: list[QAInvariant] = []
    for raw in data["invariants"]:
        if not isinstance(raw, dict):
            raise InvalidQAKnowledgeFileError(path, f"invariant entry must be a mapping, got {raw!r}")
        try:
            inv = QAInvariant(
                invariant_id=raw["invariant_id"], description=raw["description"],
                criticality=raw.get("criticality", "unknown"), source=raw.get("source", ""),
                introduced_by_work_item=raw.get("introduced_by_work_item", ""),
                related_tests=tuple(raw.get("related_tests") or ()), active=bool(raw.get("active", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidQAKnowledgeFileError(path, str(exc)) from exc
        if inv.invariant_id in seen:
            raise DuplicateInvariantIdError(inv.invariant_id)
        seen.add(inv.invariant_id)
        result.append(inv)
    return tuple(result)


def write_invariants(repository_path: str | Path, invariants: Sequence[QAInvariant]) -> None:
    path = Path(repository_path) / ".qa" / _INVARIANTS_FILE
    ids = [inv.invariant_id for inv in invariants]
    if len(ids) != len(set(ids)):
        raise DuplicateInvariantIdError(next(i for i in ids if ids.count(i) > 1))
    data = {
        "invariants": [
            {
                "invariant_id": inv.invariant_id, "description": inv.description, "criticality": inv.criticality,
                "source": inv.source, "introduced_by_work_item": inv.introduced_by_work_item,
                "related_tests": list(inv.related_tests), "active": inv.active,
            }
            for inv in invariants
        ]
    }
    _atomic_write_yaml(path, data)


def _load_regression_map(path: Path) -> tuple[RegressionMapEntry, ...]:
    data = _safe_load_yaml(path)
    if data is None:
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise InvalidQAKnowledgeFileError(path, "expected a mapping with an 'entries' list")
    seen: set[str] = set()
    result: list[RegressionMapEntry] = []
    for raw in data["entries"]:
        if not isinstance(raw, dict):
            raise InvalidQAKnowledgeFileError(path, f"regression-map entry must be a mapping, got {raw!r}")
        try:
            entry = RegressionMapEntry(
                entry_id=raw["id"], paths=tuple(raw["paths"]),
                related_tests=tuple(raw.get("related_tests") or ()),
                invariant_ids=tuple(raw.get("invariant_ids") or ()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidQAKnowledgeFileError(path, str(exc)) from exc
        if entry.entry_id in seen:
            raise DuplicateRegressionMapIdError(entry.entry_id)
        seen.add(entry.entry_id)
        result.append(entry)
    return tuple(result)


def write_regression_map(repository_path: str | Path, entries: Sequence[RegressionMapEntry]) -> None:
    path = Path(repository_path) / ".qa" / _REGRESSION_MAP_FILE
    ids = [e.entry_id for e in entries]
    if len(ids) != len(set(ids)):
        raise DuplicateRegressionMapIdError(next(i for i in ids if ids.count(i) > 1))
    data = {
        "entries": [
            {
                "id": e.entry_id, "paths": list(e.paths), "related_tests": list(e.related_tests),
                "invariant_ids": list(e.invariant_ids),
            }
            for e in entries
        ]
    }
    _atomic_write_yaml(path, data)


def _load_critical_paths(path: Path) -> tuple[CriticalPath, ...]:
    data = _safe_load_yaml(path)
    if data is None:
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("critical_paths"), list):
        raise InvalidQAKnowledgeFileError(path, "expected a mapping with a 'critical_paths' list")
    seen: set[str] = set()
    result: list[CriticalPath] = []
    for raw in data["critical_paths"]:
        if not isinstance(raw, dict):
            raise InvalidQAKnowledgeFileError(path, f"critical-path entry must be a mapping, got {raw!r}")
        try:
            entry = CriticalPath(
                path_id=raw["id"], paths=tuple(raw["paths"]),
                required_tests=tuple(raw.get("required_tests") or ()),
                required_invariant_ids=tuple(raw.get("required_invariant_ids") or ()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidQAKnowledgeFileError(path, str(exc)) from exc
        if entry.path_id in seen:
            raise DuplicateCriticalPathIdError(entry.path_id)
        seen.add(entry.path_id)
        result.append(entry)
    return tuple(result)


def write_critical_paths(repository_path: str | Path, entries: Sequence[CriticalPath]) -> None:
    path = Path(repository_path) / ".qa" / _CRITICAL_PATHS_FILE
    ids = [e.path_id for e in entries]
    if len(ids) != len(set(ids)):
        raise DuplicateCriticalPathIdError(next(i for i in ids if ids.count(i) > 1))
    data = {
        "critical_paths": [
            {
                "id": e.path_id, "paths": list(e.paths), "required_tests": list(e.required_tests),
                "required_invariant_ids": list(e.required_invariant_ids),
            }
            for e in entries
        ]
    }
    _atomic_write_yaml(path, data)


def _load_known_flaky(path: Path) -> tuple[KnownFlakyEntry, ...]:
    data = _safe_load_yaml(path)
    if data is None:
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("known_flaky"), list):
        raise InvalidQAKnowledgeFileError(path, "expected a mapping with a 'known_flaky' list")
    result: list[KnownFlakyEntry] = []
    for raw in data["known_flaky"]:
        if not isinstance(raw, dict):
            raise InvalidQAKnowledgeFileError(path, f"known-flaky entry must be a mapping, got {raw!r}")
        try:
            result.append(
                KnownFlakyEntry(
                    test_id=raw["test_id"], evidence=raw["evidence"], suspected_cause=raw.get("suspected_cause", ""),
                    first_seen_at=raw.get("first_seen_at", ""), status=raw.get("status", "open"),
                    retry_policy=raw.get("retry_policy"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidQAKnowledgeFileError(path, str(exc)) from exc
    return tuple(result)


def write_known_flaky(repository_path: str | Path, entries: Sequence[KnownFlakyEntry]) -> None:
    path = Path(repository_path) / ".qa" / _KNOWN_FLAKY_FILE
    data = {
        "known_flaky": [
            {
                "test_id": e.test_id, "evidence": e.evidence, "suspected_cause": e.suspected_cause,
                "first_seen_at": e.first_seen_at, "status": e.status, "retry_policy": e.retry_policy,
            }
            for e in entries
        ]
    }
    _atomic_write_yaml(path, data)


def load_qa_knowledge_base(repository_path: str | Path) -> QAKnowledgeBase:
    """Loads ``.qa/*.yaml`` from the target repository. A missing ``.qa/``
    directory, or any individual missing file, yields an empty section —
    never an error, and nothing is ever auto-created by reading."""
    base = Path(repository_path) / ".qa"
    return QAKnowledgeBase(
        invariants=_load_invariants(base / _INVARIANTS_FILE),
        regression_map=_load_regression_map(base / _REGRESSION_MAP_FILE),
        critical_paths=_load_critical_paths(base / _CRITICAL_PATHS_FILE),
        known_flaky=_load_known_flaky(base / _KNOWN_FLAKY_FILE),
    )


# --- deterministic Test Impact Analysis (no LLM, no AST, no dep graph) ------


def analyze_test_impact_deterministic(
    request: TestImpactRequest, knowledge_base: QAKnowledgeBase
) -> TestImpactResult:
    """The minimal, directly-derivable-from-Git-+-``.qa/`` analyzer this
    slice implements: changed path -> matching ``regression-map``/
    ``critical-paths`` entries -> related tests/invariants. Deliberately
    NOT an AST analyzer, NOT a multi-language dependency graph, NOT
    semantic analysis — those remain a future engine's job (Slice 23+).

    Matching is prefix-based on each entry's declared ``paths`` (a changed
    file is impacted by an entry if it starts with one of that entry's
    declared path prefixes) — deterministic, and an unrelated change (no
    path prefix matches) invents nothing: empty result, not a guess.
    """
    impacted_components: list[str] = []
    existing_tests: list[str] = []
    required_invariants: list[str] = []
    rationale: list[str] = []

    changed = list(request.changed_files)

    for entry in knowledge_base.regression_map:
        matched_files = [f for f in changed if any(f.startswith(p) for p in entry.paths)]
        if not matched_files:
            continue
        impacted_components.append(entry.entry_id)
        existing_tests.extend(entry.related_tests)
        required_invariants.extend(entry.invariant_ids)
        rationale.append(f"regression-map entry {entry.entry_id!r} matched changed file(s) {matched_files!r}")

    for critical in knowledge_base.critical_paths:
        matched_files = [f for f in changed if any(f.startswith(p) for p in critical.paths)]
        if not matched_files:
            continue
        impacted_components.append(critical.path_id)
        existing_tests.extend(critical.required_tests)
        required_invariants.extend(critical.required_invariant_ids)
        rationale.append(f"critical-path {critical.path_id!r} matched changed file(s) {matched_files!r}")

    return TestImpactResult(
        impacted_components=tuple(dict.fromkeys(impacted_components)),
        impacted_capabilities=(),
        existing_tests=tuple(dict.fromkeys(existing_tests)),
        missing_test_areas=(),
        recommended_test_levels=(),
        required_invariants=tuple(dict.fromkeys(required_invariants)),
        rationale=tuple(rationale),
    )
