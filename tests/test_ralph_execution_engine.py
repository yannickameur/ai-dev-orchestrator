"""Tests for RalphExecutionEngine (Phase 1 / Slice 6).

All normal tests are offline: an injected fake subprocess runner stands in
for the real `ralph` binary — no real Ralph/Claude/Codex invocation. The
fake runner simulates what a real `ralph run` would leave behind
(`.ralph/current-loop-id`, `.ralph/current-events`, an events JSONL file)
so the engine's own event-reading/verdict logic is exercised for real.

Three fixtures under tests/fixtures/ralph_events/ are real traces captured
during the Phase 0.5 Ralph spike (~/projects/ralph-spike), cleaned of
nothing sensitive (they contain no secrets) and reused here instead of
re-running Claude/Codex.

The one real subprocess this file uses is the local `git` binary, to
verify actual before/after SHA capture — no Claude/Codex/Ralph/network
involved.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.execution_policy import ExecutionPermissionMode
from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    RalphEventParseError,
    RalphExecutionEngine,
    RalphLaunchError,
    ReservedEventTopicError,
    UnsupportedBackendError,
    UnsupportedPermissionModeError,
    UnsupportedProfileOptionError,
    UntrackedFileChange,
    UntrackedFileChangeKind,
    WorkerCommitIdentityMismatchError,
    _audit_worker_commit_identity,
    _claude_code_permission_args,
    _codex_permission_args,
    _default_subprocess_runner,
    _worker_git_identity_env,
    parse_ralph_events,
    scoped_worker_git_identity,
)
from orchestrator.worker_selector import Worker

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ralph_events"
UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def _alice(**overrides) -> Worker:
    fields = dict(
        worker_id="claude_dev_01", display_name="Alice", provider="anthropic",
        backend="claude_code", model="sonnet", capabilities=frozenset({"developer"}),
    )
    fields.update(overrides)
    return Worker.with_single_profile(**fields)


def _victor(**overrides) -> Worker:
    fields = dict(
        worker_id="codex_dev_01", display_name="Victor", provider="openai",
        backend="codex", model="gpt-5.6-terra", reasoning_effort="high",
        capabilities=frozenset({"developer"}),
    )
    fields.update(overrides)
    return Worker.with_single_profile(**fields)


def _milo(**overrides) -> Worker:
    fields = dict(
        worker_id="mistral_dev_01", display_name="Milo", provider="mistral",
        backend="vibe", model="vibe-default", capabilities=frozenset({"developer"}),
    )
    fields.update(overrides)
    return Worker.with_single_profile(**fields)


def _request(tmp_path: Path, **overrides) -> ExecutionRequest:
    worker = overrides.get("worker", _alice())
    profile = worker.profile()
    fields = dict(
        execution_id="exec-001",
        task_id="task-001",
        worker=worker,
        role="developer",
        workspace=tmp_path,
        instructions="Do the thing.",
        initial_event_topic="work.start",
        success_topics=frozenset({"work.completed"}),
        failure_topics=frozenset({"work.failed"}),
        timeout_seconds=30.0,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
    )
    fields.update(overrides)
    return ExecutionRequest(**fields)


def _store(tmp_path: Path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "executions.sqlite3", clock=lambda: UTC_NOW)


def _event_line(topic: str, *, payload=None, ts: str = "2026-09-12T15:00:00.000000+00:00", iteration=None, hat=None) -> str:
    obj = {"topic": topic, "ts": ts, "payload": payload}
    if iteration is not None:
        obj["iteration"] = iteration
    if hat is not None:
        obj["hat"] = hat
    return json.dumps(obj)


def _make_fake_runner(
    *,
    loop_id: str | None = "primary-20260912-150000",
    events_lines: list[str] | None = None,
    events_from_fixture: str | None = None,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    raise_exc: BaseException | None = None,
    on_call=None,
):
    async def _runner(args, cwd, timeout):
        if on_call is not None:
            on_call(args, cwd, timeout)
        if raise_exc is not None:
            raise raise_exc
        ralph_dir = Path(cwd) / ".ralph"
        ralph_dir.mkdir(parents=True, exist_ok=True)
        if loop_id is not None:
            (ralph_dir / "current-loop-id").write_text(loop_id)
        lines = events_lines
        if events_from_fixture is not None:
            lines = (FIXTURES_DIR / events_from_fixture).read_text().splitlines()
        if lines is not None:
            events_filename = "events-test.jsonl"
            (ralph_dir / "current-events").write_text(f".ralph/{events_filename}")
            (ralph_dir / events_filename).write_text("\n".join(lines) + "\n")
        return exit_code, stdout, stderr

    return _runner


class TestParseRalphEventsOffline:
    def test_tolerates_optional_iteration_and_hat(self) -> None:
        lines = [_event_line("review.ready", payload="review_candidate.py")]
        events = parse_ralph_events(lines)
        assert events[0].topic == "review.ready"
        assert events[0].iteration is None
        assert events[0].hat is None

    def test_nanosecond_timestamp_is_parsed(self) -> None:
        lines = ['{"payload":"review_candidate.py","topic":"review.ready","ts":"2026-09-12T13:30:55.703784543+00:00"}']
        events = parse_ralph_events(lines)
        assert events[0].timestamp.tzinfo is not None

    def test_invalid_json_line_raises(self) -> None:
        with pytest.raises(RalphEventParseError):
            parse_ralph_events(["{not valid json"])

    def test_missing_topic_raises(self) -> None:
        with pytest.raises(RalphEventParseError):
            parse_ralph_events(['{"ts": "2026-09-12T15:00:00+00:00"}'])

    def test_real_author_review_fixture_parses(self) -> None:
        lines = (FIXTURES_DIR / "author_review_rejected.jsonl").read_text().splitlines()
        events = parse_ralph_events(lines)
        topics = [e.topic for e in events]
        assert "review.ready" in topics
        assert "review.rejected" in topics

    def test_real_loop_complete_without_business_event_fixture_parses(self) -> None:
        lines = (FIXTURES_DIR / "loop_complete_without_business_event.jsonl").read_text().splitlines()
        events = parse_ralph_events(lines)
        # LOOP_COMPLETE appears, but it is not a business event this engine
        # trusts on its own.
        assert any(e.topic == "LOOP_COMPLETE" for e in events)


class TestExecutionRequestValidation:
    def test_task_start_as_initial_event_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ReservedEventTopicError):
            _request(tmp_path, initial_event_topic="task.start")

    def test_task_resume_as_success_topic_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ReservedEventTopicError):
            _request(tmp_path, success_topics=frozenset({"task.resume"}))

    def test_overlapping_success_and_failure_topics_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="overlap"):
            _request(
                tmp_path,
                success_topics=frozenset({"work.done"}),
                failure_topics=frozenset({"work.done"}),
            )

    def test_non_positive_timeout_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="positive"):
            _request(tmp_path, timeout_seconds=0)


class TestRecordCreatedBeforeSubprocess:
    def test_execution_record_is_running_when_subprocess_runner_is_invoked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        seen_status = {}

        def _on_call(args, cwd, timeout):
            seen_status["status"] = store.get("exec-001").status

        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path)))

        assert seen_status["status"] is ExecutionStatus.RUNNING


class TestWorkerSnapshotAndTranslation:
    def test_full_worker_snapshot_is_captured(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_victor(), role="reviewer")))

        assert result.record.worker_id == "codex_dev_01"
        assert result.record.provider == "openai"
        assert result.record.backend == "codex"
        assert result.record.model == "gpt-5.6-terra"
        assert result.record.reasoning_effort == "high"
        assert result.record.role == "reviewer"

    def test_backend_and_model_are_transmitted_to_ralph_config(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            config_path = Path(args[args.index("-c") + 1])
            captured["config"] = config_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path, worker=_victor())))

        assert 'backend: "codex"' in captured["config"]
        assert "gpt-5.6-terra" in captured["config"]

    def test_reasoning_effort_is_transmitted_for_codex(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            hats_path = Path(args[args.index("-H") + 1])
            captured["hats"] = hats_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path, worker=_victor())))

        assert 'model_reasoning_effort=\\"high\\"' in captured["hats"]

    def test_absence_of_reasoning_effort_is_supported_for_claude(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            hats_path = Path(args[args.index("-H") + 1])
            captured["hats"] = hats_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_alice())))

        assert result.record.reasoning_effort is None
        assert "reasoning_effort" not in captured["hats"]

    def test_reasoning_effort_is_transmitted_for_claude_via_effort_flag(self, tmp_path: Path) -> None:
        """`claude --effort <level>` is a real, native Claude Code CLI flag
        (confirmed via `claude --help` on this machine — distinct from
        codex's `-c model_reasoning_effort=` override style). A Claude
        profile that does set reasoning_effort must transmit it through the
        same hat backend-args mechanism already validated for codex, never
        silently drop it.
        """
        captured = {}

        def _on_call(args, cwd, timeout):
            hats_path = Path(args[args.index("-H") + 1])
            captured["hats"] = hats_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        worker = _alice(reasoning_effort="high")
        result = asyncio.run(engine.execute(_request(tmp_path, worker=worker)))

        assert result.record.reasoning_effort == "high"
        assert '"--effort"' in captured["hats"]
        assert '"high"' in captured["hats"]
        # Never the codex-style config-override syntax for this backend.
        assert "model_reasoning_effort" not in captured["hats"]

    def test_initial_custom_event_is_transmitted(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            hats_path = Path(args[args.index("-H") + 1])
            captured["hats"] = hats_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(
            engine.execute(_request(tmp_path, initial_event_topic="probe.start"))
        )

        assert 'starting_event: "probe.start"' in captured["hats"]

    def test_unsupported_backend_raises_and_finalizes_record_as_failed(self, tmp_path: Path) -> None:
        # This must be caught before the record is left dangling RUNNING:
        # the failure happens while building the config, before any
        # subprocess is even launched.
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)
        weird_worker = _alice(worker_id="w2", backend="gemini_cli", model="gemini-pro")

        with pytest.raises(UnsupportedBackendError):
            asyncio.run(engine.execute(_request(tmp_path, worker=weird_worker, execution_id="exec-002")))

        assert store.get("exec-002").status is ExecutionStatus.FAILED

    def test_cwd_passed_to_subprocess_is_the_workspace(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            captured["cwd"] = cwd

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path)))

        assert captured["cwd"] == tmp_path


class TestVibeBackendMapping:
    """Vibe (post-MVP 0.1, see docs/VIBE_SPIKE.md) is the first backend
    that cannot use Ralph's hats mechanism at all (VERIFIED by the spike:
    Ralph's hats reject any non-native backend type). These tests prove
    the solo-mode path this engine falls back to for it, and that every
    other (native) backend's existing path is completely untouched."""

    def test_vibe_backend_omits_hats_flag_entirely(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            captured["args"] = list(args)

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path, worker=_milo())))

        assert "-H" not in captured["args"]

    def test_native_backend_still_receives_hats_flag(self, tmp_path: Path) -> None:
        """Regression guard: the vibe/solo-mode branch must never affect
        the existing native-backend (claude/codex) path."""
        captured = {}

        def _on_call(args, cwd, timeout):
            captured["args"] = list(args)

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path, worker=_alice())))

        assert "-H" in captured["args"]

    def test_vibe_config_uses_custom_backend_with_bridge_command(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            config_path = Path(args[args.index("-c") + 1])
            captured["config"] = config_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path, worker=_milo())))

        assert 'backend: "custom"' in captured["config"]
        assert "vibe_ralph_bridge.py" in captured["config"]
        assert '"--model", "vibe-default"' in captured["config"]

    def test_vibe_prompt_and_cwd_still_delivered_like_every_other_backend(self, tmp_path: Path) -> None:
        captured = {}

        def _on_call(args, cwd, timeout):
            captured["cwd"] = cwd
            # Read now: the runtime dir (holding PROMPT.md) is removed as
            # soon as the (simulated) subprocess call returns.
            captured["prompt_text"] = Path(args[args.index("-P") + 1]).read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(
            engine.execute(_request(tmp_path, worker=_milo(), instructions="Do the vibe thing."))
        )

        assert captured["cwd"] == tmp_path
        assert captured["prompt_text"] == "Do the vibe thing."

    def test_vibe_result_is_normalized_through_the_same_execution_result(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_milo())))

        assert result.record.worker_id == "mistral_dev_01"
        assert result.record.provider == "mistral"
        assert result.record.backend == "vibe"
        assert result.record.model == "vibe-default"
        assert result.record.status is ExecutionStatus.SUCCEEDED

    def test_vibe_reasoning_effort_is_rejected_not_silently_dropped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)
        worker = _milo(reasoning_effort="high")

        with pytest.raises(UnsupportedProfileOptionError):
            asyncio.run(engine.execute(_request(tmp_path, worker=worker, execution_id="exec-vibe-re")))

        assert store.get("exec-vibe-re").status is ExecutionStatus.FAILED

    def test_vibe_timeout_is_handled_identically_to_native_backends(self, tmp_path: Path) -> None:
        from orchestrator.ralph_execution_engine import RalphTimeoutError

        store = _store(tmp_path)
        runner = _make_fake_runner(raise_exc=RalphTimeoutError("timed out"))
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_milo(), execution_id="exec-vibe-to")))

        assert result.record.status is ExecutionStatus.INTERRUPTED

    def test_vibe_git_sha_is_captured_only_after_process_completion(self, tmp_path: Path) -> None:
        """No special-casing for vibe: git facts are still only read
        before-launch and after-the-whole-subprocess-returns, exactly like
        every other backend (mirrors TestGitShaCapture, worker=_milo())."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
        (tmp_path / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
        sha_before_expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()

        def _on_call(args, cwd, timeout):
            (Path(cwd) / "work.txt").write_text("done\n")
            subprocess.run(["git", "add", "work.txt"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "vibe work"], cwd=cwd, check=True)

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_milo())))
        sha_after_expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()

        assert result.record.git_sha_before == sha_before_expected
        assert result.record.git_sha_after == sha_after_expected
        assert result.record.git_sha_before != result.record.git_sha_after


class TestBusinessVerdict:
    def test_success_event_yields_succeeded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], exit_code=0)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.status is ExecutionStatus.SUCCEEDED

    def test_failure_event_yields_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.failed")], exit_code=1)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.status is ExecutionStatus.FAILED

    def test_exit_code_2_with_success_event_still_succeeds_and_keeps_exit_code(
        self, tmp_path: Path
    ) -> None:
        # Mirrors the real spike case: exit_code=2/max_iterations, but a
        # genuine business success event was published.
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], exit_code=2)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.status is ExecutionStatus.SUCCEEDED
        assert result.record.exit_code == 2

    def test_exit_code_zero_without_terminal_event_is_never_succeeded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(
            events_lines=[_event_line("iteration.summary", iteration=1, hat="loop")], exit_code=0
        )
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.status is ExecutionStatus.FAILED
        assert result.record.exit_code == 0

    def test_real_author_review_fixture_yields_failed_via_business_event(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_from_fixture="author_review_rejected.jsonl", exit_code=2)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(
            engine.execute(
                _request(
                    tmp_path,
                    success_topics=frozenset({"review.approved"}),
                    failure_topics=frozenset({"review.rejected"}),
                )
            )
        )

        assert result.record.status is ExecutionStatus.FAILED
        assert result.record.exit_code == 2

    def test_real_loop_complete_fixture_never_succeeds_without_business_event(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(
            events_from_fixture="loop_complete_without_business_event.jsonl", exit_code=2
        )
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        # LOOP_COMPLETE is not "work.completed"/"work.failed": fail-closed.
        assert result.record.status is ExecutionStatus.FAILED

    def test_real_probe_fixture_succeeds_despite_zeroed_metrics_and_nonzero_exit(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(
            events_from_fixture="probe_success_despite_nonzero_exit.jsonl", exit_code=2
        )
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(
            engine.execute(
                _request(
                    tmp_path,
                    initial_event_topic="backend.test",
                    success_topics=frozenset({"probe.done"}),
                    failure_topics=frozenset({"probe.failed"}),
                )
            )
        )

        assert result.record.status is ExecutionStatus.SUCCEEDED
        assert result.record.exit_code == 2


class TestInvalidEventsAndFailureModes:
    def test_invalid_jsonl_raises_and_finalizes_as_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=["{not valid json"], exit_code=0)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        with pytest.raises(RalphEventParseError):
            asyncio.run(engine.execute(_request(tmp_path)))

        assert store.get("exec-001").status is ExecutionStatus.FAILED

    def test_timeout_yields_interrupted_without_raising(self, tmp_path: Path) -> None:
        from orchestrator.ralph_execution_engine import RalphTimeoutError

        store = _store(tmp_path)
        runner = _make_fake_runner(raise_exc=RalphTimeoutError("timed out"))
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.status is ExecutionStatus.INTERRUPTED

    def test_ralph_binary_not_found_raises_launch_error_and_finalizes_failed(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(raise_exc=FileNotFoundError("no such file: ralph"))
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        with pytest.raises(RalphLaunchError):
            asyncio.run(engine.execute(_request(tmp_path)))

        assert store.get("exec-001").status is ExecutionStatus.FAILED

    def test_ralph_loop_id_is_captured_on_success(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(
            loop_id="primary-20260912-150000", events_lines=[_event_line("work.completed")]
        )
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.ralph_loop_id == "primary-20260912-150000"


class TestGitShaCapture:
    def _init_git_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        (path / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)

    def _head_sha(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def test_git_sha_before_and_after_are_captured_across_a_real_commit(self, tmp_path: Path) -> None:
        self._init_git_repo(tmp_path)
        sha_before_expected = self._head_sha(tmp_path)

        def _on_call(args, cwd, timeout):
            (Path(cwd) / "work.txt").write_text("done\n")
            subprocess.run(["git", "add", "work.txt"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=cwd, check=True)

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))
        sha_after_expected = self._head_sha(tmp_path)

        assert result.record.git_sha_before == sha_before_expected
        assert result.record.git_sha_after == sha_after_expected
        assert result.record.git_sha_before != result.record.git_sha_after

    def test_non_git_workspace_yields_none_shas(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path)))

        assert result.record.git_sha_before is None
        assert result.record.git_sha_after is None


class TestWorkerGitIdentity:
    """Slice 25: worker Git commits are attributed by ``display_name``,
    never by a provider/vendor name, and never via a global git config
    mutation — see ``_worker_git_identity_env``."""

    def test_alice_maps_to_author_alice(self) -> None:
        env = _worker_git_identity_env(_alice())
        assert env["GIT_AUTHOR_NAME"] == "Alice"
        assert env["GIT_COMMITTER_NAME"] == "Alice"

    def test_victor_maps_to_author_victor(self) -> None:
        env = _worker_git_identity_env(_victor())
        assert env["GIT_AUTHOR_NAME"] == "Victor"
        assert env["GIT_COMMITTER_NAME"] == "Victor"

    def test_email_is_deterministic_and_technical(self) -> None:
        env = _worker_git_identity_env(_alice())
        assert env["GIT_AUTHOR_EMAIL"] == "claude_dev_01@workers.ai-dev-orchestrator.local"
        assert env["GIT_COMMITTER_EMAIL"] == env["GIT_AUTHOR_EMAIL"]
        # deterministic: calling it again for the same worker is identical
        assert _worker_git_identity_env(_alice()) == env

    def test_different_workers_get_different_identities(self) -> None:
        assert _worker_git_identity_env(_alice()) != _worker_git_identity_env(_victor())
        assert _worker_git_identity_env(_alice()) != _worker_git_identity_env(_milo())

    def test_never_a_provider_or_vendor_name(self) -> None:
        # The author *name* (never a provider/vendor name) is the actual
        # requirement here — the email is derived from worker_id, a
        # project's own config choice (e.g. this file's fixtures use
        # "claude_dev_01" for unrelated historical reasons; real
        # config/workers.yaml worker_ids are plain first names like
        # "alice"/"victor").
        for worker in (_alice(), _victor(), _milo()):
            env = _worker_git_identity_env(worker)
            name_blob = f"{env['GIT_AUTHOR_NAME']} {env['GIT_COMMITTER_NAME']}".lower()
            for forbidden in ("claude", "anthropic", "codex", "openai", "mistral", "vibe"):
                assert forbidden not in name_blob

    def test_never_claims_a_real_github_account(self) -> None:
        # A clearly non-human, non-guessable email domain — never
        # something that could be mistaken for the maintainer's own
        # GitHub-linked email or a real human account.
        for worker in (_alice(), _victor(), _milo()):
            env = _worker_git_identity_env(worker)
            assert env["GIT_AUTHOR_EMAIL"].endswith("@workers.ai-dev-orchestrator.local")


class TestWorkerGitIdentityRealCommit:
    """Uses the real ``git`` binary (like ``TestGitShaCapture`` above) to
    prove the identity actually lands on a real commit, scoped to that
    one subprocess only — never a global git config mutation."""

    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        # Deliberately configured to a *different* identity: proves the
        # env-supplied worker identity is what actually wins, not
        # whatever the repo/global config already says.
        subprocess.run(["git", "config", "user.email", "someone-else@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Someone Else"], cwd=path, check=True)
        (path / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)

    def _last_author(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"], cwd=path, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def test_alice_commit_is_attributed_to_alice(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        asyncio.run(
            _default_subprocess_runner(
                ["git", "commit", "-q", "--allow-empty", "-m", "alice work"], tmp_path, 10.0,
                env=_worker_git_identity_env(_alice()),
            )
        )
        assert self._last_author(tmp_path) == "Alice <claude_dev_01@workers.ai-dev-orchestrator.local>"

    def test_victor_commit_is_attributed_to_victor(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        asyncio.run(
            _default_subprocess_runner(
                ["git", "commit", "-q", "--allow-empty", "-m", "victor work"], tmp_path, 10.0,
                env=_worker_git_identity_env(_victor()),
            )
        )
        assert self._last_author(tmp_path) == "Victor <codex_dev_01@workers.ai-dev-orchestrator.local>"

    def test_global_git_config_is_never_touched(self, tmp_path: Path) -> None:
        before = subprocess.run(
            ["git", "config", "--global", "--list"], capture_output=True, text=True,
        )
        self._init_repo(tmp_path)
        asyncio.run(
            _default_subprocess_runner(
                ["git", "commit", "-q", "--allow-empty", "-m", "alice work"], tmp_path, 10.0,
                env=_worker_git_identity_env(_alice()),
            )
        )
        after = subprocess.run(
            ["git", "config", "--global", "--list"], capture_output=True, text=True,
        )
        assert before.stdout == after.stdout
        assert before.returncode == after.returncode

    def test_repo_local_config_is_also_never_touched(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        before = subprocess.run(
            ["git", "config", "--local", "--list"], cwd=tmp_path, capture_output=True, text=True, check=True,
        )
        asyncio.run(
            _default_subprocess_runner(
                ["git", "commit", "-q", "--allow-empty", "-m", "alice work"], tmp_path, 10.0,
                env=_worker_git_identity_env(_alice()),
            )
        )
        after = subprocess.run(
            ["git", "config", "--local", "--list"], cwd=tmp_path, capture_output=True, text=True, check=True,
        )
        assert before.stdout == after.stdout


class TestDefaultSubprocessRunnerTimeout:
    """AUD-8: the real, production ``_default_subprocess_runner`` timeout/
    kill path — every other test in this file exercises ``RalphTimeoutError``
    only via a fake runner scripted to raise it directly, never the real
    ``asyncio.wait_for``/``process.kill()`` mechanism. A real, local, fast,
    portable slow subprocess (``python -c "... time.sleep(5) ..."``) proves
    the process is genuinely terminated, not merely that the call raises
    while a process keeps running in the background."""

    def test_timeout_actually_kills_the_process_no_orphan(self, tmp_path: Path) -> None:
        from orchestrator.ralph_execution_engine import RalphTimeoutError

        finished_marker = tmp_path / "finished"
        script = (
            "import pathlib, time; "
            "time.sleep(5); "
            f"pathlib.Path({str(finished_marker)!r}).write_text('done')"
        )

        async def _run() -> float:
            loop = asyncio.get_event_loop()
            start = loop.time()
            with pytest.raises(RalphTimeoutError):
                await _default_subprocess_runner([sys.executable, "-c", script], tmp_path, 0.3)
            elapsed = loop.time() - start
            # A genuinely-still-running process gets ample extra time here
            # to finish sleeping and write its post-sleep marker, if the
            # kill above had not actually terminated it.
            await asyncio.sleep(1.5)
            return elapsed

        elapsed = asyncio.run(_run())
        assert elapsed < 2.0  # bounded by the timeout, never anywhere near the 5s sleep
        assert not finished_marker.exists()  # never reached the post-sleep write: genuinely killed


class TestNoForbiddenBehavior:
    def test_no_fallback_or_retry_on_failure(self, tmp_path: Path) -> None:
        calls = []

        def _on_call(args, cwd, timeout):
            calls.append(args)

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.failed")], on_call=_on_call, exit_code=1)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        asyncio.run(engine.execute(_request(tmp_path)))

        assert len(calls) == 1  # exactly one attempt, never retried

    def test_module_never_calls_claude_or_codex_directly(self) -> None:
        import inspect

        from orchestrator import ralph_execution_engine as module

        source = inspect.getsource(module)
        for forbidden in ("ClaudeCodeAdapter", "CodexAdapter", '"claude"\n', "subprocess_exec(\"claude\"", "subprocess_exec(\"codex\""):
            assert forbidden not in source


class TestExecutionPermissionMode:
    """P12 — project-controlled worker execution permission mode.

    All verified argv assertions below are exact, not substring-fuzzy, so
    a STANDARD test can never accidentally pass because an UNRESTRICTED
    flag also happens to appear somewhere in the file.
    """

    def _captured_hats_config(self, tmp_path: Path, *, worker: Worker, permission_mode) -> str:
        captured = {}

        def _on_call(args, cwd, timeout):
            hats_path = Path(args[args.index("-H") + 1])
            captured["hats"] = hats_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(
            store, subprocess_runner=runner, clock=lambda: UTC_NOW, permission_mode=permission_mode,
        )
        asyncio.run(engine.execute(_request(tmp_path, worker=worker)))
        return captured["hats"]

    def _captured_vibe_config(self, tmp_path: Path, *, permission_mode) -> str:
        captured = {}

        def _on_call(args, cwd, timeout):
            config_path = Path(args[args.index("-c") + 1])
            captured["config"] = config_path.read_text()

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(
            store, subprocess_runner=runner, clock=lambda: UTC_NOW, permission_mode=permission_mode,
        )
        asyncio.run(engine.execute(_request(tmp_path, worker=_milo())))
        return captured["config"]

    # --- claude_code -----------------------------------------------------

    def test_claude_code_standard_uses_verified_manual_deny_mechanism(self, tmp_path: Path) -> None:
        hats = self._captured_hats_config(tmp_path, worker=_alice(), permission_mode=ExecutionPermissionMode.STANDARD)
        assert '"--permission-mode", "manual"' in hats
        assert '"--permission-prompts", "none"' in hats
        assert "--dangerously-skip-permissions" not in hats

    def test_claude_code_unrestricted_uses_verified_bypass_mechanism(self, tmp_path: Path) -> None:
        hats = self._captured_hats_config(tmp_path, worker=_alice(), permission_mode=ExecutionPermissionMode.UNRESTRICTED)
        assert '"--dangerously-skip-permissions"' in hats
        assert "--permission-mode" not in hats
        assert "--permission-prompts" not in hats

    # --- codex -------------------------------------------------------------

    def test_codex_standard_uses_verified_sandboxed_no_escalation_mechanism(self, tmp_path: Path) -> None:
        """`--ask-for-approval` is never passed to `codex exec` (a real
        governed run found it rejected by codex-cli 0.157.0's `exec`
        subcommand, exit code 2 — see this function's own docstring):
        `--sandbox workspace-write` alone is the verified, non-hanging
        restriction for STANDARD."""
        hats = self._captured_hats_config(tmp_path, worker=_victor(), permission_mode=ExecutionPermissionMode.STANDARD)
        assert '"--sandbox", "workspace-write"' in hats
        assert "--ask-for-approval" not in hats
        assert "--dangerously-bypass-approvals-and-sandbox" not in hats

    def test_codex_dev_b_regression_ask_for_approval_never_generated(self, tmp_path: Path) -> None:
        """Real incident regression (GitLabPluginRoadmap WI-S0A-01, DEV B
        worker "victor"/openai/codex): `_codex_permission_args(STANDARD)`
        used to include `--ask-for-approval "never"`, which the real,
        installed `codex exec` (codex-cli 0.157.0) rejects outright —
        `error: unexpected argument '--ask-for-approval' found`, exit
        code 2, confirmed directly and via a standalone `ralph run` using
        this exact backend/args combination. That real execution failed
        in ~10ms per attempt (5 iterations, `max_iterations`), never
        reaching the model at all — QA was never even attempted.
        `--ask-for-approval` (in any form) must never again appear in
        the codex backend args this engine generates for any configured
        permission mode."""
        for mode in (ExecutionPermissionMode.STANDARD, ExecutionPermissionMode.UNRESTRICTED):
            args = _codex_permission_args(mode)
            assert not any("ask-for-approval" in a or "ask_for_approval" in a for a in args), (
                f"codex backend args for {mode} still contain an --ask-for-approval "
                f"flag rejected by codex exec: {args!r}"
            )

    def test_codex_unrestricted_uses_verified_bypass_mechanism(self, tmp_path: Path) -> None:
        hats = self._captured_hats_config(tmp_path, worker=_victor(), permission_mode=ExecutionPermissionMode.UNRESTRICTED)
        assert '"--dangerously-bypass-approvals-and-sandbox"' in hats
        assert "--sandbox" not in hats
        assert "--ask-for-approval" not in hats

    # --- vibe (generic mode crosses to the bridge as plain argv) -----------

    def test_vibe_standard_passes_generic_mode_to_bridge(self, tmp_path: Path) -> None:
        config = self._captured_vibe_config(tmp_path, permission_mode=ExecutionPermissionMode.STANDARD)
        assert '"--permission-mode", "standard"' in config

    def test_vibe_unrestricted_passes_generic_mode_to_bridge(self, tmp_path: Path) -> None:
        config = self._captured_vibe_config(tmp_path, permission_mode=ExecutionPermissionMode.UNRESTRICTED)
        assert '"--permission-mode", "unrestricted"' in config

    # --- omitted mode (engine unconfigured) == unchanged legacy behavior --

    def test_omitted_permission_mode_adds_no_flags_for_claude_code(self, tmp_path: Path) -> None:
        hats = self._captured_hats_config(tmp_path, worker=_alice(), permission_mode=None)
        assert "--permission-mode" not in hats
        assert "--dangerously-skip-permissions" not in hats

    def test_omitted_permission_mode_adds_no_flags_for_codex(self, tmp_path: Path) -> None:
        hats = self._captured_hats_config(tmp_path, worker=_victor(), permission_mode=None)
        assert "--sandbox" not in hats
        assert "--dangerously-bypass-approvals-and-sandbox" not in hats

    def test_omitted_permission_mode_adds_no_bridge_flag_for_vibe(self, tmp_path: Path) -> None:
        config = self._captured_vibe_config(tmp_path, permission_mode=None)
        assert "--permission-mode" not in config

    # --- fail-closed: an unmapped mode never silently passes --------------

    def test_claude_code_translator_fails_closed_for_unmapped_mode(self) -> None:
        with pytest.raises(UnsupportedPermissionModeError):
            _claude_code_permission_args("not-a-real-mode")  # type: ignore[arg-type]

    def test_codex_translator_fails_closed_for_unmapped_mode(self) -> None:
        with pytest.raises(UnsupportedPermissionModeError):
            _codex_permission_args("not-a-real-mode")  # type: ignore[arg-type]

    # --- audit: the engine's own configured mode is persisted -------------

    def test_execution_record_persists_the_engines_configured_permission_mode(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(
            store, subprocess_runner=runner, clock=lambda: UTC_NOW,
            permission_mode=ExecutionPermissionMode.UNRESTRICTED,
        )
        result = asyncio.run(engine.execute(_request(tmp_path)))
        assert result.record.permission_mode == "unrestricted"
        assert store.get(result.record.execution_id).permission_mode == "unrestricted"

    def test_execution_record_permission_mode_is_none_when_engine_unconfigured(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")])
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)
        result = asyncio.run(engine.execute(_request(tmp_path)))
        assert result.record.permission_mode is None


class TestScopedWorkerGitIdentity:
    """P13.2 (see ROADMAP.md): ``scoped_worker_git_identity`` sets/restores
    a workspace's own LOCAL (never --global/--system) git user.name/
    user.email around a block, as a second defense alongside
    ``_worker_git_identity_env``'s env-var injection."""

    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)

    def _local_config(self, path: Path, key: str) -> str | None:
        result = subprocess.run(
            ["git", "config", "--local", "--get", key], cwd=path, capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def test_sets_local_identity_to_worker_during_block(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        with scoped_worker_git_identity(tmp_path, _alice()):
            assert self._local_config(tmp_path, "user.name") == "Alice"
            assert self._local_config(tmp_path, "user.email") == "claude_dev_01@workers.ai-dev-orchestrator.local"

    def test_a_plain_nested_git_commit_with_no_special_env_gets_worker_identity(self, tmp_path: Path) -> None:
        # Proves the actual fix mechanism directly: a `git commit` that
        # receives NO GIT_AUTHOR_*/GIT_COMMITTER_* env at all (exactly the
        # real, observed WI-M1.1-01 failure mode) still lands on the
        # worker's identity, because it is read from local config.
        self._init_repo(tmp_path)
        with scoped_worker_git_identity(tmp_path, _victor()):
            subprocess.run(
                ["git", "commit", "-q", "--allow-empty", "-m", "no special env"],
                cwd=tmp_path, check=True, env={"PATH": os.environ.get("PATH", "")},
            )
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert author == "Victor <codex_dev_01@workers.ai-dev-orchestrator.local>"

    def test_restores_previous_local_identity_after_block(self, tmp_path: Path) -> None:
        # Case J: repo already has a local identity -> restored EXACTLY.
        self._init_repo(tmp_path)
        subprocess.run(["git", "config", "--local", "user.name", "Someone Else"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "--local", "user.email", "someone@example.com"], cwd=tmp_path, check=True)

        with scoped_worker_git_identity(tmp_path, _alice()):
            pass

        assert self._local_config(tmp_path, "user.name") == "Someone Else"
        assert self._local_config(tmp_path, "user.email") == "someone@example.com"

    def test_restores_absence_of_local_identity_after_block(self, tmp_path: Path) -> None:
        # Case K: repo has no local identity at all -> absence restored.
        self._init_repo(tmp_path)
        assert self._local_config(tmp_path, "user.name") is None

        with scoped_worker_git_identity(tmp_path, _alice()):
            assert self._local_config(tmp_path, "user.name") == "Alice"

        assert self._local_config(tmp_path, "user.name") is None
        assert self._local_config(tmp_path, "user.email") is None

    def test_restores_after_exception(self, tmp_path: Path) -> None:
        # Case I.
        self._init_repo(tmp_path)
        subprocess.run(["git", "config", "--local", "user.name", "Someone Else"], cwd=tmp_path, check=True)

        with pytest.raises(RuntimeError):
            with scoped_worker_git_identity(tmp_path, _alice()):
                assert self._local_config(tmp_path, "user.name") == "Alice"
                raise RuntimeError("boom")

        assert self._local_config(tmp_path, "user.name") == "Someone Else"

    def test_never_touches_global_config(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        before = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True)
        with scoped_worker_git_identity(tmp_path, _alice()):
            pass
        after = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True)
        assert before.stdout == after.stdout

    def test_noop_for_non_git_workspace(self, tmp_path: Path) -> None:
        # No git init at all -> silent no-op, like `_git_head_sha`.
        with scoped_worker_git_identity(tmp_path, _alice()):
            pass  # must not raise

    def test_switching_worker_between_two_calls_never_leaks(self, tmp_path: Path) -> None:
        # Case H, at the identity-scoping level: Alice's identity must
        # never still be configured once Victor's own scope begins.
        self._init_repo(tmp_path)

        with scoped_worker_git_identity(tmp_path, _alice()):
            assert self._local_config(tmp_path, "user.name") == "Alice"

        assert self._local_config(tmp_path, "user.name") is None

        with scoped_worker_git_identity(tmp_path, _victor()):
            assert self._local_config(tmp_path, "user.name") == "Victor"

        assert self._local_config(tmp_path, "user.name") is None


class TestUntrackedFileChangeDetection:
    """AUD-2: pre-existing untracked files found deleted/modified during a
    real ``execute()`` call must be reported on ``ExecutionResult.
    untracked_changes`` — detection/observability only, never an automatic
    restore, never itself a reason to fail the execution. Runs through the
    real ``RalphExecutionEngine.execute()`` with a fake subprocess runner
    (never a real Ralph/provider) whose ``on_call`` hook mutates the real
    on-disk workspace exactly like a real worker would, so the git-level
    detection itself is exercised for real.

    The governed workspace (a real git repo) and the execution store's own
    SQLite file are deliberately kept in separate directories, exactly
    like real usage (workspace vs. state_dir are always distinct) — the
    store's own file must never itself be seen as an untracked file
    inside the repo it is reporting on."""

    def _init_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        (path / ".gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.com",
             "commit", "-q", "-m", "seed"],
            cwd=path, check=True,
        )

    def _engine(self, tmp_path: Path, *, on_call=None) -> RalphExecutionEngine:
        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=on_call)
        return RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

    def test_unchanged_preexisting_untracked_file_reports_nothing(self, tmp_path: Path) -> None:
        # 1.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "notes.txt").write_text("hello")

        engine = self._engine(tmp_path)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == ()

    def test_deleted_preexisting_untracked_file_is_detected(self, tmp_path: Path) -> None:
        # 2.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "notes.txt").write_text("hello")

        def _delete(args, cwd, timeout):
            (Path(cwd) / "notes.txt").unlink()

        engine = self._engine(tmp_path, on_call=_delete)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == (
            UntrackedFileChange(path="notes.txt", kind=UntrackedFileChangeKind.DELETED),
        )

    def test_modified_preexisting_untracked_file_is_detected(self, tmp_path: Path) -> None:
        # 3.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "notes.txt").write_text("hello")

        def _modify(args, cwd, timeout):
            (Path(cwd) / "notes.txt").write_text("changed")

        engine = self._engine(tmp_path, on_call=_modify)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == (
            UntrackedFileChange(path="notes.txt", kind=UntrackedFileChangeKind.MODIFIED),
        )

    def test_worker_created_file_is_never_flagged(self, tmp_path: Path) -> None:
        # 4.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)

        def _create(args, cwd, timeout):
            (Path(cwd) / "new_file.txt").write_text("brand new")

        engine = self._engine(tmp_path, on_call=_create)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == ()

    def test_worker_created_then_deleted_file_is_never_flagged(self, tmp_path: Path) -> None:
        # 5.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)

        def _create_and_delete(args, cwd, timeout):
            scratch = Path(cwd) / "scratch.txt"
            scratch.write_text("temp")
            scratch.unlink()

        engine = self._engine(tmp_path, on_call=_create_and_delete)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == ()

    def test_multiple_files_are_all_reported_independently(self, tmp_path: Path) -> None:
        # 6.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        (workspace / "c.txt").write_text("c")

        def _mutate(args, cwd, timeout):
            (Path(cwd) / "a.txt").unlink()
            (Path(cwd) / "b.txt").write_text("b-changed")
            # c.txt deliberately left untouched.

        engine = self._engine(tmp_path, on_call=_mutate)
        result = asyncio.run(engine.execute(_request(workspace)))

        by_path = {c.path: c.kind for c in result.untracked_changes}
        assert by_path == {
            "a.txt": UntrackedFileChangeKind.DELETED,
            "b.txt": UntrackedFileChangeKind.MODIFIED,
        }

    def test_path_with_spaces_is_handled_correctly(self, tmp_path: Path) -> None:
        # 7.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "my notes file.txt").write_text("hello")

        def _delete(args, cwd, timeout):
            (Path(cwd) / "my notes file.txt").unlink()

        engine = self._engine(tmp_path, on_call=_delete)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == (
            UntrackedFileChange(path="my notes file.txt", kind=UntrackedFileChangeKind.DELETED),
        )

    def test_gitignored_file_is_never_reported_even_if_deleted(self, tmp_path: Path) -> None:
        # 8. `ignored.txt` is listed in .gitignore by `_init_repo` above —
        # git ls-files --others --exclude-standard must never see it, so
        # it can never enter the baseline, so its deletion is never
        # reported: the exact semantics of `--exclude-standard`, never a
        # second, parallel ignore-rule implementation.
        workspace = tmp_path / "workspace"
        self._init_repo(workspace)
        (workspace / "ignored.txt").write_text("local scratch state")

        def _delete(args, cwd, timeout):
            (Path(cwd) / "ignored.txt").unlink()

        engine = self._engine(tmp_path, on_call=_delete)
        result = asyncio.run(engine.execute(_request(workspace)))

        assert result.untracked_changes == ()


