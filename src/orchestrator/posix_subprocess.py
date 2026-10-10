"""A single, shared POSIX process-group subprocess primitive (P18-03).

``ralph_execution_engine.py`` and ``validation.py`` each already ran a
subprocess via ``asyncio.create_subprocess_exec`` and killed only the
*direct* child on a timeout — never any descendant it may have spawned
(a real provider CLI launched by ``ralph``; a test runner's own worker
processes launched by ``pytest``/``npm``/...). This module is the one
place that launches a subprocess in its own session/process group and
terminates the *whole group* — never a second, divergent implementation
of the same primitive per caller (REUSE FIRST).

POSIX only, deliberately: this project makes no Windows support claim
anywhere else (no OS classifier, no conditional subprocess handling
elsewhere) — a Windows equivalent is real, separate, future work, never
built ahead of a demonstrated need (YAGNI).

Two real interruption sources are handled identically here, through one
code path, never two:

- a real command timeout (``asyncio.TimeoutError``, existing behavior);
- an external cancellation of the awaiting task
  (``asyncio.CancelledError`` — e.g. ``asyncio.run()``'s own cancellation
  pass on a real Ctrl+C, see ``ROADMAP.md`` P18-03). Neither is ever
  swallowed: both are re-raised, unchanged, after the process group is
  terminated and reaped — a caller decides what a timeout means for its
  own domain (``RalphTimeoutError``/``ValidationTimeoutError``); a
  cancellation always propagates as exactly that, never converted into
  a fabricated success.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Progressive observation callback: ``(stream, text)`` where ``stream``
#: is ``"stdout"`` or ``"stderr"``. Purely observational (see
#: ``run_in_new_process_group``).
OutputObserver = Callable[[str, str], None]

#: Heartbeat callback: ``(elapsed_seconds)`` — real wall-clock time since
#: the process was launched. Carries no output, no progress claim.
HeartbeatObserver = Callable[[float], None]

#: Default silence (no stdout/stderr byte) after which one heartbeat is
#: emitted, then one per further silent interval. Bounded: never a busy loop.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0

#: Upper bound, in characters, of any single chunk handed to an
#: ``OutputObserver`` — a line longer than this is split.
MAX_OBSERVED_CHUNK_CHARS = 4096
_READ_SIZE = 4096

#: How long to wait for a clean SIGTERM exit before escalating to
#: SIGKILL — short and fixed: this is cleanup after an already-decided
#: timeout/cancellation, never a second negotiation window.
GRACE_PERIOD_SECONDS = 5.0


async def run_in_new_process_group(
    argv: Sequence[str], cwd: Path, timeout: float, *, env: Mapping[str, str] | None = None,
    on_output: OutputObserver | None = None,
    on_heartbeat: HeartbeatObserver | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> tuple[int, bytes, bytes]:
    """Runs ``argv`` in its own POSIX session/process group
    (``start_new_session=True``) so the whole group — not just the
    direct child — can be terminated together.

    Raises ``asyncio.TimeoutError`` (after terminating the group) on a
    real timeout, or re-raises ``asyncio.CancelledError`` (after
    terminating the group) if the awaiting task is cancelled — a caller
    translates the former into its own domain-specific timeout error;
    neither is ever swallowed.

    ``on_output`` (optional, ``None`` = exactly the previous behavior):
    stdout and stderr are drained concurrently and each decoded line
    (or, for the final output without a trailing newline, the remaining
    fragment) is passed to it as ``(stream, text)`` *while the process
    is still running*. Order is preserved per stream (not across
    streams); no chunk exceeds ``MAX_OBSERVED_CHUNK_CHARS``. The
    callback is observational only: an exception it raises is logged
    and ignored, never terminating the worker. The returned
    ``stdout``/``stderr`` are the complete captures, identical to the
    no-callback path.

    Known limitation, not hidden: this terminates every process that
    stayed in the launched session, including descendants that moved
    into their own process group (as ``ralph`` and provider CLIs do). A
    descendant that detaches into a new session (``setsid``) is outside
    it and outside this project's control — ``ralph``/a provider CLI are
    external binaries already treated as such elsewhere in this
    codebase.
    """
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=dict(env) if env is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if on_output is None and on_heartbeat is None:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        else:
            stdout, stderr = await asyncio.wait_for(
                _communicate_observed(process, on_output, on_heartbeat, heartbeat_interval), timeout=timeout,
            )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await _terminate_process_group(process)
        raise
    return process.returncode, stdout, stderr


def _notify(on_output: OutputObserver | None, stream: str, text: str) -> None:
    if on_output is None:
        return
    for start in range(0, len(text), MAX_OBSERVED_CHUNK_CHARS):
        try:
            on_output(stream, text[start:start + MAX_OBSERVED_CHUNK_CHARS])
        except Exception:  # observational only — never kills the worker
            logger.warning("output observer failed for %s; ignored", stream, exc_info=True)


async def _drain(
    reader: asyncio.StreamReader, stream: str, on_output: OutputObserver | None,
    activity: list[float] | None = None,
) -> bytes:
    """Reads ``reader`` to EOF, returning every byte read and notifying
    ``on_output`` line by line (plus the final unterminated fragment)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    captured = bytearray()
    pending = ""
    while True:
        data = await reader.read(_READ_SIZE)
        if not data:
            break
        captured += data
        if activity is not None:
            activity[0] = time.monotonic()
        pending += decoder.decode(data)
        *lines, pending = pending.split("\n")
        for line in lines:
            _notify(on_output, stream, line + "\n")
        if len(pending) >= MAX_OBSERVED_CHUNK_CHARS:  # bound memory on newline-free output
            _notify(on_output, stream, pending)
            pending = ""
    pending += decoder.decode(b"", final=True)
    if pending:
        _notify(on_output, stream, pending)
    return bytes(captured)


