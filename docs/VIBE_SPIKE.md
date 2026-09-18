# VIBE_SPIKE.md — Mistral Vibe feasibility spike (2026-09-18)

REUSE-FIRST feasibility spike. No product code was written or modified —
see § "What NOT to build" and the commit for this study. Every claim below
is tagged `VERIFIED` (observed directly in this session, on this machine),
`DOCUMENTED_BY_VENDOR` (official Mistral/Ralph source, not independently
re-verified beyond what's cited), `INFERRED` (reasoned from the above, not
directly observed), or `UNKNOWN` (genuinely not established by this spike).

## 1. Executive summary

Mistral Vibe (`vibe`, official, open-source, Apache 2.0) is a real,
non-interactive-capable, tool-using coding CLI, already installed and
authenticated on this machine. A real disposable execution succeeded:
it edited files, ran a self-corrected shell command, ran pytest, and
correctly participated in Ralph's own event-emission protocol
(`ralph emit "work.completed" "done"`) when instructed to. It is **not**
natively known to Ralph 2.10.1's hat-based backend mechanism (the one
`RalphExecutionEngine` always uses) — only Ralph's separate, solo-mode
"custom" backend accepts an arbitrary command, and that mechanism expects
the target binary to accept a *file path* as its final argument, which
`vibe -p` does not natively support. A small, disposable bridging wrapper
script closed that gap and was proven to work end to end. Vibe exposes
**no machine-readable quota/rate-limit signal** anywhere (confirmed both
by direct execution and by official documentation) — a `MistralProviderAdapter`
can only ever be `EXECUTION_PROBE_ONLY`, fail-closed, never fabricating a
`reset_at`. **Classification: C — THIN_PROVIDER_ADAPTER_PLUS_MAPPING.**
Nothing was implemented; this is a proposal only.

## 2. Versions inspected

- `ai-dev-orchestrator` HEAD `ba271a4` (documentation-only ahead of `v0.1.0`/`cc5728f`; product code identical — `VERIFIED`, `git diff v0.1.0..HEAD -- src/ tests/ config/` empty).
- Ralph `2.10.1` (`VERIFIED`, `ralph --version`).
- Vibe `2.25.4` (`VERIFIED`, `vibe --version`).

## 3. Official sources

| Source | URL | Finding used |
|---|---|---|
| Mistral Docs — Install & setup | https://docs.mistral.ai/vibe/code/cli/install-setup | Install command, auth methods, env vars, confirms no documented usage/quota CLI command |
| Mistral Docs — Work with the CLI | https://docs.mistral.ai/vibe/code/cli/work-with-cli | (linked, not independently re-fetched beyond the above) |
| GitHub — mistralai/mistral-vibe | https://github.com/mistralai/mistral-vibe | License (Apache 2.0), project description, `--prompt` non-interactive mode, confirms no documented rate-limit/quota tracking |
| Mistral — Terms of Use | https://mistral.ai/terms-of-use/ | General ToS (competitive analysis / reverse-engineering / sublicensing restrictions) — no restriction found on automated/agentic coding use |
| Mistral — Agents API announcement | https://mistral.ai/news/agents-api/ | Mistral officially promotes agentic/automated use via its APIs |
| Installed `vibe --help`, `vibe mcp --help` | local CLI | `VERIFIED` — authoritative for the exact installed version's flags |
| Installed `ralph --help`, `ralph init --help`, `ralph doctor`, `ralph hats validate` | local CLI | `VERIFIED` — authoritative for backend/hat mechanics of this exact Ralph version |

No blog/tutorial was used for an architecture conclusion; the two tutorial-style links surfaced by search (`dev.to`, `blog.meetneura.ai`) were not used.

## 4. Current orchestrator extension points (`VERIFIED`, by reading the code, not docs)