class TestWorkerCommitIdentityAudit:
    """P13.2 (see ROADMAP.md): ``_audit_worker_commit_identity`` fail-closes
    (``WorkerCommitIdentityMismatchError``) on the first commit in
    ``sha_before..sha_after`` not attributed to the expected worker on
    both author and committer."""

    def _init_repo(self, path: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.com",
             "commit", "-q", "--allow-empty", "-m", "seed"],
            cwd=path, check=True,
        )
        return _head(path)

    def _commit_as(self, path: Path, *, name: str, email: str, message: str) -> str:
        subprocess.run(
            ["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
             "commit", "-q", "--allow-empty", "-m", message],
            cwd=path, check=True,
        )
        return _head(path)

    def test_no_commits_passes(self, tmp_path: Path) -> None:
        # Case A.
        sha = self._init_repo(tmp_path)
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_alice(), execution_id="exec-a",
            sha_before=sha, sha_after=sha,
        )  # must not raise

    def test_single_correct_commit_passes(self, tmp_path: Path) -> None:
        # Case B.
        sha_before = self._init_repo(tmp_path)
        sha_after = self._commit_as(
            tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="alice work",
        )
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_alice(), execution_id="exec-b",
            sha_before=sha_before, sha_after=sha_after,
        )  # must not raise

    def test_multiple_correct_commits_pass(self, tmp_path: Path) -> None:
        # Case C.
        sha_before = self._init_repo(tmp_path)
        self._commit_as(tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="1")
        sha_after = self._commit_as(
            tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="2",
        )
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_alice(), execution_id="exec-c",
            sha_before=sha_before, sha_after=sha_after,
        )  # must not raise

    def test_second_commit_wrong_identity_fails_closed(self, tmp_path: Path) -> None:
        # Case D.
        sha_before = self._init_repo(tmp_path)
        self._commit_as(tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="1")
        bad_sha = self._commit_as(tmp_path, name="yannickameur", email="yannick.ameur@gmail.com", message="2")

        with pytest.raises(WorkerCommitIdentityMismatchError) as exc_info:
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_alice(), execution_id="exec-d",
                sha_before=sha_before, sha_after=_head(tmp_path),
            )
        assert exc_info.value.commit_sha == bad_sha

    def test_author_correct_committer_wrong_fails_closed(self, tmp_path: Path) -> None:
        # Case E.
        sha_before = self._init_repo(tmp_path)
        subprocess.run(
            ["git", "-c", "user.name=Alice", "-c", "user.email=claude_dev_01@workers.ai-dev-orchestrator.local",
             "commit", "-q", "--allow-empty", "-m", "mixed",
             "--author=Alice <claude_dev_01@workers.ai-dev-orchestrator.local>"],
            cwd=tmp_path, check=True,
            env={**os.environ, "GIT_COMMITTER_NAME": "yannickameur", "GIT_COMMITTER_EMAIL": "yannick.ameur@gmail.com"},
        )
        with pytest.raises(WorkerCommitIdentityMismatchError) as exc_info:
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_alice(), execution_id="exec-e",
                sha_before=sha_before, sha_after=_head(tmp_path),
            )
        assert "yannick.ameur@gmail.com" in exc_info.value.observed_committer

    def test_author_wrong_committer_correct_fails_closed(self, tmp_path: Path) -> None:
        # Case F.
        sha_before = self._init_repo(tmp_path)
        subprocess.run(
            ["git", "-c", "user.name=Alice", "-c", "user.email=claude_dev_01@workers.ai-dev-orchestrator.local",
             "commit", "-q", "--allow-empty", "-m", "mixed",
             "--author=yannickameur <yannick.ameur@gmail.com>"],
            cwd=tmp_path, check=True,
        )
        with pytest.raises(WorkerCommitIdentityMismatchError) as exc_info:
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_alice(), execution_id="exec-f",
                sha_before=sha_before, sha_after=_head(tmp_path),
            )
        assert "yannick.ameur@gmail.com" in exc_info.value.observed_author

    def test_ralph_safety_commit_with_wrong_identity_is_still_caught(self, tmp_path: Path) -> None:
        # Case G: a later, otherwise-legitimate-looking "safety" commit is
        # audited exactly like any other commit in the range — no
        # special-casing by message/position.
        sha_before = self._init_repo(tmp_path)
        self._commit_as(tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="work")
        bad_sha = self._commit_as(
            tmp_path, name="yannickameur", email="yannick.ameur@gmail.com",
            message="chore: auto-commit before merge (loop primary)",
        )
        with pytest.raises(WorkerCommitIdentityMismatchError) as exc_info:
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_alice(), execution_id="exec-g",
                sha_before=sha_before, sha_after=_head(tmp_path),
            )
        assert exc_info.value.commit_sha == bad_sha

    def test_dev_a_alice_never_leaks_into_dev_b_victor_commits(self, tmp_path: Path) -> None:
        # Case H, at the audit level.
        sha0 = self._init_repo(tmp_path)
        sha_after_alice = self._commit_as(
            tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="dev a",
        )
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_alice(), execution_id="exec-h-a",
            sha_before=sha0, sha_after=sha_after_alice,
        )  # must not raise

        sha_after_victor = self._commit_as(
            tmp_path, name="Victor", email="codex_dev_01@workers.ai-dev-orchestrator.local", message="dev b",
        )
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_victor(), execution_id="exec-h-b",
            sha_before=sha_after_alice, sha_after=sha_after_victor,
        )  # must not raise

        # And Alice's own range never contains Victor's commit or vice versa.
        with pytest.raises(WorkerCommitIdentityMismatchError):
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_victor(), execution_id="exec-h-wrong",
                sha_before=sha0, sha_after=sha_after_alice,
            )

    def test_mismatch_error_carries_full_evidence(self, tmp_path: Path) -> None:
        sha_before = self._init_repo(tmp_path)
        bad_sha = self._commit_as(tmp_path, name="yannickameur", email="yannick.ameur@gmail.com", message="oops")

        with pytest.raises(WorkerCommitIdentityMismatchError) as exc_info:
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_alice(), execution_id="exec-evidence",
                sha_before=sha_before, sha_after=_head(tmp_path),
            )
        err = exc_info.value
        assert err.execution_id == "exec-evidence"
        assert err.worker_id == "claude_dev_01"
        assert err.commit_sha == bad_sha
        assert err.expected_author == "Alice <claude_dev_01@workers.ai-dev-orchestrator.local>"
        assert err.observed_author == "yannickameur <yannick.ameur@gmail.com>"
        assert err.expected_committer == err.expected_author
        assert err.observed_committer == err.observed_author

    def test_no_prior_commits_audits_full_history_from_after(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        sha = self._commit_as(
            tmp_path, name="Alice", email="claude_dev_01@workers.ai-dev-orchestrator.local", message="first ever",
        )
        _audit_worker_commit_identity(
            workspace=tmp_path, worker=_alice(), execution_id="exec-first",
            sha_before=None, sha_after=sha,
        )  # must not raise: the only commit in history is Alice's own

        with pytest.raises(WorkerCommitIdentityMismatchError):
            _audit_worker_commit_identity(
                workspace=tmp_path, worker=_victor(), execution_id="exec-first-wrong",
                sha_before=None, sha_after=sha,
            )


_FAKE_RALPH_SCRIPT = '''#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

def main():
    cwd = Path.cwd()
    ralph_dir = cwd / ".ralph"
    ralph_dir.mkdir(exist_ok=True)
    (ralph_dir / "current-loop-id").write_text("loop-1")
    events_path = ralph_dir / "events-1.jsonl"
    (ralph_dir / "current-events").write_text(".ralph/" + events_path.name)
    events_path.write_text('{"topic": "work.completed", "ts": "2026-09-12T15:00:00+00:00"}\\n')

    mode = "__MODE__"
    if mode == "stripped-env":
        # Simulates the real, observed defect: a nested shell-tool `git
        # commit` that inherits NO GIT_AUTHOR_*/GIT_COMMITTER_* env at all
        # -- only PATH, exactly like a sandboxed coding-agent Bash tool
        # that does not forward custom parent env vars.
        stripped_env = {"PATH": os.environ.get("PATH", "")}
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "nested work"],
            cwd=str(cwd), env=stripped_env, check=True,
        )
    elif mode == "adversarial-identity":
        # Simulates a nested commit that explicitly overrides identity via
        # `-c`, defeating both the env injection AND the scoped local
        # config -- the last line of defense (the post-run audit) must
        # still catch this.
        subprocess.run(
            ["git", "-c", "user.name=yannickameur", "-c", "user.email=yannick.ameur@gmail.com",
             "commit", "-q", "--allow-empty", "-m", "nested work, wrong identity"],
            cwd=str(cwd), env={"PATH": os.environ.get("PATH", "")}, check=True,
        )
    return 0

sys.exit(main())
'''


def _write_fake_ralph(tmp_path: Path, *, mode: str = "stripped-env", name: str = "fake-ralph.py") -> Path:
    script = tmp_path / name
    script.write_text(_FAKE_RALPH_SCRIPT.replace("__MODE__", mode))
    script.chmod(0o755)
    return script


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.com",
         "commit", "-q", "--allow-empty", "-m", "seed"],
        cwd=path, check=True,
    )


