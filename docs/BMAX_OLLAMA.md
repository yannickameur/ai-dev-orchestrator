# BMAX Ollama operator notes (P2)

These notes describe the BMAX setup and governed acceptance verified on
2026-11-01 with Ollama 0.40.13, Codex 0.166.0 and Ralph 2.10.1.
Engine [v0.2.2](https://github.com/yannickameur/ai-dev-orchestrator/releases/tag/v0.2.2)
is published with wheel, sdist and verified SHA256SUMS after PR #65 and green
Python 3.10/3.12 CI. The successful two-session evidence is in
[P2_ACCEPTANCE.json](P2_ACCEPTANCE.json). Earlier attempts below explain the
configuration decisions; they are not additional successful acceptance runs.
The shared HA-AI Ollama service was not changed.

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
The next governed attempt still spent its 4096-token context on Ralph task
orientation without making a Git edit. The temporary config now also sets
`tasks.enabled: false` and `memories.enabled: false` for Ollama Codex workers;
Ralph 2.10.1 keys that orientation to the memory setting. Cloud workers retain
Ralph's defaults. This does not suppress business completion events.
The task prompt still carries the required `work.completed` and `work.failed`
protocol. The successful two-session run below validates this configuration.
Ralph 2.10.1 treats `event_loop.completion_promise` as an emitted event topic.
The temporary Ollama config now uses the request's success topic and its
failure topic as `cancellation_promise` when each verdict has one topic. The
engine still requires an actual business event; final text alone cannot pass.
The first 16K isolated-endpoint attempt produced a developer commit and
`work.completed`, but the earlier `LOOP_COMPLETE` promise kept Ralph running.
That interrupted attempt is diagnostic evidence, not a two-session PASS.

To reproduce acceptance, run the explicit BMAX recipe from
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
does not require stopping or reconfiguring its service. Preserve the previous
installation when deploying the validated release;
restore its launcher and compatible global registry together for rollback.

## Verified BMAX acceptance (2026-11-01)

`LIVE-QWEN-06` completed against frozen engine `d7f08b1`: Qwen Developer
committed `f745eab`, then distinct Qwen Reviewer corrected Unicode behavior
in `d66b5bd`. The operator clarified the existing Unicode criterion during
review. Deterministic `python3 -m unittest -q` passed on `d66b5bd`; AIDO
merged that exact SHA and tagged `feature/LIVE-QWEN-06/done`. Six additional
read-only contract cases passed. No protected recipe test was changed.
Structured, reasoning-free evidence: [P2_ACCEPTANCE.json](P2_ACCEPTANCE.json).

The original service truncated 5033/5371-token inputs to 2050 tokens.
Successful acceptance used a separate temporary Ollama process on a loopback
endpoint, the same installed model/digest, `OLLAMA_CONTEXT_LENGTH=16384`,
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_VULKAN=1` and
`OLLAMA_IGPU_ENABLE=1`. `/api/ps` confirmed 16K and the Radeon 780M. The
HA-AI system service and model tags were unchanged. Use an explicit, local
endpoint configuration and existing model storage; do not change a shared
service to reproduce this setup. Stop the dedicated process/unit to undo it.

Both Ollama and OpenAI were observed available during acceptance. A separate
failed-endpoint probe reported Ollama `provider_error`, no quota windows,
and available cloud providers. Offline tests cover missing models, local
priority with cloud available, author exclusion, cloud fallback, missing
business events and interruption. The preceding failed/interrupted recipes
remain in local persisted state; they are not counted as acceptance PASS.

BMAX's operator override disables the two Mistral workers after both stalled
on noninteractive approval requests in standard mode. Other cloud workers
remain configured; removing that user override restores packaged defaults.
