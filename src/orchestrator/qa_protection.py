"""Protected-test baseline — SHA-256 evidence that regression tests have
not been silently weakened, deleted, or semantically rewritten
(Slice 22).

CENTRAL INVARIANT (``docs/QA_STRATEGY.md`` §5): an existing regression
test is a protected asset. Neither an internal QA engine nor an external
solution can delete an assertion, weaken it, skip the test, replace an
expected value, or semantically "self-heal" it, just because new code
fails — without a versioned, traceable authorization (see
``TestChangeAuthorization``/``ExpectedChangeSource``).

This module only *detects* that a protected file's content changed
(deterministic SHA-256 hashing). Deciding whether a detected change is
semantically acceptable requires an ``ExpectedChangeSource``-backed
``TestChangeAuthorization`` supplied by the caller — this module never
guesses that itself, and certainly never via an LLM.

The set of protected paths is always supplied by the caller (from a
``QAEvidenceManifest``, ``QAPolicy``, or the regression knowledge base —
see ``orchestrator.qa``/``orchestrator.qa_knowledge``) — this module never
hardcodes a "tests/" convention or any other Python-specific assumption.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


class ExpectedChangeSource(str, Enum):
    """Where a versioned decision to change a protected test came from.
    This module implements only the contract — not the full human-approval
    workflow (that remains future work, likely Slice 23/24)."""

    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    SPECIFICATION = "specification"
    ROADMAP_DECISION = "roadmap_decision"
    ARCHITECTURE_DECISION = "architecture_decision"
    HUMAN_APPROVAL = "human_approval"


@dataclass(frozen=True, slots=True)
class TestChangeAuthorization:
    """A versioned, traceable justification for a protected test's change."""

    path: str
    source: ExpectedChangeSource
    justification: str
    authorized_at: datetime
    reference: str = ""

    def __post_init__(self) -> None:
        _require_non_empty_str(self.path, field_name="TestChangeAuthorization.path")
        if not isinstance(self.source, ExpectedChangeSource):
            raise TypeError(f"TestChangeAuthorization.source must be an ExpectedChangeSource, got {type(self.source)!r}")
        _require_non_empty_str(self.justification, field_name="TestChangeAuthorization.justification")
        _require_aware(self.authorized_at, field_name="TestChangeAuthorization.authorized_at")


@dataclass(frozen=True, slots=True)
class ProtectedTestFileState:
    """One protected file's digest at baseline-capture time. ``digest is
    None`` only if the file did not exist at capture time."""

    path: str
    digest: str | None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.path, field_name="ProtectedTestFileState.path")
        if self.digest is not None:
            _require_non_empty_str(self.digest, field_name="ProtectedTestFileState.digest")


@dataclass(frozen=True, slots=True)
class ProtectedTestBaseline:
    """A deterministic SHA-256 snapshot of a caller-supplied set of
    protected files, bound to the exact ``base_sha`` it was captured at."""

    base_sha: str
    files: tuple[ProtectedTestFileState, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.base_sha, field_name="ProtectedTestBaseline.base_sha")
        object.__setattr__(self, "files", tuple(self.files))


@dataclass(frozen=True, slots=True)
class ProtectedTestChange:
    """The comparison of one protected file's baseline digest against its
    current digest. ``deleted`` is explicit (distinct from a plain
    modification); a brand-new file outside the protected set never shows
    up here at all — it is simply not a violation by construction."""

    path: str
    digest_before: str | None
    digest_after: str | None
    change_detected: bool
    deleted: bool
    authorization: TestChangeAuthorization | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.path, field_name="ProtectedTestChange.path")
        if not isinstance(self.change_detected, bool):
            raise TypeError("ProtectedTestChange.change_detected must be a bool")
        if not isinstance(self.deleted, bool):
            raise TypeError("ProtectedTestChange.deleted must be a bool")
        if self.authorization is not None and not isinstance(self.authorization, TestChangeAuthorization):
            raise TypeError("ProtectedTestChange.authorization must be a TestChangeAuthorization or None")

    @property
    def is_unauthorized(self) -> bool:
        return self.change_detected and self.authorization is None


def hash_file(path: Path) -> str | None:
    """Deterministic SHA-256 of a file's bytes, or ``None`` if it does not
    exist (never raises for a missing file — that is itself meaningful
    data, not an error)."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def capture_protected_test_baseline(
    repository_path: str | Path, protected_paths: Sequence[str], *, base_sha: str
) -> ProtectedTestBaseline:
    repo = Path(repository_path)
    files = tuple(ProtectedTestFileState(path=p, digest=hash_file(repo / p)) for p in protected_paths)
    return ProtectedTestBaseline(base_sha=base_sha, files=files)


def compare_protected_test_baseline(
    baseline: ProtectedTestBaseline,
    repository_path: str | Path,
    *,
    authorizations: Mapping[str, TestChangeAuthorization] | None = None,
) -> tuple[ProtectedTestChange, ...]:
    """Compares every file in ``baseline`` against its current on-disk
    digest. A file not present in ``baseline`` is never inspected here —
    "new unprotected test" is not automatically a violation, by
    construction, not by a special case."""
    repo = Path(repository_path)
    auth_map = authorizations or {}
    changes = []
    for file_state in baseline.files:
        current_digest = hash_file(repo / file_state.path)
        changed = current_digest != file_state.digest
        deleted = file_state.digest is not None and current_digest is None
        changes.append(
            ProtectedTestChange(
                path=file_state.path, digest_before=file_state.digest, digest_after=current_digest,
                change_detected=changed, deleted=deleted, authorization=auth_map.get(file_state.path),
            )
        )
    return tuple(changes)


def has_unauthorized_change(changes: Sequence[ProtectedTestChange]) -> bool:
    """Direct input for ``orchestrator.qa.evaluate_qa_verdict``'s
    ``unauthorized_protected_change`` kwarg."""
    return any(c.is_unauthorized for c in changes)
