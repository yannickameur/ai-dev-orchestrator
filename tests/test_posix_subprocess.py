"""Tests for ``orchestrator.posix_subprocess`` (P18-03): the shared
POSIX process-group primitive ``ralph_execution_engine.py`` and
``validation.py`` both delegate to.

Real subprocesses (``sh``/``sleep``) are used here on purpose — this is
exactly what proves the whole process group is actually terminated, not
just the direct child — but never a real provider/Ralph/pytest-under-test
process. Linux only (matches this project's own CI, ``ubuntu-latest``);
no Windows support is claimed anywhere in this project.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from orchestrator.posix_subprocess import run_in_new_process_group

# A shell that backgrounds a real child (`sleep`), records its PID, then
# waits on it — so the *direct* child (`sh`) has its own real descendant,
# exactly like `ralph` launching a real provider CLI.
_SPAWN_CHILD_SCRIPT = "sleep 30 & echo $! > child.pid; wait"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_child_pid(cwd: Path) -> int:
    return int((cwd / "child.pid").read_text().strip())


class TestTimeout:
    def test_timeout_terminates_the_whole_process_group_not_just_the_direct_child(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run_in_new_process_group(["sh", "-c", _SPAWN_CHILD_SCRIPT], tmp_path, timeout=0.3))

        child_pid = _read_child_pid(tmp_path)
        time.sleep(0.3)  # let signal delivery/process teardown settle
        assert not _pid_alive(child_pid)


class TestCancellation:
    def test_cancellation_terminates_the_whole_process_group_and_reraises_cancelled_error(
        self, tmp_path: Path,
    ) -> None:
        async def _drive() -> None:
            task = asyncio.ensure_future(
                run_in_new_process_group(["sh", "-c", _SPAWN_CHILD_SCRIPT], tmp_path, timeout=30)
            )
            # Give the shell a real moment to actually start, background
            # `sleep`, and write its PID file before cancelling.
            for _ in range(50):
                if (tmp_path / "child.pid").exists():
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())

        child_pid = _read_child_pid(tmp_path)
        time.sleep(0.3)
        assert not _pid_alive(child_pid)

    def test_cancellation_never_swallowed_as_a_success(self, tmp_path: Path) -> None:
        """A cancellation must never be observed as a normal return —
        this is the exact bug class P18-03 exists to close (see
        ROADMAP.md: a plain asyncio.to_thread(...) call could silently
        keep running after the awaiting task moved on)."""
        async def _drive() -> str:
            task = asyncio.ensure_future(
                run_in_new_process_group(["sh", "-c", "sleep 30"], tmp_path, timeout=30)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return "cancelled"
            return "NOT cancelled — bug"

        assert asyncio.run(_drive()) == "cancelled"


# Like `ralph` and the provider CLI it launches: a descendant that moves
# into its own process group inside the launched session and ignores
# SIGTERM, while the direct child exits at once on SIGTERM.
_OWN_GROUP_DESCENDANT = (
    "import os, signal, time; os.setpgid(0, 0); "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open('child.pid.tmp', 'w').write(str(os.getpid())); os.rename('child.pid.tmp', 'child.pid'); "
    "time.sleep(30)"
)
_OWN_GROUP_DESCENDANT_SCRIPT = (
    f"trap 'exit 0' TERM; {sys.executable} -c \"{_OWN_GROUP_DESCENDANT}\" & wait"
)


class TestDescendantInItsOwnProcessGroup:
    def _cancel_after_start(self, tmp_path: Path) -> None:
        async def _drive() -> None:
            task = asyncio.ensure_future(
                run_in_new_process_group(["sh", "-c", _OWN_GROUP_DESCENDANT_SCRIPT], tmp_path, timeout=30)
            )
            for _ in range(100):
                if (tmp_path / "child.pid").exists():
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())

    def test_descendant_is_in_another_group_of_the_same_session(self, tmp_path: Path) -> None:
        async def _drive() -> None:
            task = asyncio.ensure_future(
                run_in_new_process_group(["sh", "-c", _OWN_GROUP_DESCENDANT_SCRIPT], tmp_path, timeout=30)
            )
            for _ in range(100):
                if (tmp_path / "child.pid").exists():
                    break
                await asyncio.sleep(0.02)
            child = _read_child_pid(tmp_path)
            assert os.getpgid(child) == child  # its own group, like ralph/codex
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())

    def test_cancellation_returns_only_after_the_whole_session_is_gone(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import orchestrator.posix_subprocess as module

        monkeypatch.setattr(module, "GRACE_PERIOD_SECONDS", 0.5)
        self._cancel_after_start(tmp_path)
        # No settling sleep: termination must be complete when the call returns.
        assert not _pid_alive(_read_child_pid(tmp_path))

    def test_timeout_also_terminates_a_descendant_in_its_own_group(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import orchestrator.posix_subprocess as module

        monkeypatch.setattr(module, "GRACE_PERIOD_SECONDS", 0.5)
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run_in_new_process_group(["sh", "-c", _OWN_GROUP_DESCENDANT_SCRIPT], tmp_path, timeout=0.5))
        assert not _pid_alive(_read_child_pid(tmp_path))


class TestNormalCompletion:
    def test_normal_completion_returns_real_exit_code_and_output(self, tmp_path: Path) -> None:
        returncode, stdout, _stderr = asyncio.run(
            run_in_new_process_group(["sh", "-c", "echo hello"], tmp_path, timeout=5)
        )
        assert returncode == 0
        assert stdout.strip() == b"hello"


class TestProgressiveOutput:
    @staticmethod
    def _run(script: str, tmp_path: Path, timeout: float = 10, observer=None):
        seen: list[tuple[str, str, float]] = []

        def _obs(stream: str, text: str) -> None:
            seen.append((stream, text, time.monotonic()))
            if observer:
                observer(stream, text)

        result = asyncio.run(
            run_in_new_process_group(["sh", "-c", script], tmp_path, timeout, on_output=_obs)
        )
        return result, seen

    def test_streams_separate_ordered_with_final_fragment_and_full_captures(self, tmp_path: Path) -> None:
        script = "echo o1; echo e1 >&2; echo o2; echo e2 >&2; printf tail"
        (code, out, err), seen = self._run(script, tmp_path)
        assert code == 0
        assert out == b"o1\no2\ntail" and err == b"e1\ne2\n"
        assert [t for s, t, _ in seen if s == "stdout"] == ["o1\n", "o2\n", "tail"]
        assert [t for s, t, _ in seen if s == "stderr"] == ["e1\n", "e2\n"]

    def test_progress_is_delivered_before_process_exit(self, tmp_path: Path) -> None:
        start = time.monotonic()
        _, seen = self._run("echo first; sleep 1; echo last", tmp_path)
        first = next(t for s, text, t in seen if text == "first\n")
        assert first - start < 0.8

    def test_chunks_are_bounded(self, tmp_path: Path) -> None:
        from orchestrator.posix_subprocess import MAX_OBSERVED_CHUNK_CHARS

        (_, out, _), seen = self._run("head -c 20000 /dev/zero | tr '\\0' x", tmp_path)
        assert len(out) == 20000
        assert all(len(t) <= MAX_OBSERVED_CHUNK_CHARS for _, t, _ in seen)
        assert sum(len(t) for _, t, _ in seen) == 20000

    def test_both_busy_streams_are_drained_concurrently(self, tmp_path: Path) -> None:
        script = (
            "head -c 131072 /dev/zero | tr '\\0' o & "
            "head -c 131072 /dev/zero | tr '\\0' e >&2 & wait"
        )
        (code, out, err), seen = self._run(script, tmp_path)
        assert code == 0
        assert out == b"o" * 131072 and err == b"e" * 131072
        assert "".join(text for stream, text, _ in seen if stream == "stdout") == out.decode()
        assert "".join(text for stream, text, _ in seen if stream == "stderr") == err.decode()

    def test_absent_callback_preserves_behavior(self, tmp_path: Path) -> None:
        code, out, err = asyncio.run(
            run_in_new_process_group(["sh", "-c", "echo a; echo b >&2"], tmp_path, 10)
        )
        assert (code, out, err) == (0, b"a\n", b"b\n")

    def test_failing_callback_does_not_terminate_worker(self, tmp_path: Path) -> None:
        def _boom(stream: str, text: str) -> None:
            raise RuntimeError("observer bug")

        (code, out, _), seen = self._run("echo a; echo b", tmp_path, observer=_boom)
        assert code == 0 and out == b"a\nb\n" and len(seen) == 2

    def test_timeout_terminates_group_with_observer(self, tmp_path: Path) -> None:
        script = "sleep 30 & echo $! > child.pid; echo started; wait"
        seen: list[str] = []
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(
                run_in_new_process_group(
                    ["sh", "-c", script], tmp_path, 0.5, on_output=lambda s, t: seen.append(t)
                )
            )
        time.sleep(0.3)
        assert seen == ["started\n"]
        assert not _pid_alive(_read_child_pid(tmp_path))

    def test_cancellation_terminates_group_with_observer(self, tmp_path: Path) -> None:
        async def _drive() -> None:
            task = asyncio.ensure_future(
                run_in_new_process_group(
                    ["sh", "-c", _SPAWN_CHILD_SCRIPT], tmp_path, 30, on_output=lambda s, t: None
                )
            )
            await asyncio.sleep(0.5)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_drive())
        time.sleep(0.3)
        assert not _pid_alive(_read_child_pid(tmp_path))


class TestHeartbeat:
    @staticmethod
    def _run(script: str, tmp_path: Path, interval: float = 0.2, heartbeat=None):
        events: list[tuple[str, object]] = []

        def _beat(elapsed: float) -> None:
            events.append(("beat", elapsed))
            if heartbeat:
                heartbeat(elapsed)

        result = asyncio.run(run_in_new_process_group(
            ["sh", "-c", script], tmp_path, 10,
            on_output=lambda s, t: events.append(("out", t)),
            on_heartbeat=_beat, heartbeat_interval=interval,
        ))
        return result, events

    def test_silence_emits_bounded_heartbeats_with_real_elapsed(self, tmp_path: Path) -> None:
        (code, out, _), events = self._run("sleep 1", tmp_path)
        beats = [e for k, e in events if k == "beat"]
        assert code == 0 and out == b""
        assert 2 <= len(beats) <= 5  # ~1s / 0.2s, never a busy loop
        assert beats == sorted(beats) and 0.15 <= beats[0] < 0.6 and beats[-1] <= 1.5

    def test_output_resumes_after_heartbeat_and_resets_silence(self, tmp_path: Path) -> None:
        _, events = self._run("sleep 0.5; echo back; sleep 0.1; echo again", tmp_path)
        kinds = [k for k, _ in events]
        assert "beat" in kinds
        assert kinds.index("beat") < kinds.index("out")
        assert [t for k, t in events if k == "out"] == ["back\n", "again\n"]
        assert kinds.count("beat") <= 3  # chatty phase emits none

    def test_no_heartbeat_while_output_keeps_flowing(self, tmp_path: Path) -> None:
        _, events = self._run("for i in 1 2 3 4 5; do echo $i; sleep 0.1; done", tmp_path, interval=0.4)
        assert all(k != "beat" for k, _ in events)

    def test_no_heartbeat_after_process_exits_with_pipe_held_open(self, tmp_path: Path) -> None:
        # The background child inherits stdout/stderr; draining waits for it,
        # but the direct process whose execution we report has exited.
        (code, out, err), events = self._run("sleep 0.7 & exit 0", tmp_path, interval=0.1)
        assert (code, out, err) == (0, b"", b"")
        assert all(kind != "beat" for kind, _ in events)

    def test_failing_heartbeat_callback_is_isolated(self, tmp_path: Path) -> None:
        def boom(_: float) -> None:
            raise RuntimeError("renderer bug")

        (code, out, _), events = self._run("sleep 0.5; echo ok", tmp_path, heartbeat=boom)
        assert code == 0 and out == b"ok\n"
        assert any(k == "beat" for k, _ in events)

    def test_heartbeat_without_output_observer_keeps_captures(self, tmp_path: Path) -> None:
        beats: list[float] = []
        code, out, err = asyncio.run(run_in_new_process_group(
            ["sh", "-c", "sleep 0.5; echo ok; echo e >&2"], tmp_path, 10,
            on_heartbeat=beats.append, heartbeat_interval=0.2,
        ))
        assert (code, out, err) == (0, b"ok\n", b"e\n") and beats

    def test_timeout_stops_heartbeats_and_still_raises(self, tmp_path: Path) -> None:
        beats: list[float] = []
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run_in_new_process_group(
                ["sh", "-c", "sleep 30"], tmp_path, 0.6,
                on_heartbeat=beats.append, heartbeat_interval=0.2,
            ))
        assert beats
