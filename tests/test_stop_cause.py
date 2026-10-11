"""Hotfix #58 — evidence-based classification of a non-successful stop."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.execution_store import ExecutionRecord, ExecutionStatus
from orchestrator.ralph_execution_engine import ExecutionResult, RalphEvent
from orchestrator.stop_cause import StopCause, classify_stop

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
MAX_ITER = "## Reason\nmax_iterations\n\n## Summary\n- Iterations: 5"


def _result(status=ExecutionStatus.FAILED, events=(), anomaly=None, exit_code=0) -> ExecutionResult:
    record = ExecutionRecord(
        execution_id="e", task_id="t", worker_id="w", provider="p", backend="b", model="m",
        role="developer", started_at=NOW, status=status,
    )
    return ExecutionResult(
        record=record, exit_code=exit_code, event_anomaly=anomaly,
        events=tuple(RalphEvent(topic=t, timestamp=NOW, payload=p) for t, p in events),
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(ExecutionStatus.INTERRUPTED), StopCause.TIMEOUT),
        (_result(events=[("loop.terminate", MAX_ITER)]), StopCause.MAX_ITERATIONS),
        (_result(events=[("work.failed", "no")]), StopCause.EXPLICIT_FAILURE),
        # a genuine work.failed wins even when Ralph also hit max_iterations
        (_result(events=[("work.failed", "no"), ("loop.terminate", MAX_ITER)]), StopCause.EXPLICIT_FAILURE),
        (_result(anomaly="stale_events"), StopCause.INCONSISTENT_EVENTS),
        (_result(events=[("work.failed", ""), ("work.completed", "")], anomaly="conflicting_terminal_events"),
         StopCause.INCONSISTENT_EVENTS),
        (_result(exit_code=0), StopCause.TECHNICAL_ERROR),  # exit 0, no event: no success invented
        (_result(exit_code=1, events=[("loop.terminate", "garbled")]), StopCause.TECHNICAL_ERROR),
    ],
)
def test_classification(result, expected) -> None:
    assert classify_stop(result) is expected


def test_only_an_explicit_failure_is_not_recoverable() -> None:
    assert {c for c in StopCause if not c.recoverable} == {StopCause.EXPLICIT_FAILURE}