async def _heartbeat(
    process: "asyncio.subprocess.Process", on_heartbeat: HeartbeatObserver,
    interval: float, started: float, activity: list[float],
) -> None:
    """Emits only while the direct process is running and output is silent."""
    while True:
        await asyncio.sleep(max(activity[0] + interval - time.monotonic(), 0.0))
        if process.returncode is not None:
            return
        now = time.monotonic()
        if now - activity[0] < interval:
            continue  # output arrived meanwhile — silence restarted
        activity[0] = now
        try:
            on_heartbeat(now - started)
        except Exception:  # observational only
            logger.warning("heartbeat observer failed; ignored", exc_info=True)


async def _communicate_observed(
    process: "asyncio.subprocess.Process", on_output: OutputObserver | None,
    on_heartbeat: HeartbeatObserver | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None and process.stderr is not None
    started = time.monotonic()
    activity = [started]
    tasks = [
        asyncio.ensure_future(_drain(process.stdout, "stdout", on_output, activity)),
        asyncio.ensure_future(_drain(process.stderr, "stderr", on_output, activity)),
    ]
    beat = None
    if on_heartbeat is not None:
        beat = asyncio.ensure_future(_heartbeat(process, on_heartbeat, max(heartbeat_interval, 0.01), started, activity))
        tasks.append(beat)
    try:
        stdout, stderr = await asyncio.gather(*tasks[:2])
        await process.wait()
        if beat is not None:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return stdout, stderr


#: Poll interval while waiting for every member of the launched session
#: to exit during termination.
_TERMINATION_POLL_SECONDS = 0.05


def _session_members(sid: int) -> list[int]:
    """Live (non-zombie) processes whose session id is ``sid``.

    ``ralph`` and the provider CLI it launches each move into their own
    process group, but they stay in the session this module started, so
    the session — not the direct child's group — is what must end."""
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return _session_members_from_ps(sid)
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat = handle.read().decode(errors="replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) > 3 and fields[0] != "Z" and int(fields[3]) == sid:
            members.append(int(entry))
    return members


def _session_members_from_ps(sid: int) -> list[int]:
    import subprocess

    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,sess=,stat="], capture_output=True, text=True).stdout
    except OSError:
        return []
    members = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(sid) and not parts[2].startswith("Z"):
            members.append(int(parts[0]))
    return members


def _signal_session(sid: int, sig: int) -> None:
    for pid in _session_members(sid):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


async def _session_ended(process: "asyncio.subprocess.Process", sid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if process.returncode is not None and not _session_members(sid):
            return True
        if time.monotonic() >= deadline:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=_TERMINATION_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _terminate_process_group(process: "asyncio.subprocess.Process") -> None:
    """SIGTERM every process of the launched session — the direct child's
    group and any group a descendant created inside that session — then
    SIGKILL whatever is still alive after ``GRACE_PERIOD_SECONDS``, and
    return only once the direct child is reaped and no session member is
    left. Never raises on a session that already exited on its own."""
    sid = process.pid  # start_new_session=True: the child leads its own session
    _signal_session(sid, signal.SIGTERM)
    if await _session_ended(process, sid, GRACE_PERIOD_SECONDS):
        return
    _signal_session(sid, signal.SIGKILL)
    await _session_ended(process, sid, GRACE_PERIOD_SECONDS)
    await asyncio.shield(process.wait())