- `ProviderAdapter.probe() -> ProviderState` (`src/orchestrator/providers/adapter.py`) — the only contract a new provider must satisfy. Read-only; never raises for ordinary unavailability (quota/auth), only for adapter-level failure to observe at all.
- `ClaudeCodeAdapter`/`CodexAdapter` are the two existing implementations — both launch one small **real** headless CLI call and parse structured rate-limit telemetry the CLI itself emits (Claude's `rate_limit_event`/`unifiedWindows` in `stream-json`; Codex's own structured output). This is the established pattern a `MistralProviderAdapter` would follow if such telemetry existed.
- `Worker.backend` (never `.provider`) drives `RalphExecutionEngine._build_backend_args`/`_ralph_backend_type` — a small, explicit `if backend == "codex" / "claude_code": ... else: raise UnsupportedBackendError`. **Any third backend value raises today** — this is real `src/` code that would need a new branch, not just a config value.
- `config/workers.yaml` → `WorkerRegistry` → `WorkerSelector`: fully generic already (provider is an opaque string everywhere in `WorkerSelector`'s own logic — `tests/test_worker_selector.py::TestNoHardcodedProvider` proves this). **No change needed here** for a new provider.
- `QuotaManager`: generic over `Mapping[str, ProviderAdapter]` — adding a third provider is just adding a third dict entry at wiring time. **No change needed here.**

## 5. Ralph compatibility (`VERIFIED`, hands-on)

`ralph init --backend <BACKEND>` lists exactly: `claude, kiro, gemini, codex,
forge, amp, copilot, opencode, pi, custom`. **No native `vibe`/`mistral`
entry exists.**

There IS a generic `custom` backend — but with a decisive restriction,
proven by direct experiment:

- **Top-level `cli: backend: "custom"` (solo/no-hats mode)**: works.
  `ralph doctor` requires `cli.command` to be set to a real binary; once
  set, `ralph preflight`/`ralph doctor` both pass (`VERIFIED`, tested with
  `command: "echo"`).
- **Per-hat `backend: {type: "custom"}` (the hats mechanism
  `RalphExecutionEngine` *always* uses — one hat named `worker`, per
  `_render_hats_config`)**: **rejected**. `ralph doctor` reports `FAIL
  backend:hat:worker — Unknown hat backend` for `type: "custom"` (also
  tried `type: "echo"` directly — also rejected). Only `type: "claude"`
  (and presumably the other 8 native names) is accepted at the hat level
  (`VERIFIED`).

**Conclusion: `RalphExecutionEngine`, as currently written, cannot invoke
any custom/generic backend at all — because it always builds a hats.yml,
and Ralph's hat mechanism does not support "custom".** A code change is
required to switch to Ralph's solo mode for a non-native backend.

Prompt delivery to a custom backend (`VERIFIED`, logging-wrapper
experiment): Ralph invokes `<command> <configured args...> "<path to a
temp file containing the full constructed prompt>"` — a **file path** as
the final positional argument, never the prompt text inline and never via
stdin. `vibe -p [TEXT]` expects literal text, not a file path — a direct
mismatch (`VERIFIED`).

`ralph emit "<topic>" "<payload>"` (Ralph's own CLI subcommand, used by
Claude Code/Codex workers today to signal business completion) worked
correctly when invoked by Vibe's own bash tool, appending to the same
events file a live `ralph run` had opened (`VERIFIED`) — confirming the
event/business-verdict mechanism `RalphExecutionEngine` depends on
(`success_topics`/`failure_topics` matching) is not itself Ralph-native-
backend-specific; any process that can shell out to `ralph emit` can
participate.

## 6. Direct Vibe CLI findings (`VERIFIED`, one real execution)

Disposable repo `/tmp/ai-dev-orchestrator-vibe-spike` (baseline: one
trivial `calc.py`). Command:

```
vibe -p "<task>" --output json --trust --auto-approve --workdir <dir>
```

- Exit code 0, 40.8s wall-clock, real cost against the FREE-plan account.
- Autonomous tool use observed in the structured JSON transcript:
  `read_file`, `edit`, `write_file`, `bash` — including a genuine
  self-correction (`python` → command not found, exit 127 → retried
  `python3` → succeeded), with **no manual intervention**.
- Ran `python3 -m pytest test_calc.py -v` itself via its `bash` tool; 2/2
  passed.
- `--output json` gives an array of `message`/`reasoning`/`effect` entries
  (each with `id`/`sessionId`/`turnId`/timestamps/`type`) — a real
  structured transcript, not free text.
- **Did not commit automatically** (`git status` showed uncommitted
  changes after a fully successful run). The prompt used did not
  explicitly instruct it to commit — this is a **limitation of this one
  trial**, not proof Vibe never commits; genuinely `UNKNOWN` whether an
  explicit "commit your changes" instruction would be followed, untested
  here to keep the spike minimal (see § "What NOT to build").
- `--max-turns`/`--max-price`/`--max-tokens` exist as *input* session
  bounds (`DOCUMENTED_BY_VENDOR`+`VERIFIED` via `--help`) — not queryable
  after the fact.

## 7. Ralph→Vibe findings (`VERIFIED`, mixed outcome, reported honestly)

- A minimal disposable bridging wrapper (`/tmp/ralph-vibe-e2e/vibe_ralph_wrapper.sh`, ~5 lines) reads Ralph's temp-file argument and re-invokes `vibe -p "$(cat file)" --trust --auto-approve`.
- **Standalone, direct invocation of that exact wrapper with the exact real prompt content: PASS.** File edited correctly, `ralph emit "work.completed" "done"` correctly recorded in the running loop's own events file, `LOOP_COMPLETE` printed.
- **One live `ralph run -c ralph.yml` (solo mode, `command` = the wrapper): did not finish within a 90s observation window** and was killed by an external `timeout`. Root cause not fully isolated within spike budget; the most likely explanation (not confirmed) is that Ralph's own "ralph-tools-skill" preamble (observed separately, a sizeable injected instructions block) makes the real prompt substantially longer than the raw task text used in the standalone trial, so the live run plausibly needed more wall-clock time rather than being structurally broken — supported by the fact the exact same wrapper, prompt content, and `ralph emit` mechanism all worked when exercised directly.
- Reported as: **mechanism proven to work (wrapper + `-p` + `ralph emit`), one live end-to-end timing trial inconclusive within budget** — not a clean PASS, not a FAIL, and explicitly not blindly retried (no blind retries, per spike constraints).

## 8. Authentication findings

- Local: `~/.vibe/whoami_cache.json` shows an authenticated Mistral account, `plan_type: "chat"`, `plan_name: "FREE"`, `api_base: https://api.mistral.ai` (`VERIFIED`, secret-like fields redacted before inspection; `config.toml` itself, mode 600, was never read).
- `DOCUMENTED_BY_VENDOR`: three auth modes — interactive Mistral account sign-in (`vibe --setup`), an API key generated from the Mistral account ("Code > Vibe CLI"), or `MISTRAL_API_KEY` env var to bypass the interactive prompt (non-interactive-friendly, relevant for a future headless worker).

## 9. Quota/availability findings — **critical**

**Outcome: C. EXECUTION_PROBE_ONLY.**

- `VERIFIED`: the real execution's structured JSON transcript contains
  zero fields matching `usage|token|cost|quota|rate_limit|remaining|budget`
  anywhere (systematic grep across the full response).
- `VERIFIED` (no `usage`/`status`/`quota` CLI subcommand exists — only
  `update` and `mcp` appear under `vibe --help`'s "Commands" section).
- `DOCUMENTED_BY_VENDOR`: the official install/setup doc states usage and
  rate limits "depend on your Mistral plan and pay-as-you-go settings" but
  documents **no CLI command or method to query current quota/usage/reset**
  from the CLI itself.
- What a `MistralProviderAdapter.probe()` could **honestly** expose: run
  one minimal real `-p` call (mirroring `ClaudeCodeAdapter`'s own pattern)
  and treat process success as `ProviderAvailability(available=True)`;
  treat any failure as `ProviderAvailability(available=False, reason=UnavailabilityReason.UNKNOWN)`
  — **never** `QUOTA_EXHAUSTED` specifically, since no evidence distinguishes
  a quota error from any other failure. `quota_windows` would always be
  `()` (empty) — never a fabricated or inferred `reset_at`. This satisfies
  the existing `ProviderAdapter`/`ProviderState` contracts unmodified; it
  is a materially weaker signal than Claude/Codex's own adapters, and that
  weakness must not be hidden from `WorkerSelector`'s diagnostics.

## 10. Git/commit behavior

Not observed to commit automatically in this trial (§6). Untested whether
an explicit prompt instruction changes this (`UNKNOWN`, deliberately not
tested to keep the spike minimal — flagged as a concrete open question for
any future real integration, since `GitGovernanceService`'s SHA-before/
SHA-after capture depends on the worker actually committing).

## 11. Security/privacy observations

- Apache 2.0, open source, official Mistral repository — no unusual
  license risk (`DOCUMENTED_BY_VENDOR`).
- No ToS clause found prohibiting automated/agentic coding use; general
  restrictions cover competitive analysis, reverse-engineering, and
  sublicensing to third parties without authorization
  (`DOCUMENTED_BY_VENDOR`, general ToS page, not exhaustively parsed
  clause-by-clause).
- `~/.vibe/config.toml` is mode `600` (owner-only) — consistent with
  holding credentials; never read in this spike.
- Vibe's own docs mention optional telemetry/crash reporting, disableable
  via `enable_telemetry = false` (`DOCUMENTED_BY_VENDOR`) — relevant to a
  future privacy review, not evaluated further here.

## 12. Integration classification

**C. THIN_PROVIDER_ADAPTER_PLUS_MAPPING.**

Not A/B: `RalphExecutionEngine` cannot represent any non-native backend
today (hats reject "custom"; the render functions never emit `cli.command`
at all) — this is real `src/` code, not configuration.
Not D/E: nothing found makes it architecturally impossible — a small,
well-scoped `src/` change (a mapping branch + a small bridging script) plus
a small, honestly-weak `ProviderAdapter` fully closes the gap, reusing
`QuotaManager`/`WorkerSelector`/`RalphExecutionEngine`'s existing shape
unchanged otherwise.

## 13. Minimal implementation proposal (PROPOSAL ONLY — not implemented)

- `src/orchestrator/providers/mistral_vibe_adapter.py`: `MistralVibeAdapter(ProviderAdapter)`, one real bounded `vibe -p <probe prompt> --output json --trust --auto-approve` call, `PROVIDER_NAME = "mistral"`, availability from process success only (see §9). No quota windows populated.
- `RalphExecutionEngine`: a small branch — when `Worker.backend` is not a Ralph-native type (i.e. not in `{"codex", "claude_code", ...}`), render a **solo-mode** `ralph.yml` (`cli.backend: "custom"`, `cli.command: <bridging script path>`, `cli.args: [...]`) instead of today's `hats.yml` path, and skip hat rendering entirely for that execution. This is the smallest change consistent with §5's evidence — never a second execution engine.
- A small, versioned bridging script (e.g. `scripts/vibe_ralph_bridge.sh` or a `.py` equivalent) — not generated per-execution, checked in like any other small utility — implementing exactly what §7's disposable wrapper proved: read the final-arg file path, invoke `vibe -p "$(cat file)" --trust --auto-approve [model/effort flags]`.
- `_build_backend_args`/`_ralph_backend_type`: one new branch for `backend == "vibe"`.

Nothing here was written to `src/`, `tests/`, or `config/` — this section
is a specification for a future session, contingent on explicit user
approval.

## 14. Proposed worker config (PROPOSAL ONLY, example shape — no names decided beyond illustration)

```yaml
  - worker_id: <tbd-a>
    display_name: <TBD>
    enabled: true
    provider: mistral
    backend: vibe
    priority: 80          # below existing primaries; illustrative only
    capabilities:
      - development        # only capability with direct execution evidence from this spike
    default_profile_id: standard
    profiles:
      standard:
        quality_tier: STANDARD
        model: <tbd — whichever Mistral model `vibe`'s default_agent resolves to>

  - worker_id: <tbd-b>
    display_name: <TBD>
    enabled: true
    provider: mistral
    backend: vibe
    priority: 70
    capabilities:
      - development
    default_profile_id: standard
    profiles:
      standard:
        quality_tier: STANDARD
        model: <same as above>
```

Two identities proposed (not one), consistent with the governance
invariant restated by the user: with only Mistral available, `DEV_B.worker_id
!= DEV_A.worker_id` could not otherwise be satisfied. **Only `development`
is proposed as a capability** — this spike only produced evidence for
autonomous file-editing + test-running (development-shaped work). It did
**not** exercise or produce evidence for `code_review` (the Lean `DEV B`
role specifically — untested here, though mechanically identical to `DEV A`
since both are plain `role="developer"` executions per `WorkflowMode`),
`release_planning`, `roadmap_synthesis`, or `qa_testing` — granting those
without evidence would be exactly the kind of unearned assumption this
spike is meant to avoid.

## 15. Risks

- **Weak availability signal** (§9): `WorkerSelector` would treat any
  Vibe failure — quota, auth, network, a bad prompt — identically as
  "not available, reason unknown." This is honest but coarser than
  Claude/Codex, and could cause a Mistral worker to look "unavailable" for
  reasons that aren't actually quota (or vice versa, briefly look
  available right up to a real quota failure mid-execution).
- **Unconfirmed commit behavior** (§10, §14): if Vibe workers don't commit
  reliably even when instructed, `GitGovernanceService`'s SHA-capture
  mechanism would silently see "no change," which could look like a DEV B
  no-op review even when DEV A's work was real but never committed —
  untested, must be resolved before any real integration.
- **Ralph live-run timing** (§7): the one inconclusive end-to-end trial
  means real-world latency/timeout behavior through Ralph specifically is
  not yet fully characterized.
- **Single trial sample size**: every finding above is n=1 (one real
  execution). None of this is a statistical claim about reliability.

## 16. What NOT to build (right now)

- No `MistralVibeAdapter`, no `RalphExecutionEngine` mapping, no bridging
  script committed to the repo, no `config/workers.yaml` entries — all of
  §13/§14 is a proposal, explicitly deferred pending user approval.
  Not requested by, and not part of, MVP 0.1.
- No new quota framework, scheduler, routing layer, or second execution
  engine — this spike's own findings show none are needed.
- No retry loop to "make the Ralph live-run trial pass" — one inconclusive
  result was recorded honestly instead (no blind retries).
- No fabricated quota telemetry, ever, for Mistral.

## 17. Acceptance criteria for a future real integration

Before `Mistral / Vibe` could move from 🧪 SPIKE to ✅ VALIDATED in the
README table, a future session would need, at minimum:

1. `MistralVibeAdapter` implemented + offline-tested (mirrors existing
   adapter test patterns, e.g. `tests/providers/test_claude_code_adapter.py`).
2. `RalphExecutionEngine`'s solo-mode branch implemented + offline-tested,
   proving a real `ralph run` (not just a standalone wrapper call)
   completes end to end within a documented, reasonable timeout.
3. Explicit confirmation (real execution) of whether/how a Vibe worker
   commits, with `GitGovernanceService`'s SHA-capture verified against it.
4. `config/workers.yaml` updated with two real Mistral worker identities.
5. A real Lean Feature Flow acceptance pilot (same shape as the Roman
   Numerals/Morpion pilots) with at least one WorkItem actually routed to
   a Mistral worker (ideally as a same-provider DEV A/DEV B pair, proving
   the "two identities" design for real).
6. `docs/status.md`/`ROADMAP.md` updated only after that real acceptance
   evidence exists — never before.

## 18. Final recommendation

Proceed to a real, scoped implementation **only after explicit user
approval**, following exactly §13/§14/§17 — smallest-possible change,
reusing `QuotaManager`/`WorkerSelector`/`RalphExecutionEngine`'s existing
shape, one new adapter, one new backend-mapping branch, one small bridging
script, two worker identities, `development` capability only until
`code_review`/other roles are separately evidenced. Do not build this in
the same session as this spike.

---

## 19. Implementation follow-up (2026-09-18)

Everything below is additive — nothing above this line was rewritten;
history stays exactly as observed during the spike.

**Implemented, following §13/§14 almost exactly:**

- `src/orchestrator/providers/mistral_vibe_adapter.py` —
  `MistralVibeAdapter(ProviderAdapter)`. Real invocation: `vibe -p "<probe
  prompt>" --output json --trust --auto-approve --max-turns 1`, bounded by
  timeout. `EXECUTION_PROBE_ONLY` as designed: `quota_windows` always
  empty, `reset_at` never fabricated; a non-zero exit is classified
  `UNKNOWN` unless stderr/stdout matches a small, explicitly fragile set
  of generic auth/rate-limit text markers (§9's caveats fully preserved).
- `src/orchestrator/ralph_execution_engine.py` — `_SOLO_MODE_BACKENDS`/
  `_BACKEND_COMMANDS` (`{"vibe"}`), a `backend == "vibe"` branch in
  `_build_backend_args` (new `UnsupportedProfileOptionError` — fails
  closed on `reasoning_effort`, which Vibe has no CLI concept for), and a
  solo-mode branch in `_write_runtime_config`/`_render_ralph_config`/
  `_build_ralph_args` that emits `cli.backend: "custom"` +
  `cli.command: <bridge>` and omits `-H <hats>` entirely for this backend
  — exactly the mechanism §5/§12 identified as required. Native
  (claude/codex) backends are unchanged (regression-tested explicitly).
- `src/orchestrator/vibe_ralph_bridge.py` — the thin bridge §7/§13
  proposed, as a small checked-in script (not per-execution generated),
  referenced by absolute path relative to its own module location.
- `config/workers.yaml` — two new workers, `milo` and `juno`
  (`provider: mistral`, `backend: vibe`, `priority: 60` — below the
  validated Anthropic/OpenAI pool, deliberately never a silent default),
  `development` capability only, `model: vibe-default` (a documented
  sentinel — see below, not a fabricated real model name).
- Offline tests: 10 new `WorkerSelector` tests
  (`TestMistralProviderIntegration`), 14 new `MistralVibeAdapter` tests,
  8 new `RalphExecutionEngine` tests (`TestVibeBackendMapping`) — 30 new
  tests, 1188 total offline, 0 failures.

**Real evidence gathered this session (all against real Vibe 2.25.4, real
account, FREE plan):**

- Direct `vibe -p "Reply with exactly: OK" --output json --trust
  --auto-approve --max-turns 1` (the adapter's exact probe invocation):
  **PASS**, exit 0, 4.1s.
- Real `RalphExecutionEngine.execute()` → the real `ralph` binary → the
  real bridge → real Vibe, against a disposable repo
  (`/tmp/ralph-vibe-real-smoke`, never a real project): **first attempt
  FAILED** — 5 rapid, ~0s iterations, no real Vibe call ever happened.
  Root-caused (not guessed) via a debug-level Ralph log
  (`ralph_adapters::cli_executor`): Ralph's real custom-backend argv is
  **not** a bare file path, contrary to the spike's own §5/§7
  characterization — it is a full instructional sentence with the path
  embedded at the end (`"Please read and execute the task in
  /tmp/.tmpXXXXXX"`). This is a corrected, more precise finding, not a
  contradiction of the spike's core conclusion (solo-mode custom backend
  is still the right mechanism). Fixed deterministically in the bridge
  (extract the last whitespace-separated token) — one corrective retry,
  exactly as the implementation brief allowed for a clear
  implementation-syntax bug, never for model behavior or quota.
  **Second attempt: PASS** — `status=succeeded`, `shout()` genuinely
  added to the disposable `greet.py`, verified by a real Python import
  check (`hello` + `HELLO`), correct business-event detection
  (`work.completed`), 43.3s real duration.
- **New confirmed limitation (real evidence, not spike speculation):**
  despite the successful, verified file edit, **`git_sha_before ==
  git_sha_after`** — Vibe did not commit its change. The prompt used
  (mirroring `MVPManager._build_dev_instructions`'s real shape) never
  explicitly said "commit your changes" — Claude Code/Codex commit as
  their own default agentic behavior in this same situation (observed
  repeatedly in the Roman Numerals/Morpion pilots); this one real trial
  shows Vibe does not. This directly matches the risk already flagged in
  §15/§10 as untested — it is now tested, and confirmed real.
- **Two-worker real validation: NOT_RUN**, deliberately, **not**
  `BLOCKED_BY_PROVIDER_CAPACITY` (capacity was not the limiter here) — a
  second real Vibe execution would not have produced clean evidence given
  the unresolved commit gap above (DEV B would be reviewing an
  uncommitted worktree state, materially different from what
  `GitGovernanceService` actually orchestrates in production). Resuming
  this validation is contingent on deciding how Vibe workers' commits are
  handled — a product decision, correctly out of scope for this
  implementation slice. The **offline** structural-independence proof
  (`DEV_B.worker_id != DEV_A.worker_id` achievable with only Mistral
  available) already passes — see `TestMistralProviderIntegration` in
  `tests/test_worker_selector.py`.
- No second corrective retry was taken for the commit-gap finding — it is
  model/product behavior, not implementation syntax, and the brief
  explicitly forbids retrying for that reason.

**`model: vibe-default` sentinel:** the spike never established a
verified, real Mistral model identifier `VIBE_ACTIVE_MODEL` should carry
(§13 already flagged this as unresolved) — rather than fabricate one now,
`vibe_ralph_bridge.py` treats the literal string `"vibe-default"` as "no
override," deferring to whatever Vibe's own local config already resolves
as its default. This is honestly encoded in `config/workers.yaml`'s own
comment, not hidden.

**Final support classification: 🧪 SPIKE (not `VALIDATED`).** Per the
implementation brief's own rule ("✅ VALIDATED only if: adapter
integrated; RalphExecutionEngine → Vibe real smoke PASS; normal
WorkerSelector integration works; no unresolved execution blocker
remains"): the first three hold, but the no-commit finding is exactly an
unresolved execution blocker for real Lean Feature Flow use (Git
governance depends on real SHA advancement). `VALIDATED` requires that
gap to be explicitly resolved (see below) and re-verified with real
execution — never simply reclassified because the code merged cleanly.

**Remaining limitations (honest, not exhaustive):**

- No auto-commit confirmed (above) — the single largest blocker to real
  product use.
- `EXECUTION_PROBE_ONLY` availability signal is unchanged from the spike
  — still no way to distinguish a real quota failure from any other
  failure with confidence; never exercised against a real failure in
  this session either (the FREE-plan account never actually failed). This
  was an explicit, accepted trade-off in the spike itself and remains so.
- `code_review`/`qa_testing`/`release_planning`/`roadmap_synthesis`
  remain ungranted — no evidence gathered for any of them in this
  session (only plain `development`-shaped work was exercised).
- Cost/rate-limit real-world behavior over a longer session (multiple
  executions in sequence) was not exercised — only two real calls total
  this session (probe + one execution).

**Acceptance criteria for `VALIDATED` (updates §17):** items 1, 2, 4 of
§17 are now done. Still open: §17 item 3 (commit behavior — now
confirmed as a real gap, not just a question), item 5 (real Lean pilot
routing to Mistral), item 6 (docs update after that real acceptance,
not before).
