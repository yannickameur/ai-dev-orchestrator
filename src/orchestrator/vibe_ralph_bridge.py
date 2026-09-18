#!/usr/bin/env python3
"""vibe_ralph_bridge — bridges Ralph's "custom" solo backend to Vibe's CLI.

Ralph's hats mechanism (used by every other backend in this project) does
not accept a "custom" backend type at all (see docs/VIBE_SPIKE.md §5) —
only Ralph's top-level, solo-mode `cli.backend: "custom"` does, and that
mechanism invokes ``<command> <configured args...> <prompt-file-path>``:
the final argument is a PATH to a file containing the full prompt, never
the prompt text itself and never delivered via stdin (VERIFIED in the
spike). Vibe's own ``-p [TEXT]`` flag expects literal text, not a file
path — this script is exactly, and only, that translation.

This is a thin, provider/backend-specific bridge, not a second execution
engine: it does not select workers, does not decide success/failure (Ralph
still reads business events from ``.ralph/events-*.jsonl`` exactly as it
does for every other backend — this script never touches that mechanism),
and does not implement any orchestration logic of its own.

Argv shape (see ``_build_backend_args("vibe", ...)`` in
``ralph_execution_engine.py``, the only caller that constructs it):

    vibe_ralph_bridge.py [--model NAME] <final argument>

The final argument is NOT a bare path, despite the module name above —
VERIFIED against real Ralph 2.10.1 (`ralph_adapters::cli_executor` debug
log): it is a full instructional sentence with the path embedded at the
end, e.g. ``"Please read and execute the task in /tmp/.tmpXXXXXX"``. This
is a more precise finding than the spike's own characterization ("a PATH
to a file") — paths never contain whitespace, so the last
whitespace-separated token of that sentence is taken as the real path.

``--model NAME``, if present, is translated to the ``VIBE_ACTIVE_MODEL``
environment variable (Vibe has no ``--model`` CLI flag — model selection
is config/env-driven, per ``vibe --help``) for the spawned ``vibe``
process only; it is never applied globally. The sentinel value
``vibe-default`` (see ``DEFAULT_MODEL_SENTINEL`` / ``config/workers.yaml``)
means "no override" — the spike never established a verified Mistral
model identifier to hard-code, so this bridge honestly defers to
whatever Vibe itself already resolves as its own default rather than
fabricating one.

``--permission-mode {standard,unrestricted}``, if present, is the generic
``ExecutionPermissionMode`` (P12, ROADMAP.md §13) chosen by
``RalphExecutionEngine``, translated here — and only here — into real,
VERIFIED Vibe CLI flags (``vibe --help``, Vibe 2.25.4):

- ``--trust`` is passed UNCONDITIONALLY, for every invocation regardless
  of permission mode. VERIFIED: it only "trust[s] the working directory
  for this invocation" (skips the one-time directory-trust prompt) and is
  explicitly documented as the mechanism to "use ... for non-interactive
  automation" — it has nothing to do with tool-call approval. Withholding
  it would make even STANDARD hang on a prompt nothing can answer, which
  is not what STANDARD means (denying/failing a would-be-approved action
  deterministically is; hanging forever is not).
- ``standard`` -> ``--agent ask``: Vibe's real, documented builtin agent
  that requires approval for each tool call — the verified STANDARD
  mechanism. In non-interactive (``-p``) mode with nothing able to answer
  an approval prompt, this is expected to fail/deny rather than silently
  proceed — an honest STANDARD limitation, not a bug.
- ``unrestricted`` -> ``--auto-approve`` (equivalently ``--yolo``):
  "Approves all tool calls without prompting" — the verified UNRESTRICTED
  mechanism. This is exactly what this bridge used to pass
  unconditionally before P12; it is now gated on an explicit request.
- Omitted entirely (no ``--permission-mode`` argv given at all): the
  legacy, pre-P12 behavior is preserved unchanged for any caller that has
  not yet adopted an explicit permission policy — ``--auto-approve`` is
  still passed, matching this bridge's original, unconditional behavior.
  New callers should always pass an explicit mode; this exists only for
  backward compatibility with ``RalphExecutionEngine`` instances
  constructed without a ``permission_mode``.
"""

from __future__ import annotations

import os
import subprocess
import sys

VIBE_BINARY = "vibe"
DEFAULT_MODEL_SENTINEL = "vibe-default"

# Legacy behavior (pre-P12): this bridge always ran with `--auto-approve`
# unconditionally. Preserved ONLY for a caller that omits `--permission-mode`
# entirely (an ExecutionPermissionMode-unconfigured RalphExecutionEngine) —
# never for a caller that explicitly requests "standard". New callers should
# always pass an explicit mode.
_LEGACY_PERMISSION_ARGS = ["--auto-approve"]


class BridgeArgError(Exception):
    """Raised for any malformed bridge argv — caught once in ``main()``."""


def permission_args(mode: str) -> list[str]:
    """Pure, offline-testable translation — VERIFIED via `vibe --help`
    (Vibe 2.25.4):

    - "ask" is a real, documented builtin agent that requires approval for
      each tool call — the STANDARD mechanism.
    - "--auto-approve" ("Approves all tool calls without prompting") is
      the UNRESTRICTED mechanism.
    """
    if mode == "standard":
        return ["--agent", "ask"]
    if mode == "unrestricted":
        return ["--auto-approve"]
    raise BridgeArgError(f"unsupported --permission-mode {mode!r}")


def _extract_flag_value(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        raise BridgeArgError(f"{flag} given with no value")
    return args[index + 1]


def build_vibe_command(argv: list[str], *, prompt_text: str) -> tuple[list[str], str | None]:
    """Pure: parses this bridge's own argv + a prompt text into the exact
    real ``vibe`` command line, and the model env override (if any) —
    everything this module does that is meaningfully offline-testable,
    with no file I/O or subprocess launch.

    Returns ``(command, model_env_override)``.
    """
    final_arg, rest = argv[-1], argv[:-1]

    model = _extract_flag_value(rest, "--model")
    mode = _extract_flag_value(rest, "--permission-mode")
    # `--trust` is unconditional regardless of permission mode — VERIFIED
    # to only skip the one-time directory-trust prompt (required for any
    # non-interactive automation), never tool-call approval. See module
    # docstring.
    perm_args = permission_args(mode) if mode is not None else _LEGACY_PERMISSION_ARGS

    command = [VIBE_BINARY, "-p", prompt_text, "--trust", *perm_args, "--output", "json"]
    model_env_override = model if model and model != DEFAULT_MODEL_SENTINEL else None
    return command, model_env_override


def main(argv: list[str]) -> int:
    if not argv:
        print("vibe_ralph_bridge: missing prompt file path argument", file=sys.stderr)
        return 2

    final_arg = argv[-1]
    # Ralph's real argv is an instructional sentence, not a bare path (see
    # module docstring) — the path is its last whitespace-separated token.
    prompt_file = final_arg.split()[-1] if final_arg.split() else final_arg

    try:
        prompt_text = open(prompt_file, encoding="utf-8").read()
    except OSError as exc:
        print(f"vibe_ralph_bridge: could not read prompt file {prompt_file!r} (extracted from final argument {final_arg!r}): {exc}", file=sys.stderr)
        return 2

    try:
        command, model_env_override = build_vibe_command(argv, prompt_text=prompt_text)
    except BridgeArgError as exc:
        print(f"vibe_ralph_bridge: {exc}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    if model_env_override:
        env["VIBE_ACTIVE_MODEL"] = model_env_override

    result = subprocess.run(command, env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
