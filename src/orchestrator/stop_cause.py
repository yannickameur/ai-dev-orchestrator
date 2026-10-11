"""Stop-cause classification for a development execution that did not succeed.

Pure and evidence-driven (hotfix #58): the cause is derived only from the
persisted ``ExecutionResult`` — execution status, attributed Ralph events and
Ralph's own ``loop.terminate`` payload — never from an agent's prose or from
``exit_code`` alone. Provider quota exhaustion is deliberately *not* detected
here: ``WorkerSelector``/``QuotaManager`` own provider truth and exclude an
exhausted provider when the recovery attempt re-selects a worker.
"""

from __future__ import annotations

import re
from enum import Enum

from orchestrator.execution_store import ExecutionStatus
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent, _determine_verdict

#: Same topics MVPManager hands to the engine (``work.completed``/``work.failed``).
_SUCCESS = frozenset({"work.completed"})
_FAILURE = frozenset({"work.failed"})


_RALPH_REASON_RE = re.compile(r"^## Reason\s*\n\s*(\S+)", re.MULTILINE)
_RALPH_ITERATIONS_RE = re.compile(r"^- Iterations:\s*(\d+)", re.MULTILINE)
_PUBLIC_RALPH_REASONS = frozenset({"max_iterations", "max_runtime_seconds"})


def ralph_termination(events: tuple[RalphEvent, ...]) -> tuple[str | None, int | None]:
    """Reason/iteration count of the last readable ``loop.terminate``
    event, each ``None`` when Ralph's payload does not actually state it."""
    for event in reversed(events):
        if event.topic != "loop.terminate":
            continue
        payload = event.payload or ""
        reason = _RALPH_REASON_RE.search(payload)
        iterations = _RALPH_ITERATIONS_RE.search(payload)
        count = int(iterations.group(1)) if iterations else event.iteration
        public_reason = reason.group(1) if reason else None
        return (public_reason if public_reason in _PUBLIC_RALPH_REASONS else None), count
    return None, None


class StopCause(str, Enum):
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    TECHNICAL_ERROR = "technical_error"
    INCONSISTENT_EVENTS = "inconsistent_events"
    #: An attributed, valid ``work.failed``: a genuine business failure.
    EXPLICIT_FAILURE = "explicit_failure"

    @property
    def recoverable(self) -> bool:
        return self is not StopCause.EXPLICIT_FAILURE


def classify_stop(result: ExecutionResult) -> StopCause:
    if result.record.status is ExecutionStatus.INTERRUPTED:
        return StopCause.TIMEOUT
    if result.event_anomaly is not None:
        return StopCause.INCONSISTENT_EVENTS
    if _determine_verdict(result.events, success_topics=_SUCCESS, failure_topics=_FAILURE) is False:
        return StopCause.EXPLICIT_FAILURE
    if ralph_termination(result.events)[0] == "max_iterations":
        return StopCause.MAX_ITERATIONS
    return StopCause.TECHNICAL_ERROR
