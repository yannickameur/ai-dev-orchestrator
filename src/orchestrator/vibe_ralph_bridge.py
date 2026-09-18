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
"""

from __future__ import annotations

import os
import subprocess
import sys

VIBE_BINARY = "vibe"
DEFAULT_MODEL_SENTINEL = "vibe-default"


def main(argv: list[str]) -> int:
    if not argv:
        print("vibe_ralph_bridge: missing prompt file path argument", file=sys.stderr)
        return 2

    final_arg, rest = argv[-1], argv[:-1]
    # Ralph's real argv is an instructional sentence, not a bare path (see
    # module docstring) — the path is its last whitespace-separated token.
    prompt_file = final_arg.split()[-1] if final_arg.split() else final_arg

    model: str | None = None
    if "--model" in rest:
        index = rest.index("--model")
        if index + 1 >= len(rest):
            print("vibe_ralph_bridge: --model given with no value", file=sys.stderr)
            return 2
        model = rest[index + 1]

    try:
        prompt_text = open(prompt_file, encoding="utf-8").read()
    except OSError as exc:
        print(f"vibe_ralph_bridge: could not read prompt file {prompt_file!r} (extracted from final argument {final_arg!r}): {exc}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    if model and model != DEFAULT_MODEL_SENTINEL:
        env["VIBE_ACTIVE_MODEL"] = model

    command = [VIBE_BINARY, "-p", prompt_text, "--trust", "--auto-approve", "--output", "json"]
    result = subprocess.run(command, env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
