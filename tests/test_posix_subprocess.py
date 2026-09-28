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


class TestNormalCompletion:
    def test_normal_completion_returns_real_exit_code_and_output(self, tmp_path: Path) -> None:
        returncode, stdout, _stderr = asyncio.run(
            run_in_new_process_group(["sh", "-c", "echo hello"], tmp_path, timeout=5)
        )
        assert returncode == 0
        assert stdout.strip() == b"hello"
