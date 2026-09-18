"""RalphExecutionEngine — runs one already-selected Worker via Ralph.

This module is the only place in the codebase that actually launches a
worker. It sits strictly downstream of :mod:`~orchestrator.worker_selector`
(which decides *which* worker, before this module ever runs) and upstream
of nothing else in this slice — no MVP/task orchestration, no GitHub PR
flow. See ROADMAP.md, Phase 1, step 9, and docs/SPIKE_RALPH.md for the
validated Ralph behaviors this module relies on.

Pipeline (see task description): ``ExecutionRequest`` -> create an
``ExecutionRecord`` as ``RUNNING`` -> write a temporary, request-scoped
Ralph config/hats/prompt -> run ``ralph`` as a subprocess -> read back its
events -> compute a business verdict -> finalize the ``ExecutionRecord``.

Design invariants:

- This engine never chooses a worker, never falls back to a different
  provider/worker, never retries automatically, and never queries a
  provider's quota directly (that is QuotaManager's/WorkerSelector's job,
  already done before an ``ExecutionRequest`` reaches this module).
- It never calls ``claude`` or ``codex`` directly — only ``ralph``. Ralph
  itself launches the concrete backend.
- EXIT CODE != BUSINESS VERDICT (validated in docs/SPIKE_RALPH.md: a real
  run produced the ``LOOP_COMPLETE`` event, yet Ralph still reported
  ``reason=max_iterations`` and exit code 2). The verdict is derived
  exclusively from the business events named in
  ``ExecutionRequest.success_topics``/``failure_topics``; ``exit_code`` is
  preserved as audit data only, alongside the record.
- Fail-closed: if Ralph exits without producing a recognized terminal
  business event, the execution is finalized as ``FAILED`` — it is never
  finalized as ``SUCCEEDED`` just because the process exited zero or
  because ``LOOP_COMPLETE`` appeared (that promise alone is not proof of
  business success, per the spike).
- No blind retry: a timeout finalizes the execution as ``INTERRUPTED``
  (via ``ExecutionStore.mark_interrupted``) and returns normally — this
  engine does not re-launch anything itself, and does not scan for other
  ``RUNNING`` executions of the same task. Recognizing an older ``RUNNING``
  execution left over from a previous process and deciding to mark it
  ``RECOVERY_REQUIRED`` is a future reconciliation pass built on top of
  ``ExecutionStore.list_running()`` (already available since the previous
  slice) — this engine does not perform that scan itself.
- Git access is read-only: ``git rev-parse HEAD`` before and after the
  run, nothing else. No checkout, branch, worktree, add, commit, reset,
  merge, or clean. A non-git workspace yields ``None`` for both SHAs.
- The generated Ralph config/hats/prompt files live in a dedicated
  temporary directory per execution, never in the user's permanent Ralph
  configuration; that directory is removed after the run regardless of
  outcome.
- Backend/model/reasoning_effort translation is driven by
  ``Worker.backend`` (never ``Worker.provider``) through a small,
  explicit, extensible table — this is a CLI-syntax translation concern
  (how *this* backend expects ``--model``/effort flags), not a governance
  decision, and stays independent of WorkerSelector's provider-agnostic
  selection logic.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from orchestrator.execution_store import ExecutionRecord, ExecutionStore
from orchestrator.worker_selector import Worker

Clock = Callable[[], datetime]

DEFAULT_RALPH_BINARY = "ralph"
DEFAULT_MAX_ITERATIONS = 5
_MAX_CAPTURED_OUTPUT_CHARS = 4000

_RESERVED_EVENT_TOPICS = frozenset({"task.start", "task.resume"})

# Worker.backend -> Ralph's own `backend.type` identifier. Only remapped
# where they differ; anything not listed here is passed through unchanged
# (e.g. "codex" already matches Ralph's own naming).
#
# "vibe" maps to Ralph's own "custom" backend type deliberately: Ralph's
# hats mechanism (used by every other backend below) rejects any backend
# type it does not natively know — VERIFIED in docs/VIBE_SPIKE.md §5
# (`ralph doctor` reports "Unknown hat backend" for anything other than
# Ralph's fixed native list). Only Ralph's top-level, solo-mode
# `cli.backend: "custom"` accepts an arbitrary command — see
# `_SOLO_MODE_BACKENDS`/`_BACKEND_COMMANDS` below, which drive
# `_write_runtime_config`/`_build_ralph_args` to skip hats.yml entirely
# for these backends, exactly as the spike's real experiment required.
_RALPH_BACKEND_TYPE: dict[str, str] = {
    "claude_code": "claude",
    "vibe": "custom",
}

# Backends that must run via Ralph's solo/no-hats "custom" mechanism
# instead of the hats.yml path every native backend uses (see comment
# above). Each entry's command is the thin bridge script that translates
# Ralph's file-path-argument convention into that backend's own CLI
# invocation — never a second execution engine, never business-event
# logic of its own (Ralph's existing `.ralph/events-*.jsonl` reading is
# unchanged and unaware of this distinction).
_SOLO_MODE_BACKENDS: frozenset[str] = frozenset({"vibe"})
_BACKEND_COMMANDS: dict[str, Path] = {
    "vibe": Path(__file__).resolve().parent / "vibe_ralph_bridge.py",
}


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


class RalphExecutionEngineError(Exception):
    """Base for RalphExecutionEngine domain errors."""


class ReservedEventTopicError(RalphExecutionEngineError):
    """Raised when a request uses a topic reserved for Ralph's own coordinator."""

    def __init__(self, topics: Sequence[str]) -> None:
        super().__init__(
            f"reserved Ralph event topic(s) cannot be used as custom events: {sorted(topics)!r}"
        )
        self.topics = tuple(topics)


