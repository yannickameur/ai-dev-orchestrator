#!/usr/bin/env python3
"""gravity_ralph_bridge — bridges Ralph's "custom" solo backend to Gravity's `agy` CLI.

Ralph's hats mechanism (used by every other backend in this project) does
not accept a "custom" backend type at all (see the Vibe spike (Git history)) —
only Ralph's top-level, solo-mode `cli.backend: "custom"` does, and that
mechanism invokes `<command> <configured args...> <prompt-file-path>`:
the final argument is a full instructional sentence with the prompt file's
path embedded at its end, never the prompt text itself and never
delivered via stdin (VERIFIED for this exact Ralph mechanism by
`vibe_ralph_bridge.py`, independent of which CLI is being bridged to —
see that module's own docstring). `agy`'s own `-p`/`--print` flag expects
literal text, not a file path — this script is exactly, and only, that
translation, on the same model as `vibe_ralph_bridge.py` (ROADMAP.md,
P19).

This is a thin, provider/backend-specific bridge, not a second execution
engine: it does not select workers, does not decide success/failure (Ralph
still reads business events from ``.ralph/events-*.jsonl`` exactly as it
does for every other backend — this script never touches that mechanism),
and does not implement any orchestration logic of its own.

Argv shape (see ``_build_backend_args("gravity", ...)`` in
``ralph_execution_engine.py``, the only caller that constructs it):

    gravity_ralph_bridge.py [--model NAME] [--permission-mode {standard,unrestricted}] <final argument>

VERIFIED against the real `agy` CLI (version 1.2.12, ROADMAP.md P19
Phase A, 2026-09-28):

- ``--model NAME``, if present, is forwarded verbatim as `agy`'s own
  `--model` flag — unlike Vibe, `agy --model <id>` is a real, working CLI
  flag (`agy models` lists real, selectable ids), never an env-var
  workaround.
- ``--permission-mode {standard,unrestricted}``, if present, is the
  generic ``ExecutionPermissionMode`` (P12, ROADMAP.md) chosen by
  ``RalphExecutionEngine``, translated here — and only here — into real,
  VERIFIED `agy` CLI flags:

  - ``--mode=accept-edits`` is passed UNCONDITIONALLY, for every
    invocation regardless of permission mode — VERIFIED (Phase A): with
    this flag alone (no ``--dangerously-skip-permissions``), a real,
    disposable-repo test created a file non-interactively, exit code
    ``0``, no interactive prompt reached at all. This is the STANDARD
    mechanism, and it is honestly non-bypassing.
  - ``standard`` -> no extra flag: exactly the STANDARD invocation just
    verified above.
  - ``unrestricted`` -> ``--dangerously-skip-permissions`` added: also
    VERIFIED (Phase A) to work non-interactively, exit code ``0``.
  - Omitted entirely (no ``--permission-mode`` argv given at all): this
    bridge never defaults to a bypass — the STANDARD, non-bypassing
    invocation is used, the safe choice, unlike
    ``vibe_ralph_bridge.py``'s own legacy pre-P12 default (that default
    exists only for backward compatibility with callers that predate
    ExecutionPermissionMode; `gravity_ralph_bridge.py` has no such
    history, so there is no reason to default to a bypass here).
- No `--output-format`/`--output` flag is added: `agy`'s default text
  output was VERIFIED (Phase A) to work correctly in this exact
  non-interactive invocation shape; adding a flag beyond what was
  actually verified/requested would not be REUSE FIRST, it would be
  scope creep copied from `vibe_ralph_bridge.py` without a demonstrated
  need here.
"""

from __future__ import annotations

import os
import subprocess
import sys

GRAVITY_BINARY = "agy"


class BridgeArgError(Exception):
    """Raised for any malformed bridge argv — caught once in ``main()``."""


def permission_args(mode: str) -> list[str]:
    """Pure, offline-testable translation — VERIFIED via a real, disposable
    controlled test (`agy` 1.2.12, ROADMAP.md P19 Phase A):

    - ``standard`` -> no extra flag: `--mode=accept-edits` alone already
      runs fully non-interactively, no approval bypass.
    - ``unrestricted`` -> `--dangerously-skip-permissions`: also verified
      non-interactive.
    """
    if mode == "standard":
        return []
    if mode == "unrestricted":
        return ["--dangerously-skip-permissions"]
    raise BridgeArgError(f"unsupported --permission-mode {mode!r}")


def _extract_flag_value(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        raise BridgeArgError(f"{flag} given with no value")
    return args[index + 1]


def build_gravity_command(argv: list[str], *, prompt_text: str) -> list[str]:
    """Pure: parses this bridge's own argv + a prompt text into the exact
    real ``agy`` command line — everything this module does that is
    meaningfully offline-testable, with no file I/O or subprocess launch.
    """
    rest = argv[:-1]

    model = _extract_flag_value(rest, "--model")
    mode = _extract_flag_value(rest, "--permission-mode")
    # Never defaults to a bypass when `--permission-mode` is omitted — see
    # module docstring.
    perm_args = permission_args(mode) if mode is not None else []

    command = [GRAVITY_BINARY, "-p", prompt_text, "--mode=accept-edits", *perm_args]
    if model:
        command += ["--model", model]
    return command


def main(argv: list[str]) -> int:
    if not argv:
        print("gravity_ralph_bridge: missing prompt file path argument", file=sys.stderr)
        return 2

    final_arg = argv[-1]
    # Ralph's real argv is an instructional sentence, not a bare path (see
    # module docstring) — the path is its last whitespace-separated token.
    prompt_file = final_arg.split()[-1] if final_arg.split() else final_arg

    try:
        prompt_text = open(prompt_file, encoding="utf-8").read()
    except OSError as exc:
        print(f"gravity_ralph_bridge: could not read prompt file {prompt_file!r} (extracted from final argument {final_arg!r}): {exc}", file=sys.stderr)
        return 2

    try:
        command = build_gravity_command(argv, prompt_text=prompt_text)
    except BridgeArgError as exc:
        print(f"gravity_ralph_bridge: {exc}", file=sys.stderr)
        return 2

    result = subprocess.run(command, env=os.environ.copy())
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
