# BMAX Ollama operator notes (P2)

These notes describe the local setup observed on 2026-10-31. They are not a
record of the required two-session AIDO + Ralph acceptance. The local evidence
is in `.local-p2/bmax-evidence.txt` (untracked, machine-local): Ollama 0.40.13,
Codex 0.166.0, Ralph 2.10.1, and one successful Qwen tool-use smoke test.
The Ollama service was not changed.

## Configure the local route

1. Use an Ollama endpoint reachable from BMAX. Set `CODEX_OSS_BASE_URL` in
   the environment that launches AIDO/Ralph to that endpoint's `/v1` URL,
   for example `http://<ollama-host>:11434/v1`. Do not put a machine address
   in `aido.yaml` or the shared worker registry. `OLLAMA_BASE_URL` did not
   route Codex in the observed smoke test. The engine probes `/api/tags` on
   the same host after removing `/v1`.
2. In the **global** `workers.yaml`, declare two enabled workers with distinct
   `worker_id` values (`qwen_developer`, `qwen_reviewer`), `provider: ollama`,
   `backend: codex`, the `development` capability, and profiles using a model
   that `/api/tags` actually lists. The observed model was `qwen3.5:9b`.
   Keep cloud workers in the registry for fallback. Add the following global
   opt-in only when local-first selection is wanted:

   ```yaml
   selection:
     preferred_provider: ollama
   ```

   This preference belongs to the global registry, not a project manifest.
   It does not override availability, capability, or author/reviewer
   separation. See [worker selection](PROJECT_CONFIG.md#worker-selection).
3. The engine passes `--oss --local-provider ollama`,
   `-c model_provider="ollama"`, `--disable goals`, `--disable apps`, and
   `-c web_search="disabled"` to Codex for Ollama workers. The provider setting
   was needed by Codex 0.166.0: `--oss` alone caused `providerMismatch` in
   the local smoke test. A machine-local Codex profile named `bmax` was also
   created with `model_provider = "ollama"`, `model = "qwen3.5:9b"`, disabled
   web search, goals, and apps. The engine's explicit arguments are the
   route used by AIDO; the profile is a local diagnostic convenience.

## Context and acceptance limits

The smoke test's `/api/ps` reported an actual context of **4096**. Warming
the model at 16384 did not keep that context for later Codex requests. Plan
small work items and inspect `/api/ps` during the real run; do not claim 16K
capacity from a warmup alone. Avoid running two model sessions concurrently
on this 32 GB machine. A pulled model need not already be loaded in RAM for
the availability probe to pass.

The first governed attempt stalled while Ralph injected its built-in skill
reference into that 4096-token context. For Ollama Codex workers, AIDO now
sets `skills.enabled: false` in the temporary Ralph config for each execution.
The task prompt still carries the required `work.completed` and `work.failed`
protocol. This change needs a fresh two-session run before acceptance.

Before calling P2 accepted, run the explicit BMAX recipe from
[WI-P2-04](../ROADMAP.md)
in a disposable Git repository. Capture two successive Qwen worker sessions
through AIDO + Ralph, their distinct identities and audited commits, Ralph
completion events, and deterministic QA tied to the exact commit SHA before
merge. Exercise an unavailable Ollama endpoint and a missing Ralph completion
event; neither may produce PASS. Confirm unknown local quota stays unknown
and cloud workers remain selectable. A single Codex smoke test cannot satisfy
these gates.

## Rollback

Remove `selection.preferred_provider: ollama` from the global registry to
restore the previous routing order. To exclude local workers entirely, set
their `enabled` fields to `false` in that registry. Start a new AIDO run or
resume the affected work item through its normal recovery path, then verify
the selected worker and QA state. Leave Ollama running for HA-AI; rollback
does not require stopping or reconfiguring its service. Keep the existing
engine version until the governed QA, BMAX recipe, and release artifacts
have passed.