class UnsupportedBackendError(RalphExecutionEngineError):
    """Raised when a Worker's backend has no known Ralph CLI-arg translation."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"unsupported backend for Ralph translation: {backend!r}")


class UnsupportedProfileOptionError(RalphExecutionEngineError):
    """Raised when an ExecutionProfile option has no honest translation for a
    backend (e.g. ``reasoning_effort`` for a backend with no such CLI
    concept) — distinct from ``UnsupportedBackendError``: the backend
    itself is known, only this specific option is not representable."""

    def __init__(self, backend: str, option: str, value: str) -> None:
        super().__init__(f"{backend!r} backend does not support {option}={value!r}")
        self.backend = backend
        self.option = option
        self.backend = backend


class RalphLaunchError(RalphExecutionEngineError):
    """Raised when the ralph subprocess could not be started at all."""


class RalphTimeoutError(RalphExecutionEngineError):
    """Internal: raised when the ralph subprocess exceeds its timeout."""


class RalphEventParseError(RalphExecutionEngineError):
    """Raised when a Ralph events JSONL line is invalid or missing required fields."""


def _build_backend_args(backend: str, model: str, reasoning_effort: str | None) -> list[str]:
    if backend == "codex":
        args = ["--model", model]
        if reasoning_effort:
            args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        return args
    if backend == "claude_code":
        # `claude --effort <low|medium|high|xhigh|max>` is a real, native
        # Claude Code CLI flag (confirmed via `claude --help` — not a
        # codex-style `-c key=value` override, Claude Code takes it
        # directly), passed through verbatim as an extra hat backend arg,
        # the same mechanism already validated for codex's
        # `model_reasoning_effort` (see docs/SPIKE_RALPH.md). Omitted
        # entirely when unset, matching the codex branch above, so a
        # profile that never set reasoning_effort (e.g. today's Claude
        # profiles in config/workers.yaml) transmits nothing extra.
        args = ["--model", model]
        if reasoning_effort:
            args += ["--effort", reasoning_effort]
        return args
    if backend == "vibe":
        # Vibe has no `--model`/reasoning-effort CLI flag at all (VERIFIED
        # via `vibe --help`, docs/VIBE_SPIKE.md §6/§13) — model selection
        # is env/config-driven (`VIBE_ACTIVE_MODEL`). `--model <name>` here
        # is consumed by `vibe_ralph_bridge.py`, never forwarded to `vibe`
        # itself as a literal flag. reasoning_effort has no honest
        # translation for this backend — failing closed rather than
        # silently dropping a configured value.
        if reasoning_effort:
            raise UnsupportedProfileOptionError(backend, "reasoning_effort", reasoning_effort)
        return ["--model", model]
    raise UnsupportedBackendError(backend)


def _ralph_backend_type(backend: str) -> str:
    return _RALPH_BACKEND_TYPE.get(backend, backend)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """What to run, and how to judge it — nothing about which worker to pick."""

    execution_id: str
    task_id: str
    worker: Worker
    role: str
    workspace: Path
    instructions: str
    initial_event_topic: str
    success_topics: frozenset[str]
    failure_topics: frozenset[str]
    timeout_seconds: float
    model: str
    reasoning_effort: str | None = None
    initial_event_payload: str | None = None

    def __post_init__(self) -> None:
        for name in ("execution_id", "task_id", "role", "instructions", "initial_event_topic", "model"):
            _require_non_empty_str(getattr(self, name), field_name=f"ExecutionRequest.{name}")
        if not isinstance(self.worker, Worker):
            raise TypeError(f"ExecutionRequest.worker must be a Worker, got {type(self.worker)!r}")
        if self.reasoning_effort is not None:
            _require_non_empty_str(self.reasoning_effort, field_name="ExecutionRequest.reasoning_effort")
        object.__setattr__(self, "workspace", Path(self.workspace))
        object.__setattr__(self, "success_topics", frozenset(self.success_topics))
        object.__setattr__(self, "failure_topics", frozenset(self.failure_topics))

        reserved_used = (
            {self.initial_event_topic} | self.success_topics | self.failure_topics
        ) & _RESERVED_EVENT_TOPICS
        if reserved_used:
            raise ReservedEventTopicError(sorted(reserved_used))

        overlap = self.success_topics & self.failure_topics
        if overlap:
            raise ValueError(f"success_topics and failure_topics overlap: {sorted(overlap)!r}")
        if not self.success_topics and not self.failure_topics:
            raise ValueError("at least one success or failure topic must be provided")

        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                f"ExecutionRequest.timeout_seconds must be a positive number, got {self.timeout_seconds!r}"
            )
        if self.initial_event_payload is not None:
            _require_non_empty_str(
                self.initial_event_payload, field_name="ExecutionRequest.initial_event_payload"
            )


@dataclass(frozen=True, slots=True)
class RalphEvent:
    """One line of a Ralph events-*.jsonl file, as actually observed in practice.

    ``iteration``/``hat`` are present on Ralph's own loop-generated events
    (e.g. ``task.start``, ``iteration.summary``, ``loop.terminate``) but
    absent on plain ``ralph emit``-published custom events — both shapes
    are real and must be tolerated.
    """

    topic: str
    timestamp: datetime
    payload: str | None = None
    iteration: int | None = None
    hat: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The outcome of one ``RalphExecutionEngine.execute()`` call."""

    record: ExecutionRecord
    events: tuple[RalphEvent, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")


def _parse_ralph_timestamp(value: str) -> datetime:
    # Ralph emits nanosecond-precision fractional seconds
    # (e.g. "...088945824+00:00"); datetime.fromisoformat only supports up
    # to microseconds on some Python versions, so truncate defensively.
    match = _FRACTIONAL_SECONDS_RE.search(value)
    if match and len(match.group(1)) > 6:
        value = value[: match.start(1)] + match.group(1)[:6] + value[match.end(1) :]
    return datetime.fromisoformat(value)


def parse_ralph_events(lines: Sequence[str]) -> list[RalphEvent]:
    """Parse Ralph's events-*.jsonl lines into RalphEvent, offline-testable.

    Tolerates the optional ``iteration``/``hat`` fields; rejects (raises
    ``RalphEventParseError``) any line that is not valid JSON or is missing
    the fields a verdict actually depends on (``topic``, ``ts``).
    """
    events: list[RalphEvent] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RalphEventParseError(f"invalid JSON line in Ralph events file: {exc}") from exc
        if not isinstance(data, dict) or "topic" not in data or "ts" not in data:
            raise RalphEventParseError(
                f"Ralph event line missing required 'topic'/'ts' fields: {data!r}"
            )
        try:
            timestamp = _parse_ralph_timestamp(data["ts"])
        except (ValueError, TypeError) as exc:
            raise RalphEventParseError(f"invalid Ralph event timestamp {data['ts']!r}: {exc}") from exc
        events.append(
            RalphEvent(
                topic=data["topic"],
                timestamp=timestamp,
                payload=data.get("payload"),
                iteration=data.get("iteration"),
                hat=data.get("hat"),
            )
        )
    return events


def _determine_verdict(
    events: Sequence[RalphEvent], *, success_topics: frozenset[str], failure_topics: frozenset[str]
) -> bool | None:
    """Returns True (success), False (failure), or None (no verdict found).

    The first event, in order, whose topic matches either set wins — this
    slice runs a single hat for a single terminal decision, so the first
    match is also the only one expected in practice.
    """
    for event in events:
        if event.topic in failure_topics:
            return False
        if event.topic in success_topics:
            return True
    return None


def _git_head_sha(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _read_optional_text(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None


def _read_ralph_loop_id(workspace: Path) -> str | None:
    return _read_optional_text(workspace / ".ralph" / "current-loop-id")


def _read_ralph_events(workspace: Path) -> list[RalphEvent]:
    events_relative_path = _read_optional_text(workspace / ".ralph" / "current-events")
    if events_relative_path is None:
        return []
    try:
        lines = (workspace / events_relative_path).read_text().splitlines()
    except OSError:
        return []
    return parse_ralph_events(lines)


def _bounded(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= _MAX_CAPTURED_OUTPUT_CHARS:
        return text
    return text[:_MAX_CAPTURED_OUTPUT_CHARS] + "...<truncated>"


def _yaml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_yaml_str(v) for v in values) + "]"


def _yaml_indented_block(text: str, *, indent: int) -> str:
    pad = " " * indent
    lines = text.rstrip("\n").splitlines() or [""]
    return "\n".join(f"{pad}{line}" if line else pad.rstrip() for line in lines)


def _render_ralph_config(
    *,
    backend_type: str,
    backend_args: list[str],
    prompt_path: Path,
    max_runtime_seconds: int,
    backend_command: str | None = None,
) -> str:
    # `backend_command` is only set for solo-mode backends (see
    # `_SOLO_MODE_BACKENDS`) — Ralph's "custom" backend requires an
    # explicit `cli.command` (VERIFIED: `ralph doctor` fails closed
    # without it, docs/VIBE_SPIKE.md §5); native backends never set this,
    # Ralph resolves their binary itself.
    command_line = f"  command: {_yaml_str(backend_command)}\n" if backend_command else ""
    return (
        "cli:\n"
        f"  backend: {_yaml_str(backend_type)}\n"
        f"{command_line}"
        f"  args: {_yaml_list(backend_args)}\n"
        "\n"
        "event_loop:\n"
        f"  prompt_file: {_yaml_str(str(prompt_path))}\n"
        '  completion_promise: "LOOP_COMPLETE"\n'
        f"  max_iterations: {DEFAULT_MAX_ITERATIONS}\n"
        f"  max_runtime_seconds: {max_runtime_seconds}\n"
    )


def _render_hats_config(
    *,
    initial_topic: str,
    hat_name: str,
    hat_description: str,
    publishes: Sequence[str],
    backend_type: str,
    backend_args: list[str],
    instructions: str,
) -> str:
    publishes_yaml = "\n".join(f"      - {_yaml_str(topic)}" for topic in publishes)
    instructions_block = _yaml_indented_block(instructions, indent=6)
    return (
        "event_loop:\n"
        f"  starting_event: {_yaml_str(initial_topic)}\n"
        "\n"
        "hats:\n"
        "  worker:\n"
        f"    name: {_yaml_str(hat_name)}\n"
        f"    description: {_yaml_str(hat_description)}\n"
        "    triggers:\n"
        f"      - {_yaml_str(initial_topic)}\n"
        "    publishes:\n"
        f"{publishes_yaml}\n"
        "    backend:\n"
        f"      type: {_yaml_str(backend_type)}\n"
        f"      args: {_yaml_list(backend_args)}\n"
        "    instructions: |\n"
        f"{instructions_block}\n"
    )


def _write_runtime_config(
    runtime_dir: Path, request: ExecutionRequest
) -> tuple[Path, Path | None, Path]:
    backend = request.worker.backend
    backend_type = _ralph_backend_type(backend)
    backend_args = _build_backend_args(backend, request.model, request.reasoning_effort)
    solo_mode = backend in _SOLO_MODE_BACKENDS

    prompt_path = runtime_dir / "PROMPT.md"
    prompt_path.write_text(request.instructions)

    config_path = runtime_dir / "ralph.yml"
    config_path.write_text(
        _render_ralph_config(
            backend_type=backend_type,
            backend_args=backend_args,
            prompt_path=prompt_path,
            max_runtime_seconds=max(1, int(request.timeout_seconds)),
            backend_command=str(_BACKEND_COMMANDS[backend]) if solo_mode else None,
        )
    )

    if solo_mode:
        # Ralph's hats mechanism rejects solo-mode backends entirely
        # (VERIFIED, docs/VIBE_SPIKE.md §5) — no hats.yml is written or
        # referenced for these; business events are still read from
        # `.ralph/events-*.jsonl` exactly as for every other backend (see
        # `_read_ralph_events`, which is hat-agnostic already).
        return config_path, None, prompt_path

    hats_path = runtime_dir / "hats.yml"
    hats_path.write_text(
        _render_hats_config(
            initial_topic=request.initial_event_topic,
            hat_name=request.worker.display_name,
            hat_description=(
                f"Runs worker {request.worker.worker_id!r} in role {request.role!r} "
                f"for execution {request.execution_id!r}."
            ),
            publishes=sorted(request.success_topics | request.failure_topics),
            backend_type=backend_type,
            backend_args=backend_args,
            instructions=request.instructions,
        )
    )
    return config_path, hats_path, prompt_path


SubprocessRunner = Callable[[Sequence[str], Path, float], Awaitable[tuple[int, bytes, bytes]]]


async def _default_subprocess_runner(
    args: Sequence[str], cwd: Path, timeout: float
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RalphTimeoutError(f"ralph run timed out after {timeout}s")
    return process.returncode, stdout, stderr


class RalphExecutionEngine:
    """Runs one already-selected Worker via Ralph and finalizes its audit record."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        *,
        ralph_binary: str = DEFAULT_RALPH_BINARY,
        clock: Clock | None = None,
        subprocess_runner: SubprocessRunner | None = None,
    ) -> None:
        self._execution_store = execution_store
        self._ralph_binary = ralph_binary
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run_subprocess = subprocess_runner or _default_subprocess_runner

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        git_sha_before = _git_head_sha(request.workspace)

        record = self._execution_store.create(
            execution_id=request.execution_id,
            task_id=request.task_id,
            worker_id=request.worker.worker_id,
            provider=request.worker.provider,
            backend=request.worker.backend,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            role=request.role,
            git_sha_before=git_sha_before,
            started_at=self._clock(),
        )

        runtime_dir = Path(tempfile.mkdtemp(prefix=f"ralph-exec-{request.execution_id}-"))
        try:
            try:
                args = self._build_ralph_args(runtime_dir, request)
                exit_code, stdout, stderr = await self._run_subprocess(
                    args, request.workspace, request.timeout_seconds
                )
            except RalphTimeoutError:
                loop_id = _read_ralph_loop_id(request.workspace)
                updated = self._execution_store.mark_interrupted(
                    request.execution_id, ralph_loop_id=loop_id, finished_at=self._clock()
                )
                return ExecutionResult(record=updated)
            except FileNotFoundError as exc:
                self._execution_store.mark_failed(request.execution_id, finished_at=self._clock())
                raise RalphLaunchError(f"could not launch {self._ralph_binary!r}: {exc}") from exc
            except RalphExecutionEngineError:
                # e.g. UnsupportedBackendError raised while building the
                # temporary config, before any subprocess was even
                # launched: a known engine-level failure. Finalize the
                # record before propagating — it must never stay RUNNING.
                self._execution_store.mark_failed(request.execution_id, finished_at=self._clock())
                raise
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)

        git_sha_after = _git_head_sha(request.workspace)
        ralph_loop_id = _read_ralph_loop_id(request.workspace)

        try:
            events = _read_ralph_events(request.workspace)
        except RalphEventParseError:
            self._execution_store.mark_failed(
                request.execution_id, exit_code=exit_code, git_sha_after=git_sha_after,
                ralph_loop_id=ralph_loop_id, finished_at=self._clock(),
            )
            raise

        verdict = _determine_verdict(
            events, success_topics=request.success_topics, failure_topics=request.failure_topics
        )

        if verdict is True:
            updated = self._execution_store.mark_succeeded(
                request.execution_id, exit_code=exit_code, git_sha_after=git_sha_after,
                ralph_loop_id=ralph_loop_id, finished_at=self._clock(),
            )
        else:
            # verdict is False (explicit failure event) or None (no
            # reliable terminal business event at all) — both finalize as
            # FAILED. exit_code is preserved as audit data either way, it
            # never overrides this business-event-driven decision.
            updated = self._execution_store.mark_failed(
                request.execution_id, exit_code=exit_code, git_sha_after=git_sha_after,
                ralph_loop_id=ralph_loop_id, finished_at=self._clock(),
            )

        return ExecutionResult(
            record=updated,
            events=tuple(events),
            exit_code=exit_code,
            stdout=_bounded(stdout),
            stderr=_bounded(stderr),
        )

    def _build_ralph_args(self, runtime_dir: Path, request: ExecutionRequest) -> list[str]:
        config_path, hats_path, prompt_path = _write_runtime_config(runtime_dir, request)
        args = [self._ralph_binary, "run", "-a", "-q", "-c", str(config_path)]
        if hats_path is not None:
            args += ["-H", str(hats_path)]
        args += ["-P", str(prompt_path)]
        return args
