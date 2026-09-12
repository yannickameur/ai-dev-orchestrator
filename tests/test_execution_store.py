"""Tests for ExecutionStore / ExecutionRecord (Phase 1 / Slice 5).

All tests are offline and local: a real sqlite3 file under pytest's
``tmp_path``, no network, no subprocess, no Claude/Codex/Ralph/Git
invocation anywhere in this file.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator import execution_store as execution_store_module
from orchestrator.execution_store import (
    CorruptExecutionRecordError,
    DuplicateExecutionError,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStore,
    InvalidTransitionError,
    UnknownExecutionError,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
NAIVE_NOW = datetime(2026, 9, 12, 15, 0)


def _store(tmp_path: Path, *, clock=None) -> ExecutionStore:
    return ExecutionStore(tmp_path / "executions.sqlite3", clock=clock or (lambda: UTC_NOW))


def _create_running(store: ExecutionStore, **overrides) -> ExecutionRecord:
    fields = dict(
        execution_id="exec-001",
        task_id="task-001",
        worker_id="claude_dev_01",
        provider="anthropic",
        backend="claude_code",
        model="sonnet",
        role="developer",
    )
    fields.update(overrides)
    return store.create(**fields)


class TestCreateAndRead:
    def test_create_yields_a_running_execution(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(store)

        assert record.status is ExecutionStatus.RUNNING
        assert record.execution_id == "exec-001"
        assert record.finished_at is None
        assert record.exit_code is None

    def test_get_after_reopening_the_store_returns_the_same_data(self, tmp_path: Path) -> None:
        db_path = tmp_path / "executions.sqlite3"
        store = ExecutionStore(db_path, clock=lambda: UTC_NOW)
        _create_running(store)
        store.close()

        reopened = ExecutionStore(db_path, clock=lambda: UTC_NOW)
        record = reopened.get("exec-001")

        assert record.execution_id == "exec-001"
        assert record.status is ExecutionStatus.RUNNING
        assert record.started_at == UTC_NOW

    def test_started_at_must_be_timezone_aware(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        with pytest.raises(ValueError, match="timezone-aware"):
            _create_running(store, started_at=NAIVE_NOW)

    def test_reasoning_effort_defaults_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(store)
        assert record.reasoning_effort is None

    def test_reasoning_effort_can_be_set(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(
            store,
            worker_id="codex_dev_01", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="high",
        )
        assert record.reasoning_effort == "high"

    def test_full_worker_provider_model_snapshot_is_captured(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(
            store,
            worker_id="codex_dev_01", provider="openai", backend="codex",
            model="gpt-5.6-terra", reasoning_effort="high", role="reviewer",
        )
        fetched = store.get("exec-001")

        assert fetched.worker_id == "codex_dev_01"
        assert fetched.provider == "openai"
        assert fetched.backend == "codex"
        assert fetched.model == "gpt-5.6-terra"
        assert fetched.reasoning_effort == "high"
        assert fetched.role == "reviewer"
        assert fetched == record

    def test_unknown_execution_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownExecutionError):
            store.get("does-not-exist")

    def test_duplicate_execution_id_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        with pytest.raises(DuplicateExecutionError):
            _create_running(store)

    def test_git_sha_before_optional_and_after_absent_until_terminal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(store, git_sha_before="abc123")
        assert record.git_sha_before == "abc123"
        assert record.git_sha_after is None

    def test_session_ids_are_optional(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(
            store, provider_session_id="sess-xyz", ralph_loop_id="loop-42"
        )
        assert record.provider_session_id == "sess-xyz"
        assert record.ralph_loop_id == "loop-42"

    def test_session_ids_default_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _create_running(store)
        assert record.provider_session_id is None
        assert record.ralph_loop_id is None


class TestExitCodeIsAuditOnly:
    def test_exit_code_is_preserved_but_does_not_set_status(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)

        # A non-zero exit code alone must never imply FAILED automatically;
        # only an explicit mark_* call changes status.
        updated = store.mark_succeeded("exec-001", exit_code=2)

        assert updated.exit_code == 2
        assert updated.status is ExecutionStatus.SUCCEEDED

    def test_exit_code_zero_with_explicit_failed_stays_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)

        updated = store.mark_failed("exec-001", exit_code=0)

        assert updated.exit_code == 0
        assert updated.status is ExecutionStatus.FAILED


class TestTransitions:
    def test_running_to_succeeded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        updated = store.mark_succeeded("exec-001", exit_code=0, git_sha_after="deadbee")

        assert updated.status is ExecutionStatus.SUCCEEDED
        assert updated.git_sha_after == "deadbee"
        assert updated.finished_at is not None

    def test_running_to_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        updated = store.mark_failed("exec-001", exit_code=1)

        assert updated.status is ExecutionStatus.FAILED

    def test_running_to_interrupted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        updated = store.mark_interrupted("exec-001")

        assert updated.status is ExecutionStatus.INTERRUPTED
        assert updated.finished_at is not None

    def test_running_to_recovery_required(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        updated = store.mark_recovery_required("exec-001")

        assert updated.status is ExecutionStatus.RECOVERY_REQUIRED

    @pytest.mark.parametrize(
        "terminal_setter",
        ["mark_succeeded", "mark_failed", "mark_interrupted", "mark_recovery_required"],
    )
    def test_terminal_execution_cannot_be_transitioned_again(
        self, tmp_path: Path, terminal_setter: str
    ) -> None:
        store = _store(tmp_path)
        _create_running(store)
        store.mark_succeeded("exec-001")

        with pytest.raises(InvalidTransitionError):
            getattr(store, terminal_setter)("exec-001")

    def test_identity_fields_are_unchanged_across_a_transition(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        original = _create_running(store, git_sha_before="abc123")
        updated = store.mark_succeeded("exec-001", git_sha_after="def456")

        assert updated.execution_id == original.execution_id
        assert updated.task_id == original.task_id
        assert updated.worker_id == original.worker_id
        assert updated.provider == original.provider
        assert updated.backend == original.backend
        assert updated.model == original.model
        assert updated.reasoning_effort == original.reasoning_effort
        assert updated.role == original.role
        assert updated.started_at == original.started_at
        assert updated.git_sha_before == original.git_sha_before  # never touched by transition


class TestListRunning:
    def test_list_running_returns_only_running_executions(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store, execution_id="exec-running")
        _create_running(store, execution_id="exec-done")
        store.mark_succeeded("exec-done")

        running = store.list_running()

        assert [r.execution_id for r in running] == ["exec-running"]

    def test_list_running_empty_when_none_running(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        store.mark_failed("exec-001")

        assert store.list_running() == []

    def test_multiple_executions_of_the_same_task_are_all_listed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store, execution_id="exec-a", task_id="task-shared")
        _create_running(store, execution_id="exec-b", task_id="task-shared")

        running = store.list_running()

        assert {r.execution_id for r in running} == {"exec-a", "exec-b"}
        assert all(r.task_id == "task-shared" for r in running)

    def test_multiple_executions_of_the_same_worker_are_all_listed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store, execution_id="exec-a", worker_id="claude_dev_01")
        _create_running(store, execution_id="exec-b", worker_id="claude_dev_01")

        running = store.list_running()

        assert {r.execution_id for r in running} == {"exec-a", "exec-b"}
        assert all(r.worker_id == "claude_dev_01" for r in running)


class TestRecoveryAfterRestart:
    def test_running_execution_survives_restart_and_can_be_marked_recovery_required(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "executions.sqlite3"

        store = ExecutionStore(db_path, clock=lambda: UTC_NOW)
        _create_running(store)
        # Simulate a crash: the process/store goes away without a terminal
        # transition ever being recorded.
        store.close()

        # A new process reopens the same store.
        reopened = ExecutionStore(db_path, clock=lambda: UTC_NOW + timedelta(minutes=5))
        still_running = reopened.list_running()
        assert [r.execution_id for r in still_running] == ["exec-001"]
        assert still_running[0].status is ExecutionStatus.RUNNING

        # No automatic retry ever happened: it is still exactly RUNNING,
        # identical to what was persisted before the crash, until an
        # explicit decision is made here.
        updated = reopened.mark_recovery_required("exec-001")
        assert updated.status is ExecutionStatus.RECOVERY_REQUIRED
        assert reopened.list_running() == []

        # The recovery decision itself survives a further restart.
        reopened.close()
        final = ExecutionStore(db_path, clock=lambda: UTC_NOW)
        assert final.get("exec-001").status is ExecutionStatus.RECOVERY_REQUIRED


class TestCorruptData:
    def test_corrupt_status_value_raises_on_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        store._conn.execute(  # noqa: SLF001 - deliberately bypassing the public API to corrupt data
            "UPDATE executions SET status = 'not_a_real_status' WHERE execution_id = ?",
            ("exec-001",),
        )
        store._conn.commit()

        with pytest.raises(CorruptExecutionRecordError):
            store.get("exec-001")

    def test_corrupt_timestamp_raises_on_list_running(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _create_running(store)
        store._conn.execute(
            "UPDATE executions SET started_at = 'not-a-timestamp' WHERE execution_id = ?",
            ("exec-001",),
        )
        store._conn.commit()

        with pytest.raises(CorruptExecutionRecordError):
            store.list_running()


class TestNoRealExecutionDependency:
    def test_module_never_shells_out(self) -> None:
        source = inspect.getsource(execution_store_module)
        for forbidden in (
            "import subprocess", "asyncio.create_subprocess", "Popen",
            "ClaudeCodeAdapter", "CodexAdapter", "WorkerSelector",
        ):
            assert forbidden not in source