class TestWorkerCommitIdentityEndToEnd:
    """P13.2 (see ROADMAP.md): the real ``_default_subprocess_runner`` path
    through ``RalphExecutionEngine.execute()``, with a tiny local fake
    ``ralph`` script standing in for the real binary — a real subprocess,
    a real nested subprocess, and real `git`, but no Claude/Codex/Ralph/
    network, fully offline, proving both the regression (section 4) and
    the fix through the actual code path new production runs use."""

    def test_nested_commit_with_stripped_env_still_gets_worker_identity(self, tmp_path: Path) -> None:
        _init_git_workspace(tmp_path)
        script = _write_fake_ralph(tmp_path)
        store = _store(tmp_path)
        engine = RalphExecutionEngine(store, ralph_binary=str(script), clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_victor())))

        assert result.record.status.value == "succeeded"
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert author == "Victor <codex_dev_01@workers.ai-dev-orchestrator.local>"

    def test_adversarial_nested_identity_is_caught_and_fails_closed(self, tmp_path: Path) -> None:
        _init_git_workspace(tmp_path)
        script = _write_fake_ralph(tmp_path, mode="adversarial-identity", name="fake-ralph-adversarial.py")
        store = _store(tmp_path)
        engine = RalphExecutionEngine(store, ralph_binary=str(script), clock=lambda: UTC_NOW)

        with pytest.raises(WorkerCommitIdentityMismatchError):
            asyncio.run(engine.execute(_request(tmp_path, worker=_alice())))

        record = store.get("exec-001")
        assert record.status.value == "failed"

    def test_local_config_restored_after_a_real_run(self, tmp_path: Path) -> None:
        _init_git_workspace(tmp_path)
        script = _write_fake_ralph(tmp_path)
        store = _store(tmp_path)
        engine = RalphExecutionEngine(store, ralph_binary=str(script), clock=lambda: UTC_NOW)

        before = subprocess.run(
            ["git", "config", "--local", "--get", "user.name"], cwd=tmp_path, capture_output=True, text=True,
        )
        asyncio.run(engine.execute(_request(tmp_path, worker=_victor())))
        after = subprocess.run(
            ["git", "config", "--local", "--get", "user.name"], cwd=tmp_path, capture_output=True, text=True,
        )
        assert before.returncode == after.returncode == 1  # never set locally, before or after

    def test_fake_runner_path_is_unaffected_by_this_feature(self, tmp_path: Path) -> None:
        # A fake/injected subprocess_runner (every other test in this
        # suite) must behave exactly as before this feature: no scoped
        # identity, no audit, even if it creates a commit under an
        # unrelated identity.
        _init_git_workspace(tmp_path)
        subprocess.run(["git", "config", "--local", "user.name", "Pre-existing"], cwd=tmp_path, check=True)

        def _on_call(args, cwd, timeout):
            subprocess.run(
                ["git", "-c", "user.name=Not Alice", "-c", "user.email=not-alice@example.com",
                 "commit", "-q", "--allow-empty", "-m", "fake runner work"],
                cwd=cwd, check=True,
            )

        store = _store(tmp_path)
        runner = _make_fake_runner(events_lines=[_event_line("work.completed")], on_call=_on_call)
        engine = RalphExecutionEngine(store, subprocess_runner=runner, clock=lambda: UTC_NOW)

        result = asyncio.run(engine.execute(_request(tmp_path, worker=_alice())))

        assert result.record.status.value == "succeeded"  # not caught, by design (see class docstring)


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()
