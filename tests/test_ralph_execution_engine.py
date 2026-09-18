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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.execution_store import ExecutionStatus, ExecutionStore
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    RalphEventParseError,
    RalphExecutionEngine,
    RalphLaunchError,
    ReservedEventTopicError,
    UnsupportedBackendError,
    UnsupportedProfileOptionError,
    parse_ralph_events,
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
