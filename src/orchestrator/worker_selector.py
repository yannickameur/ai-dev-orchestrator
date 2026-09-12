"""WorkerSelector — chooses a Worker for a task, before any execution.

This module answers exactly one question: "given the workers configured,
the capability/role needed, and the provider state QuotaManager already
knows about, which worker (if any) may be used for this task?" It never
runs a worker, never retries, never falls back after a failed execution —
those belong to the later RalphExecutionEngine (see ROADMAP.md, Phase 1,
step 9).

Design invariants:

- No provider is intrinsically "author-only" or "reviewer-only". A role is
  just a capability string a task asks for (e.g. ``"developer"``,
  ``"reviewer"``); any worker declaring it is a candidate. Claude and Codex
  (or any future provider) are never named in this module's logic — only
  in test fixtures/configuration.
- Provider availability is read exclusively through
  :class:`~orchestrator.quota_manager.QuotaManager`. This module never
  launches a provider CLI, never parses stream-json or app-server output,
  and never duplicates QuotaManager's own caching/single-flight — it just
  calls ``QuotaManager.get(provider)`` once per distinct provider actually
  needed for a given selection.
- Fail-closed: a worker is only selectable when its provider's
  ``ProviderState.availability.available`` is exactly ``True``. Anything
  else — ``False`` for a known reason (quota exhausted, auth error,
  provider error) or ``False`` with an ``UNKNOWN`` reason — is treated
  identically as "not selectable".
- EXPECTED PROVIDER FAILURE != PROGRAMMING FAILURE: a ``ProviderProbeError``
  from ``QuotaManager.get(provider)`` is an expected, provider-level
  failure — that provider is treated as not-available for this selection,
  and other providers/candidates are still evaluated normally. Any other,
  unexpected exception is a programming failure and is never swallowed or
  turned into "not available" — it propagates to the caller unchanged.
- Quota *numbers* (``quota_windows``, ``utilization``, reset credits) are
  never used to rank or select a worker: the canonical availability signal
  (``ProviderState.availability.available``) alone answers the only
  question this module asks. Since Slice 11, ``QuotaWindow.reset_at``
  values ARE read, but strictly for attaching read-only diagnostics to a
  selection *failure* (see ``ProviderSelectionDiagnostic`` below) — never
  to decide who is selected. This module still never waits, retries, or
  schedules anything itself; a caller (orchestration-level) uses the
  diagnostics to decide whether waiting could plausibly help.
- Reset credits are visible on ``ProviderState`` but this module never
  consumes one, never calls a consume/usage endpoint, and never treats
  their mere presence as making an otherwise-unavailable provider
  selectable.

Selection order:

    1. worker registered with this selector
    2. required capabilities satisfied
    3. governance exclusions (explicit exclusions + author != reviewer)
    4. provider AVAILABLE (via QuotaManager)
    5. preference policy (priority, then a documented deterministic
       tie-break; for review requests, provider-independence preference)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from orchestrator.quota_manager import ProviderProbeError, QuotaManager


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _as_frozenset_of_str(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    result = frozenset(values)
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must only contain non-empty strings, got {item!r}")
    return result


@dataclass(frozen=True, slots=True)
class Worker:
    """A typed, technical identity for one worker configuration.

    ``worker_id`` is the stable technical identity used for every
    governance decision (author != reviewer, exclusions, priority
    tie-break). ``display_name`` is a human label only — it must never be
    used for a governance or selection decision.

    Two workers may share the same ``provider`` with different
    ``model``/``reasoning_effort`` and are independent identities for
    selection purposes (e.g. a fast/cheap and a careful/expensive variant
    of the same backend).
    """

    worker_id: str
    display_name: str
    provider: str
    backend: str
    model: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    reasoning_effort: str | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        _require_non_empty_str(self.worker_id, field_name="Worker.worker_id")
        _require_non_empty_str(self.display_name, field_name="Worker.display_name")
        _require_non_empty_str(self.provider, field_name="Worker.provider")
        _require_non_empty_str(self.backend, field_name="Worker.backend")
        _require_non_empty_str(self.model, field_name="Worker.model")
        object.__setattr__(
            self,
            "capabilities",
            _as_frozenset_of_str(self.capabilities, field_name="Worker.capabilities"),
        )
        if self.reasoning_effort is not None:
            _require_non_empty_str(self.reasoning_effort, field_name="Worker.reasoning_effort")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError(f"Worker.priority must be an int, got {type(self.priority)!r}")


@dataclass(frozen=True, slots=True)
class WorkerSelectionPolicy:
    """Small, explicit, deterministic selection policy.

    ``require_distinct_worker_for_review`` is the absolute minimum
    guarantee this module makes when an ``author_worker_id`` is supplied: a
    worker can never review its own work. It is pinned to ``True`` — it
    exists as an explicit field for readability/documentation, not as a
    knob that can be turned off.

    ``prefer_distinct_provider_for_review`` (soft) and
    ``require_distinct_provider_for_review`` (hard) control cross-provider
    independence: prefer tries a different-provider reviewer first and
    only falls back to a same-provider one when none exists; require never
    falls back and raises ``ReviewIndependenceError`` instead.
    """

    require_distinct_worker_for_review: bool = True
    prefer_distinct_provider_for_review: bool = True
    require_distinct_provider_for_review: bool = False

    def __post_init__(self) -> None:
        if self.require_distinct_worker_for_review is not True:
            raise ValueError(
                "WorkerSelectionPolicy.require_distinct_worker_for_review must be True: "
                "a worker must never be allowed to review its own work"
            )


@dataclass(frozen=True, slots=True)
class WorkerSelectionRequest:
    """What the caller needs selected.

    ``author_worker_id``, when supplied, marks this as a review selection:
    the resulting worker can never be that same worker, and the
    provider-independence policy applies.
    """

    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    author_worker_id: str | None = None
    excluded_worker_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_capabilities",
            _as_frozenset_of_str(
                self.required_capabilities, field_name="WorkerSelectionRequest.required_capabilities"
            ),
        )
        object.__setattr__(
            self,
            "excluded_worker_ids",
            _as_frozenset_of_str(
                self.excluded_worker_ids, field_name="WorkerSelectionRequest.excluded_worker_ids"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderSelectionDiagnostic:
    """Read-only facts about one candidate provider's state at selection time.

    Attached to a selection failure (never to a success) so a caller —
    typically an orchestration-level wait/resume decision, never this
    module — can distinguish *why* a provider wasn't usable: quota
    exhausted with a known reset time, an unavailable/unknown reason, or a
    probe error. This module never interprets these facts itself; it never
    waits, retries, or ranks anything by them.
    """

    provider: str
    available: bool
    reason: str
    reset_at: tuple[datetime, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reset_at", tuple(sorted(self.reset_at)))


class WorkerSelectorError(Exception):
    """Base for WorkerSelector domain errors."""


class UnknownWorkerError(WorkerSelectorError):
    """Raised when a referenced worker_id (e.g. author_worker_id) is not registered."""

    def __init__(self, worker_id: str) -> None:
        super().__init__(f"unknown worker: {worker_id!r}")
        self.worker_id = worker_id


class NoEligibleWorkerError(WorkerSelectorError):
    """Raised when no registered worker survives capability/governance/availability filters.

    ``diagnostics`` covers every candidate provider considered for this
    selection (not just the ones that ended up eligible) — see
    ``ProviderSelectionDiagnostic``. Defaults to ``()`` for callers that
    construct this error directly (e.g. tests) without diagnostics.
    """

    def __init__(
        self,
        request: WorkerSelectionRequest,
        *,
        diagnostics: tuple[ProviderSelectionDiagnostic, ...] = (),
    ) -> None:
        super().__init__(
            "no eligible worker for required_capabilities="
            f"{set(request.required_capabilities)!r}"
        )
        self.request = request
        self.diagnostics = diagnostics


class ReviewIndependenceError(WorkerSelectorError):
    """Raised when review independence is required but cannot be satisfied.

    Distinct from ``NoEligibleWorkerError``: eligible reviewer candidates
    did exist, but none of them satisfied
    ``require_distinct_provider_for_review`` against the author's provider.
    ``diagnostics`` covers every candidate provider considered (including
    providers that exist among registered reviewer-capable workers but
    were themselves unavailable) — see ``ProviderSelectionDiagnostic``.
    """

    def __init__(
        self,
        author: Worker,
        *,
        diagnostics: tuple[ProviderSelectionDiagnostic, ...] = (),
    ) -> None:
        super().__init__(
            f"no reviewer on a provider distinct from author {author.worker_id!r} "
            f"(provider={author.provider!r}) is available, and "
            "require_distinct_provider_for_review=True forbids falling back"
        )
        self.author = author
        self.diagnostics = diagnostics


class WorkerSelector:
    """Chooses a Worker for a task, using QuotaManager for availability."""

    def __init__(
        self,
        workers: Sequence[Worker],
        quota_manager: QuotaManager,
        policy: WorkerSelectionPolicy | None = None,
    ) -> None:
        self._workers = list(workers)
        self._by_id: dict[str, Worker] = {}
        for worker in self._workers:
            if worker.worker_id in self._by_id:
                raise ValueError(f"duplicate worker_id: {worker.worker_id!r}")
            self._by_id[worker.worker_id] = worker
        self._quota_manager = quota_manager
        self._policy = policy or WorkerSelectionPolicy()

    async def select(self, request: WorkerSelectionRequest) -> Worker:
        author = self._resolve_author(request.author_worker_id)

        candidates = self._filter_by_capabilities_and_governance(request, author)
        diagnostics = await self._diagnose_candidate_providers(candidates)
        available_by_provider = {d.provider: d.available for d in diagnostics}
        eligible = [w for w in candidates if available_by_provider.get(w.provider, False)]

        if not eligible:
            raise NoEligibleWorkerError(request, diagnostics=diagnostics)

        if author is None:
            return self._pick_best(eligible)

        cross_provider = [w for w in eligible if w.provider != author.provider]

        if self._policy.require_distinct_provider_for_review:
            if not cross_provider:
                raise ReviewIndependenceError(author, diagnostics=diagnostics)
            return self._pick_best(cross_provider)

        if self._policy.prefer_distinct_provider_for_review and cross_provider:
            return self._pick_best(cross_provider)

        return self._pick_best(eligible)

    def _resolve_author(self, author_worker_id: str | None) -> Worker | None:
        if author_worker_id is None:
            return None
        author = self._by_id.get(author_worker_id)
        if author is None:
            raise UnknownWorkerError(author_worker_id)
        return author

    def _filter_by_capabilities_and_governance(
        self, request: WorkerSelectionRequest, author: Worker | None
    ) -> list[Worker]:
        excluded = set(request.excluded_worker_ids)
        if author is not None:
            # Absolute minimum guarantee: a worker never reviews itself.
            excluded.add(author.worker_id)
        return [
            worker
            for worker in self._workers
            if worker.worker_id not in excluded
            and request.required_capabilities <= worker.capabilities
        ]

    async def _diagnose_candidate_providers(
        self, candidates: Sequence[Worker]
    ) -> tuple[ProviderSelectionDiagnostic, ...]:
        providers = sorted({worker.provider for worker in candidates})
        if not providers:
            return ()
        results = await asyncio.gather(
            *(self._quota_manager.get(provider) for provider in providers),
            return_exceptions=True,
        )
        diagnostics: list[ProviderSelectionDiagnostic] = []
        for provider, result in zip(providers, results):
            if isinstance(result, ProviderProbeError):
                # Expected provider-level failure: not available for this
                # selection, but other providers are still evaluated.
                diagnostics.append(
                    ProviderSelectionDiagnostic(provider=provider, available=False, reason="probe_error")
                )
            elif isinstance(result, BaseException):
                # Programming failure, not a provider failure: never
                # swallowed or turned into "not available".
                raise result
            else:
                available = result.availability.available is True
                reason = "available" if available else (
                    result.availability.reason.value if result.availability.reason else "unknown"
                )
                reset_at = tuple(w.reset_at for w in result.quota_windows if w.reset_at is not None)
                diagnostics.append(
                    ProviderSelectionDiagnostic(
                        provider=provider, available=available, reason=reason, reset_at=reset_at
                    )
                )
        return tuple(diagnostics)

    def _pick_best(self, candidates: Sequence[Worker]) -> Worker:
        # Deterministic, documented tie-break: highest priority first, then
        # ascending worker_id lexical order for equal priority.
        return sorted(candidates, key=lambda w: (-w.priority, w.worker_id))[0]
