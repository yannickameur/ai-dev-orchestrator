# OmniRoute Opportunity Report — Claude (independent audit)

Date: 2026-09-13
Author: Claude (this session), independent of any Codex analysis — written and
finalized before reading any Codex report content (see §35 and the process
note at the end of this document).

Scope note: this document evaluates whether `ai-dev-orchestrator` and/or
Ralph Orchestrator should continue to exist given OmniRoute
(https://github.com/diegosouzapw/OmniRoute). Per explicit instruction, no
prior investment in `ai-dev-orchestrator` is treated as a reason to keep it.
Sunk cost is not evidence.

---

## 1. Executive summary

OmniRoute is a mature, professionally engineered **AI model gateway/router**
(TypeScript/Next.js, MIT licence, npm package `omniroute@3.8.51`). It solves
"send this chat-completion request through the cheapest/most-available of
356 providers, with quota-aware fallback and up to ~89% token compression."
It does **not** solve, and does not attempt to solve, the problem
`ai-dev-orchestrator` exists for: driving a multi-step software-delivery
lifecycle (WorkItem → adaptive worker/profile selection → TDD execution via
Ralph → quality gate → independent review → durable cross-worker/cold-restart
handoff → release → realization/activity reporting → roadmap
synthesis/approval) with governance guarantees (no silent quality downgrade,
author≠reviewer, fail-closed everywhere, full audit trail).

OmniRoute operates **one layer below** where our orchestrator operates: it is
a thing Claude Code/Codex CLIs (and other tools) can be pointed at as their
*model backend* (`ANTHROPIC_BASE_URL`-style), or a thing that can launch those
CLIs (`omniroute run claude`) as a convenience wrapper. It has no concept of a
WorkItem, MVP, quality gate, independent-review policy, or durable
crash-resumable handoff. Its "cloud agent" integration (Jules/Devin/Codex
Cloud/Cursor Cloud) and its "issue agent" are thin, single-shot task
delegations to *external* autonomous agents — not a replacement for our
WorkItem state machine.

Where OmniRoute is genuinely stronger than anything in our codebase or in
Ralph is: (a) the sheer breadth of provider/model coverage and free-tier
aggregation, (b) a much more mature *token-level* quota/routing engine
(fair-share, burn-rate, subscription-vs-metered billing classification,
reset-aware ladders), (c) request-level prompt compression, and (d) test/CI
maturity (mutation testing, nightly resilience/security suites, CodeQL,
Semgrep).

**Primary conclusion: KEEP_CURRENT_ARCHITECTURE**, with one optional,
narrow addition worth a future spike (not this session): using OmniRoute as
an optional model-routing/cost-optimization backend *underneath* Codex/Claude
Code CLI calls, never as a replacement for WorkerSelector, RalphExecutionEngine,
QuotaManager (subscription-window sense), or any of the WorkItem-lifecycle
components. See §35 for the full decision block, including
`SECOND_BEST_OPTION` and `WHAT_WOULD_CHANGE_MY_MIND`.

This is not a close call. OmniRoute and `ai-dev-orchestrator` answer
different questions ("which model serves this HTTP request cheapest" vs.
"has this WorkItem been correctly built, tested, independently reviewed, and
durably handed off"). Neither Ralph nor our orchestrator is threatened by
OmniRoute's existence; if anything, OmniRoute is a candidate *provider
underneath* our stack, never a candidate *replacement* of any orchestration
layer.

---

## 2. OmniRoute version studied

- Repository: `https://github.com/diegosouzapw/OmniRoute`
- Clone method: `git clone --depth 50` into a disposable `/tmp` directory
  (never inside this repo; removed after use).
- Commit studied: `152d95108c9c3d557562311ffed63240a511eb31`
  (2026-09-11 22:29:29 -0300).
- Branch/tag: `release/v3.8.51` (repository's only branch/HEAD).
- `package.json` version: `3.8.51`.
- npm package name: `omniroute`.
- Distribution: npm (`npm i -g omniroute`), Docker Hub image, Electron
  desktop builds, source (pnpm workspace).

## 3. Licence

- `LICENSE` file: **MIT**, copyright (c) 2026 diegosouzapw.
- A `THIRD_PARTY_NOTICES.md` file exists (17KB) — indicates the project
  itself tracks third-party licence obligations for its own dependencies,
  a positive maintainability signal.
- MIT is permissive: dependency use, code copy, modification, and vendoring
  are all `ALLOWED` under the licence text itself (attribution notice must be
  preserved). See §24-ish decision matrix below (folded into build-vs-adopt,
  §27) for the per-use-mode classification.
- No dual-licensing, no CLA-only restriction, no field-of-use restriction
  found in `LICENSE`, `CONTRIBUTING.md`, or `CODE_OF_CONDUCT.md`.

## 4. Current architecture (ai-dev-orchestrator)

```
Project (workspace, roadmap, current MVP)
  -> MVPManager (WorkItems, dependencies, durable handoff)
  -> WorkerSelector (capability > governance > quota > cost; adaptive tier since Slice 17)
  -> RalphExecutionEngine (real `ralph` subprocess: hats, TDD, review)
  -> ExecutionRecord + HandoffRecord (audit + durable resume)
  -> ValidationStore / QualityGateRunner (real commands, fail-closed)
  -> ReviewStore (independent review, author != reviewer enforced)
  -> ReleaseManager -> ActivityReport (MVP/release-level)
  -> PlanningCoordinator -> RoadmapProposal -> ApprovalCoordinator -> RoadmapApplicationService
  -> RealizationReport (WorkItem-level, deterministic, non-LLM aggregation)
```

Underneath `WorkerSelector`: `ProviderAdapter.probe()` (ClaudeCodeAdapter,
CodexAdapter — read-only CLI-quota probes) → `QuotaManager` (freshness/TTL
cache over multi-window `ProviderState`, e.g. Claude's 5h/7d windows). This
is a **CLI-subscription quota model** (a human is logged into `claude`/`codex`
CLIs outside this project; the orchestrator never holds API keys), not an
API-key/token-billing model.

Governance invariants enforced today, all with tests (766 passing at last
count): fail-closed everywhere (no default PASS), no silent quality-tier
downgrade on fresh selection *or* on wait/recovery resume
(`MVPManager._select_dev_worker` shared across all three call sites),
insert-only audit persistence (nothing is ever overwritten, only appended),
atomic file writes (`tempfile.mkstemp` + `os.replace`), structural
author≠reviewer enforcement in `WorkerSelector` (not just configurability).

12,069 lines of Python across `src/orchestrator/`, 25 modules, ~29 test
files. Real, versioned proof of a genuine cross-worker (Claude Code →
Codex), cold-restart-safe handoff exists:
`docs/reports/real-cross-worker-resume-2026-09-13.html`.

## 5. OmniRoute architecture

```
Any OpenAI/Anthropic-compatible client (Claude Code, Codex, Cursor, Cline,
  raw HTTP, LangChain/CrewAI/AutoGen via A2A, MCP clients)
    -> POST /v1/chat/completions (or /v1/responses, or A2A JSON-RPC, or MCP tool call)
    -> OmniRoute Smart Router
         - capability/combo selection (which of 356 providers/1312+ model ids can serve this)
         - RTK + "Caveman" prompt compression (documented 15-95%, ~89% avg — see §23)
         - 19 routing strategies (auto/cheapest, auto/quality, auto/subscription,
           auto/thrifty, per-category variants, ...)
         - allocation (Ghostlight internal budgets) -> quota telemetry
           (healthy/approaching_limit/exhausted/unavailable/unknown)
           -> circuit breaker (closed/open/half_open) -> provider dispatch
    -> one of 356 upstream providers (OpenAI, Anthropic, Google, Groq, many
       free-tier/keyless providers, "web" providers via authenticated
       browser-cookie sessions e.g. claude-web, chatgpt-web, ...)
```

Separately, and architecturally distinct from the router: `omniroute run
<cli>` launches a supported coding CLI (Claude Code, Codex, Aider, Goose,
OpenCode, Qwen Code, Gemini CLI) as a child process with its model backend
pointed at OmniRoute — a convenience launcher plus model-routing shim, not a
task/workflow orchestrator. `cloudAgent/` is a thin registry+adapter
(`jules`, `devin`, `codex-cloud`, `cursor-cloud`) that submits a single
`CloudAgentTask {status: queued|running|awaiting_approval|completed|failed|
cancelled}` to an external hosted agent and polls it — no WorkItem
dependency graph, no quality gate, no independent-review policy, no
multi-worker handoff. `issueAgent/` triages a GitHub issue via one
chat-completion call (classification), not a build/test/review loop.

Storage: SQLite (`sqliteQuotaStore.ts`, `sqliteBackend.ts` for memory) and
optional Redis (`redisQuotaStore.ts`). Auth/authz layer, CORS, WS support.
Node/TypeScript, Next.js app router, ~82 top-level `src/lib/*` modules,
5000+ test files (`tests/unit`, `tests/integration`, `tests/e2e`, nightly
mutation/resilience/security suites in CI).

## 6. Ralph architecture (recap, for the three-way comparison)

Ralph Orchestrator (`ralph` CLI, Rust, MIT, ~158k LOC, studied in
`docs/ECOSYSTEM.md` and `docs/SPIKE_RALPH.md`) is the actual execution
engine our `RalphExecutionEngine` drives via subprocess: hats/roles with
per-hat backend/model config, an event bus (`ralph emit`,
`.ralph/current-events` JSONL), completion promises (`LOOP_COMPLETE`), git
auto-commit, worktrees + parallel loops/waves, a review preset
(`presets/review.yml`). It has no QuotaManager for CLI subscription windows,
no WorkerSelector with cost/governance ranking, and no structural (only
configurable) author≠reviewer guarantee — this is exactly the gap
`ai-dev-orchestrator` was built to close (see `docs/ECOSYSTEM.md` §1).
OmniRoute does not change this picture: it has no hats/event-bus/TDD-loop
concept at all — it is not a Ralph competitor, it is a different kind of
system entirely (request router, not an execution-loop engine).

## 7. Capability matrix

| Capability | ai-dev-orchestrator | Ralph | OmniRoute |
|---|---|---|---|
| Multi-step WorkItem lifecycle (dependencies, statuses, MVP grouping) | YES (CODE_CONFIRMED, `project_state.py`) | NO | NO (NOT_FOUND) |
| TDD/hats execution loop | Delegates to Ralph | YES | NO |
| Worker/Profile selection by capability+governance+quota+cost+quality-tier | YES (`worker_selector.py`, `adaptive_execution.py`) | Partial (static backend-per-hat config only) | NO (per-request model routing only, not "worker" identity) |
| Structural author≠reviewer guarantee | YES (CODE_CONFIRMED) | Configurable only, no guarantee | N/A (no review concept) |
| CLI-subscription multi-window quota (Claude Code Max/Pro, Codex/ChatGPT Plus) | YES (`quota_manager.py`, per-provider adapters) | NO | PARTIAL — models a generic "subscription" billing class (`connectionBillingCatalog.ts`) for OAuth *connections* used as chat-completion backends, not for governing which CLI *worker* runs a WorkItem (DOC_CONFIRMED + CODE_CONFIRMED for the routing use case; NOT_FOUND for WorkItem-level worker governance) |
| Durable, cold-restart, cross-process handoff | YES (real smoke-tested, CODE_CONFIRMED + TEST_CONFIRMED) | Partial (`handoff.rs`, single-process) | NOT_FOUND (memory/session context only, see §21) |
| Independent code review (quality verdict, findings) | YES (`review.py`) | Preset exists (`presets/review.yml`), not policy-enforced | NOT_FOUND |
| Quality gate (real command execution, fail-closed) | YES (`validation.py`) | Quality gates exist (tests/lint/coverage) | NOT_FOUND (no software build-verification concept) |
| Release/activity reporting | YES (`activity_report.py`, `realization_report.py`) | NOT_FOUND | NOT_FOUND |
| Roadmap synthesis + optimistic approval | YES (`planning.py`, `approval.py`) | NOT_FOUND | NOT_FOUND |
| Multi-provider model routing (cost/latency/free-tier) | NOT_FOUND (out of scope by design) | NOT_FOUND | YES, extensively (CODE_CONFIRMED, TEST_CONFIRMED) |
| Prompt/token compression | NOT_FOUND | NOT_FOUND | YES (DOC_CONFIRMED, partially TEST_CONFIRMED — see §23) |
| Free-tier aggregation/catalog | NOT_FOUND | NOT_FOUND | YES (CODE_CONFIRMED, `docs/reference/FREE_TIERS.md`) |
| MCP server | NOT_FOUND | NOT_FOUND (Ralph itself is not an MCP server; separate ecosystem) | YES (CODE_CONFIRMED, `bin/mcp-server.mjs`) |
| A2A server (expose self as an agent) | NOT_FOUND | NOT_FOUND | YES (CODE_CONFIRMED, `src/lib/a2a/`) — exposes "smart-routing"/"quota-management" skills, opposite direction from what we'd need |
| Cloud-agent triggering (Jules/Devin/Codex Cloud/Cursor Cloud) | NOT_FOUND | NOT_FOUND | YES (CODE_CONFIRMED, thin, single-shot) |
| GitHub issue triage | NOT_FOUND | NOT_FOUND | YES (CODE_CONFIRMED, single chat-completion classification) |
| Git/PR/merge governance (branches, PRs via `gh`) | Planned, not yet built (Slice 20) | Partial ("remote review" push, no `gh`/API integration confirmed) | NOT_FOUND |

## 8. OmniRoute vs our orchestrator

Different problem classes. Our orchestrator answers "did this WorkItem get
built correctly, safely, and auditably, across possibly-crashing
processes and possibly-different workers?" OmniRoute answers "which of 356
providers should serve this one HTTP chat-completion request, at what cost,
with how much of the prompt trimmed?" There is no meaningful capability
overlap at the orchestration level. The only genuine overlap is
**quota-awareness** as a concept — but even there the unit of governance
differs: ours governs *which worker (CLI identity) may run a WorkItem*;
OmniRoute governs *which provider connection may serve a request*.

## 9. OmniRoute vs Ralph

No meaningful overlap. Ralph is an execution-loop engine (hats, TDD,
worktrees, event bus). OmniRoute is a request router. OmniRoute does not
run TDD loops, does not manage git commits per iteration, does not have a
"hat" or role concept tied to an execution loop. `omniroute run claude`
merely launches the Claude Code CLI with its model backend redirected —
Ralph is not involved and is not replaced.

## 10. OmniRoute vs both combined

Even summing Ralph's and our orchestrator's capabilities, OmniRoute does
not cover the union: it has no WorkItem/MVP concept, no quality gate, no
independent review policy, no durable cold-restart handoff, no
release/roadmap governance, and no TDD execution loop. It *adds* things
neither of us has (356-provider routing, compression, free-tier
aggregation, MCP/A2A surfaces) but adds nothing that substitutes for either
Ralph's or our own core responsibilities.

## 11. Functional overlap

Genuine overlap, ranked by significance:

1. **Quota-awareness** (moderate overlap, different unit of governance — see
   §7, §19).
2. **Provider abstraction** (weak overlap — our `ProviderAdapter` abstracts
   two CLI subscription probes; OmniRoute abstracts 356 request-serving
   connections; different scale and purpose).
3. **CLI launching** (`omniroute run claude` vs our
   `RalphExecutionEngine` spawning `ralph`) — superficially similar
   ("launches a CLI subprocess") but OmniRoute's launch has no
   ExecutionRecord, no ExecutionRequest with quality tier, no verdict
   parsing, no audit trail; it is a convenience wrapper, not an execution
   engine.

Everything else (WorkItem lifecycle, review, quality gates, handoff,
reporting, roadmap/approval) has **zero** overlap — OmniRoute simply does
not model these concepts.

## 12. Components to KEEP (as-is)

All WorkItem-lifecycle and governance components have no OmniRoute
equivalent and should be kept unchanged: `ProjectStateStore`, `MVPManager`,
`HandoffStore`/`HandoffRecord`, `WaitCoordinator`, `RecoveryCoordinator`,
`QualityGateRunner`/`ValidationStore`, `ReviewStore`/review orchestration,
`ReleaseManager`/`ActivityReport`, `PlanningCoordinator`/`RoadmapProposal`,
`ApprovalCoordinator`, `RoadmapApplicationService`, `RealizationReport`/
`Service`/`Store`, `ExecutionStore`/`ExecutionRecord`, `WorkerSelector`,
`WorkerRegistry`, `ExecutionProfile`/`QualityTier`,
`ComplexityEstimation*`, `AdaptiveExecutionSelector`/`Decision`/
`DecisionStore`, `RalphExecutionEngine`/`ExecutionRequest`.

## 13. Components to EXTEND

None *require* extension because of OmniRoute. One **optional, low-priority**
future idea: `QuotaManager`/`ProviderAdapter` could gain an *additional*,
clearly-labeled `OmniRouteRoutingAdapter` used only if/when a Worker's
backend is itself configured to call models through OmniRoute (e.g. a future
`victor` profile whose underlying HTTP calls are OmniRoute-routed for cost
reasons) — this would be additive, optional, and never replace the existing
CLI-subscription probes for `alice`/`victor` as they exist today.

## 14. Components to REPLACE

None. No component in `src/orchestrator/` has an OmniRoute equivalent that
is both (a) functionally equal or superior and (b) semantically compatible
with our governance model (WorkItem-scoped, insert-only, fail-closed,
structural author≠reviewer). Replacing any of them with OmniRoute would be
a category error — OmniRoute has no substitute concept to replace them
*with*.

## 15. Components to REMOVE

None. Nothing in our stack becomes redundant by OmniRoute's existence.

## 16. New capabilities worth knowing about (not adopting now)

- **Free-tier aggregation/catalog** (`docs/reference/FREE_TIERS.md`,
  `/dashboard/free-tiers`): genuinely useful if we ever wanted to reduce
  *API* spend for a future non-CLI-subscription worker — irrelevant to our
  current two CLI-subscription workers.
- **Prompt compression** (RTK + "Caveman", documented 15-95%/~89% avg):
  potentially interesting for token-cost reduction on any future
  API-key-based worker; would need independent verification (see §23) and
  is not applicable to our current CLI-subscription-driven workers (Ralph
  hands the CLI a repo/workspace, not a raw prompt we control token-by-token).
- **Subscription-vs-metered billing classification**
  (`docs/routing/SUBSCRIPTION_LADDER.md`): a well-designed pattern
  (`ConnectionBillingClass = "subscription" | "metered" | "keyless" |
  "unknown"`, fail-closed to `metered` when uncurated) — conceptually
  validates our own principle of never conflating subscription and metered
  budgets, but is not directly reusable since it governs *request
  dispatch*, not *worker selection*.
- **MCP server** (`bin/mcp-server.mjs`): could be interesting later if we
  wanted MCP-based tool exposure of our own orchestrator to other agents —
  unrelated to today's need.
- **Circuit breaker pattern** (`closed/open/half_open`,
  `docs/OMNIROUTE_PROVIDER_FAILOVER.md`): a clean, well-documented
  resilience pattern; INSPIRE-only, our current retry/backoff needs (if
  any) are already narrower (CLI subprocess failures handled by
  `RalphExecutionEngine`/`RecoveryCoordinator`, not per-HTTP-request
  circuit state).

## 17. Routing analysis

Three candidate routing architectures were considered, per the audit
instructions:

- **Candidate A — CURRENT**: `WorkerSelector` + `AdaptiveExecutionSelector`
  remain the sole routing authority for *which Worker/Profile executes a
  WorkItem role*. OmniRoute, if ever adopted, would sit strictly *below*
  a Worker's backend call (invisible to WorkerSelector), never above it.
- **Candidate B — OmniRoute controls routing**: OmniRoute's `auto/*`
  strategies would decide which model/provider actually serves each
  request, effectively usurping `WorkerSelector`'s job. **Rejected**:
  OmniRoute has no concept of WorkItem, quality tier, author≠reviewer
  governance, or CLI-subscription probing for Claude Code Max/Codex ChatGPT
  Plus (its "subscription" rung governs *OAuth connections used as chat
  backends*, not *our CLI worker identities*). Adopting B would delete our
  entire governance model for a routing engine that cannot express it.
- **Candidate C — constrained hybrid**: `WorkerSelector` keeps full
  authority over Worker/Profile/quality-tier selection; only *within* a
  chosen Worker's own backend implementation, IF that backend is ever
  changed to be an HTTP-API call (not a CLI subprocess), an OmniRoute
  endpoint could be used as the HTTP transport for cost/failover reasons —
  fully invisible to and unconstrained-by OmniRoute's own routing logic
  (`auto/*` strategies would need to be pinned/disabled to prevent silent
  model substitution, which would violate our no-downgrade guarantee).

**Recommended: Candidate A (status quo)**, with Candidate C noted as a
possible, narrow, future POC-gated option (see §34) — never Candidate B.

## 18. Quality-tier / no-downgrade guarantee feasibility

**Verdict: NO** — OmniRoute cannot host this guarantee today.
`QualityTier`/no-downgrade is a *WorkItem-execution-role* concept
(SIMPLE/STANDARD/COMPLEX/CRITICAL mapped to Worker+Profile, filtered
*before* quota diagnosis). OmniRoute's `auto/*` routing optimizes for
cost/latency/quota across *interchangeable* model candidates within a
category (e.g. `auto/quality`) — it has no notion of "this WorkItem
requires at least COMPLEX and must never silently receive a SIMPLE-tier
model," and no mechanism analogous to our
`NoCapableProfileError`/fail-closed filter. Using OmniRoute's `auto/*` in
place of `AdaptiveExecutionSelector` would risk exactly the silent
downgrade our Slice 17 work was built to prevent. If OmniRoute were ever
used underneath a Worker (Candidate C, §17), the quality tier would have to
be pinned to an explicit, non-`auto` model id — never delegated to
OmniRoute's own scoring — to preserve the guarantee. UNKNOWN/PARTIAL only
in the narrow sense that OmniRoute's `auto/subscription` "fail closed, empty
pool is fine" pattern is philosophically compatible with fail-closed design
even though it answers a different question.

## 19. Quota strategy

**Decision: KEEP** our `QuotaManager`/`ProviderAdapter` design unchanged.
Do **not** REPLACE, and do not naively AGGREGATE.

Rationale: our quota model must never conflate Claude Code subscription
quota, Codex subscription quota, Anthropic API budget, OpenAI API budget,
any third-party budget, or a local-model budget (explicit, repeated
constraint). OmniRoute's quota engine (`src/lib/quota/*`: dimensions,
fair-share, burn-rate, plan registry/resolver, connection recovery,
saturation signals) is a *token/request-cost* accounting system scoped to
*API connections* it proxies — it is a different, per-request billing
ledger, not a per-CLI-subscription-window probe. Feeding our two CLI
subscription workers (`alice`/`victor`) through OmniRoute's quota engine
would require modeling them as "connections" inside a system built for
API-key/OAuth relaying, which is not what they are (they are pre-authenticated
CLI sessions Ralph shells out to directly) — this would be a net increase in
complexity and an indirection with no governance benefit, and a EXTEND-only
option (§13) is the most that is justified, and only if a future non-CLI,
API-key-based worker is ever added.

## 20. Resilience

Confirmed distinction, per the audit's stated intuition: OmniRoute delivers
**infra-level resilience** (circuit breakers with `closed/open/half_open`
state, classified transient-vs-permanent failures, bounded probe/cooldown
re-entry — `docs/OMNIROUTE_PROVIDER_FAILOVER.md`, CODE_CONFIRMED). Our
orchestrator delivers **business-level recovery**
(`RecoveryCoordinator.reconcile_work_item` distinguishing "still RUNNING
after a crash — unknown fate, must not be silently treated as
SUCCEEDED" from "INTERRUPTED but WorkItem never transitioned out of
RUNNING/REVIEWING"; `WaitCoordinator` translating a diagnosed quota
exhaustion into a scheduled resume). These operate at different layers and
are complementary in principle, not substitutable: OmniRoute's circuit
breaker could in theory protect a future HTTP-based provider call, but it
has no concept of "was this WorkItem's business outcome ever reliably
determined," which is exactly what `RecoveryCoordinator` exists for.

## 21. Context / handoff — can OmniRoute replace our durable cold-restart handoff?

**No — NOT_FOUND.** OmniRoute's `src/lib/memory/*` is a RAG-style
conversational memory system (vector store, typed decay, summarization,
injection with a token budget, Obsidian/Qdrant backends) designed to give a
*chat conversation* continuity/recall across turns. It is not: (a) scoped to
a WorkItem, (b) a structured, fact-only, insert-only record like our
`HandoffRecord` (completed_work / decisions / test_results / open_issues /
next_action / files_touched / git_sha_after), (c) proven to survive a
genuinely separate OS process picking up work a different CLI/worker left
off (our real smoke test, `docs/reports/real-cross-worker-resume-2026-09-13.html`,
demonstrates exactly this for `alice`→`victor`; nothing equivalent exists or
is claimed for OmniRoute). OmniRoute's A2A "task manager"
(`src/lib/a2a/taskManager.ts`) is a JSON-RPC task lifecycle for *routing
requests through OmniRoute as an agent skill provider*, not a durable
handoff mechanism for a third-party WorkItem — this matches the explicit
audit warning "A2A != durable handoff" precisely. **Decision: KEEP** our
`HandoffStore`/`HandoffRecord` unchanged.

## 22. Reports / audit — realization-report equivalent

**Decision: KEEP.** No OmniRoute feature produces anything resembling our
`RealizationReport` (a deterministic, non-LLM, insert-only, per-WorkItem
aggregation across recommendation/adaptive-decision/execution/handoff/
validation/review/wait facts, rendered to a self-contained, escaped HTML).
OmniRoute's `/dashboard` surfaces are operational dashboards for the router
itself (usage, quota, free-tier budget, gamification) — a different
audience and a different kind of fact (request-level telemetry, not
WorkItem-realization audit trail). REPLACE, EXTEND, and MERGE are all
inapplicable; there is nothing to replace/extend/merge with.

## 23. Cost / token optimization opportunities

Classified per the requested scheme:

- **Prompt compression (RTK + "Caveman")** — `MEDIUM_TERM`. The README
  claims are documentary (DOC_CONFIRMED: extensively documented in
  `docs/compression/`) and the code exists
  (`src/lib/compression/judgeModelClient.ts` and related, CODE_CONFIRMED),
  but I did not execute a benchmark myself this session, so the specific
  "15-95%, ~89% avg" figures remain **MARKETING_ONLY** from this audit's own
  standpoint until independently measured; also not directly usable while
  our two workers run as full CLI subprocesses (Ralph hands the whole
  repository/workspace to `claude`/`codex`, not a single controllable
  prompt string we could pre-compress).
- **Free-tier aggregation** — `LOW_VALUE` for us today (our workers are
  paid-subscription CLI sessions, not free-tier API calls), `MEDIUM_TERM`
  if a future non-CLI worker is ever added.
- **Subscription-vs-metered ladder** (`auto/subscription`,
  `auto/thrifty`) — `AVOID` for our current architecture (§19): adopting it
  would require re-modeling our CLI-subscription workers as OmniRoute
  "connections," a net complexity increase with no governance benefit.
- **Adopting OmniRoute wholesale for cost reasons** — `AVOID`: no measured
  cost problem in our current architecture justifies the migration risk
  (§31/§33).

## 24. Security

**Classification: LOW risk, if ever adopted narrowly (Candidate C, §17);
MEDIUM if ever exposed as a shared network service.** OmniRoute is a
network-facing HTTP gateway (Next.js server, `/v1/chat/completions`,
`/a2a`, `/api/mcp`, dashboard, WS) with its own auth/authz/CORS/origin
layers (`src/server/auth`, `authz/policies`, `cors`, `origin`), a
`SECURITY.md`, and a `docs/security/` directory covering WAF, ban
detection, CLI token handling, compliance, egress policy, error
sanitization, and even MITM/TLS-stealth documentation (the last being a
provider-evasion feature relevant to *providers'* own terms of service —
worth flagging as an operational/legal risk category distinct from
software security, not a vulnerability in OmniRoute itself). CI includes
CodeQL, Semgrep, a security scorecard, `gitleaks`, and nightly LLM-security
tests — a genuinely mature security posture (CODE_CONFIRMED). Mitigation if
ever adopted: run it network-isolated (localhost-only, as its own default
`http://localhost:20128` suggests), never give it credentials beyond what a
specific narrow use requires, and never let its `auto/*` routing touch a
governed WorkItem execution path (§17-18).

## 25. Maintainability

OmniRoute: very actively maintained (frequent releases, v3.8.49→v3.8.51 in
the README's own changelog table), large contributor base ("600
Contributors" referenced in README anchor), heavy CI investment, but also a
**very large, fast-moving surface** (82 `src/lib/*` modules, 132KB README,
2.6MB `CHANGELOG.md`, weekly-cadence provider additions) — a real
maintenance-burden risk *for us* if we depended on it: breaking changes,
provider deprecations, and a governance model (Node/TS, Next.js,
Docker/Electron) entirely foreign to our Python/sqlite3/stdlib-only stack.
Our own codebase: 12,069 lines, 25 modules, explicit "no dependency beyond
PyYAML" discipline, 766 tests, deliberately small and auditable. Depending
on OmniRoute for anything load-bearing would trade our current
low-maintenance, fully-understood stack for a dependency on a
100x-larger, externally-governed project whose roadmap we do not control.

## 26. Operations (Linux target)

OmniRoute: Docker image (with a documented `OMNIROUTE_MEMORY_MB=1024`
default that must be raised for coding-agent-sized contexts — a concrete
operational gotcha called out in its own README), `docker-compose.yml`,
`fly.toml`, a `flake.nix`, systemd-adjacent guidance in
`docs/DEVELOPER-ENVIRONMENT.md`. Runs as a persistent network service (not
a one-shot CLI invocation), which is a meaningfully different operational
model from our orchestrator's current fully-local, no-daemon, subprocess-
per-execution design. Adopting it, even narrowly, adds an always-on service
to operate, monitor, and patch — a real operational cost that must be
weighed against any narrow benefit (§17 Candidate C).

## 27. Build-vs-adopt scenarios (S1–S7)

| Scenario | Functional coverage of our need | Custom code remaining | Reliability/audit | Recovery | Quota correctness (subscription-safe) | Cost | Maintainability | Extensibility | Operational complexity | Migration effort | Vendor/dependency risk |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 — Keep current architecture as-is | Full (already built for our need) | ~12k LOC, unchanged | Strong (fail-closed, insert-only, tested) | Strong (RecoveryCoordinator) | Correct by construction | No new spend | High (small, owned) | High (our own extension points) | Low (no daemon) | None | None |
| S2 — Add OmniRoute optionally, underneath one future non-CLI worker | Full + optional cost/routing benefit for a hypothetical future API-key worker | ~12k LOC + a small, isolated adapter | Unchanged for existing workers | Unchanged | Correct if kept out of CLI-subscription path | Possible token savings on that one hypothetical worker | Slightly lower (one more moving part, narrowly scoped) | Slightly higher (one more provider option) | Slightly higher (one more service to run) | Low (opt-in, additive) | Low (isolated, disable-able) |
| S3 — Adopt OmniRoute and simplify our stack | Severe functional loss (no WorkItem/review/gate/handoff/report/roadmap concepts) | Would still need most of our 12k LOC rebuilt on top anyway | Would need to be rebuilt | Would need to be rebuilt | Would need re-derivation, current model doesn't fit | Unclear/likely negative net (engineering cost) | Lower (foreign large stack) | Unclear | Higher (daemon + our layer) | Very high | High (project-governance risk) |
| S4 — Replace Ralph with OmniRoute | Not possible — OmniRoute has no TDD/hats/execution-loop concept | Would need a new execution engine built from scratch | N/A | N/A | N/A | N/A | N/A | N/A | N/A | Infeasible | N/A |
| S5 — Reduce orchestrator to a thin layer, delegate rest to OmniRoute | Severe functional loss (same as S3, nothing to delegate WorkItem-governance *to*) | Would remove real, working, tested governance code for no replacement | Would regress | Would regress | Would regress | Unclear | Unclear | Unclear | Unclear | High | High |
| S6 — Stop ai-dev-orchestrator, use OmniRoute only | Total loss of WorkItem lifecycle, review policy, handoff, reporting, roadmap governance | 0 (all deleted) | None (nothing left to audit our development process) | None | None (no CLI-subscription concept) | N/A | N/A (not our problem anymore, but neither is the value) | N/A | N/A | N/A (destructive) | N/A |
| S7 — Stop both projects, use OmniRoute only | Same as S6, plus loses the TDD/hats execution loop too | 0 | None | None | None | N/A | N/A | N/A | N/A | N/A | N/A |

S1 dominates on every axis relevant to our actual need. S2 is the only
scenario that adds anything without subtracting anything, and only for a
hypothetical future worker type we do not have today — hence it is noted as
optional/future, not adopted now (§35). S3–S7 all fail on functional
coverage of the one problem this project exists to solve.

## 28. Minimum custom differentiator (only if warranted)

Not warranted. The evidence in §7–§22 does not support reducing our custom
code — OmniRoute covers none of our differentiating functional surface
(WorkItem lifecycle, adaptive quality-tier selection, structural
author≠reviewer, durable cold-restart handoff, realization/activity
reporting, roadmap synthesis+approval). There is no "Thin AI Dev
Governance" layer to retain because there is nothing to hand off to
OmniRoute in the first place.

## 29. Component-by-component decision

| Component | Decision |
|---|---|
| ProviderAdapters (ClaudeCodeAdapter, CodexAdapter) | KEEP_AS_IS |
| QuotaManager | KEEP_AS_IS |
| WorkerRegistry | KEEP_AS_IS |
| ExecutionProfile / QualityTier | KEEP_AS_IS |
| WorkerSelector | KEEP_AS_IS |
| ComplexityEstimator (ComplexityEstimationRequest/Service/Store) | KEEP_AS_IS |
| AdaptiveExecutionSelector | KEEP_AS_IS |
| RalphExecutionEngine | KEEP_AS_IS |
| ExecutionStore | KEEP_AS_IS |
| HandoffStore | KEEP_AS_IS |
| WaitCoordinator | KEEP_AS_IS |
| RecoveryCoordinator | KEEP_AS_IS |
| QualityGateRunner | KEEP_AS_IS |
| Review orchestration | KEEP_AS_IS |
| Planning orchestration | KEEP_AS_IS |
| ReleaseManager | KEEP_AS_IS |
| RealizationReport | KEEP_AS_IS |
| RoadmapApplicationService | KEEP_AS_IS |

No component is even KEEP_AND_EXTEND today — extension (§13) is explicitly
optional and gated on a future, currently-hypothetical, non-CLI worker
type. `DELEGATE_PARTIALLY`, `PARTIALLY_REPLACE`, `FULLY_REPLACE`, and
`REMOVE` are not used for any component: none is justified by the evidence
gathered.

## 30. Weighted scorecard (/100)

Weights: Functional coverage 20, Reliability/recovery 15,
Quality/governance 15, Maintainability 10, Routing/quota/cost 10,
Auditability 10, Operational simplicity 10, Extensibility 5, Provider
independence 5.

| Architecture | Functional (20) | Reliability (15) | Quality/gov (15) | Maintainability (10) | Routing/quota/cost (10) | Auditability (10) | Op. simplicity (10) | Extensibility (5) | Provider independence (5) | **Total /100** |
|---|---|---|---|---|---|---|---|---|---|---|
| OUR_STACK (current) | 20 | 14 | 15 | 9 | 6 | 10 | 9 | 4 | 5 | **92** |
| OUR_STACK_PLUS_OMNIROUTE (S2, optional/future) | 20 | 14 | 15 | 8 | 8 | 10 | 7 | 5 | 5 | **92** |
| NO_RALPH (hypothetical, build our own exec engine) | 12 | 6 | 10 | 4 | 6 | 7 | 5 | 3 | 5 | **58** |
| THIN_ORCHESTRATOR (delegate governance to OmniRoute) | 4 | 3 | 3 | 6 | 8 | 3 | 6 | 3 | 2 | **38** |
| OMNIROUTE_ONLY | 2 | 2 | 1 | 6 | 9 | 2 | 5 | 2 | 1 | **30** |

Justification for the significant gaps:

- **OMNIROUTE_ONLY / THIN_ORCHESTRATOR score near-zero on
  Functional/Reliability/Quality-governance/Auditability** because OmniRoute
  has no WorkItem, no quality gate, no review policy, and no durable
  cross-worker handoff — the exact things those weights (20+15+15+10=60 of
  100 points) are measuring.
- **NO_RALPH scores low on Functional/Reliability** because Ralph's TDD/hats
  execution loop is validated, working infrastructure (`docs/SPIKE_RALPH.md`)
  that would have to be rebuilt from scratch at high risk for no evidenced
  benefit.
- **OUR_STACK and OUR_STACK_PLUS_OMNIROUTE tie at 92** because the optional
  OmniRoute addition (§13, §27 S2) is additive and narrowly scoped — it
  trades a couple of Maintainability/Operational-simplicity points for a
  couple of Routing/quota/cost and Extensibility points, net-neutral, and is
  explicitly not adopted now (§35).
- Nothing scores 100 because even OUR_STACK has room to grow (Slice 19/20
  not yet built, per `ROADMAP.md`) — the scorecard reflects the
  architecture's fitness, not a claim that construction is finished.

## 31. Migration plan

**Not applicable as a mandatory action** — no migration is recommended
(§35: KEEP_CURRENT_ARCHITECTURE). If the optional S2 addition (§13, §27) is
ever pursued in a future session, the plan would be: (1) confirm a genuine
non-CLI, API-key-based worker need exists; (2) build an isolated
`OmniRouteRoutingAdapter` implementing our existing `ProviderAdapter`
interface, pinned to an explicit non-`auto` model id; (3) add it to
`config/workers.yaml` as a new, clearly-labeled profile, never replacing
`alice`/`victor`'s existing CLI-subscription profiles; (4) run the existing
offline test suite plus a new adapter-specific test suite; (5) run one
cost-bounded real smoke test before trusting it in the loop. No existing
component would be modified in this plan — pure addition.

## 32. Rollback plan

Not applicable — no change is being made this session (§35), so there is
nothing to roll back. If the optional S2 addition described in §31 is ever
implemented in a future session, its rollback would be: remove the new
adapter/profile from `config/workers.yaml` and delete the adapter module;
because it is purely additive and never touches `alice`/`victor`'s existing
profiles, no data migration or backfill would be needed to roll back.

## 33. Risks

- **Risk of doing nothing (status quo)**: none identified beyond the
  already-known, already-tracked remaining roadmap work (Slice 19/20). This
  audit did not surface any new risk to the current architecture.
- **Risk of adopting OmniRoute narrowly (S2, not adopted now)**: adds an
  always-on network service and a large, fast-moving external dependency
  (§25, §26) for a currently-hypothetical benefit; must be re-justified by
  a real future need, not adopted speculatively.
- **Risk of adopting OmniRoute broadly (S3–S7)**: total loss of the
  functional surface this project exists to provide (§27); not
  recommended under any circumstance current evidence supports.
- **Risk of this audit itself**: OmniRoute is an extremely large codebase
  (82 `src/lib/*` modules, 132KB README); this audit read broadly (README,
  key docs, module structure, representative source files, quota/routing/
  resilience/security subsystems, tests count, CI config) but did not
  execute OmniRoute's test suite or benchmark its compression claims —
  flagged explicitly throughout (§16, §23) rather than asserted as fact.

## 34. POC needed (proposed, NOT executed this session)

Given the strength and breadth of evidence already gathered, **no POC is
required to support the primary decision** (KEEP_CURRENT_ARCHITECTURE) —
the functional-coverage gap is not a close call. A POC would only be
warranted if the *optional* S2 addition (§13, §27, §31) is ever seriously
considered in a future session. Proposed POC, described only, not executed:

1. Add a throwaway `omniroute` worker profile pointed at a locally-run
   OmniRoute instance (`docker run` or `npx omniroute`), pinned to one
   explicit non-`auto` model id (never `auto/*`, to preserve the
   no-downgrade guarantee).
2. Run one disposable WorkItem (same "Fix add() to actually add" scenario
   used in the existing real smoke test) with this worker in the developer
   role, through the real `RalphExecutionEngine`.
3. Verify: (a) `ExecutionRecord` and quality gate still function
   unchanged; (b) OmniRoute's own quota/circuit-breaker state never leaks
   into or overrides `WorkerSelector`'s decision; (c) killing the OmniRoute
   process mid-execution produces the same `RECOVERY_REQUIRED` handling as
   any other backend failure, with no special-casing needed.
4. Explicitly do **not** wire `auto/*` routing into this POC — pin the
   model id, to test the addition in isolation from OmniRoute's own
   decision-making.

## 35. Final decision

### DECISION

`KEEP_CURRENT_ARCHITECTURE`

### SECOND_BEST_OPTION

`ADD_OMNIROUTE_OPTIONALLY` — narrowly, as a future, opt-in routing/cost
backend underneath a hypothetical non-CLI, API-key-based worker (§13, §27
S2, §31). Not adopted this session; would need a real future need to
justify even the narrow POC in §34.

### WHAT_WOULD_CHANGE_MY_MIND

- Discovering that OmniRoute (in a version not yet studied) has added a
  genuine WorkItem/task-dependency/quality-gate/independent-review model —
  none of which exists in the commit studied (`152d951`).
- A real, measured cost problem in our current architecture (e.g., API
  spend on a future non-CLI worker) that OmniRoute's routing/compression
  could concretely and safely reduce, re-evaluated through the POC in §34
  rather than assumed from OmniRoute's own marketing claims.
- Evidence that Ralph itself becomes unmaintained/abandoned, which would
  motivate re-running the original `docs/ECOSYSTEM.md`/`docs/SPIKE_RALPH.md`
  study against new candidates — OmniRoute would still not be a candidate
  replacement for Ralph specifically, since it has no execution-loop
  concept at all (§9).

### STOP PROJECT

- Can OmniRoute replace `ai-dev-orchestrator`? **NO**
- Can OmniRoute replace Ralph? **NO**
- Can OmniRoute replace both? **NO**
- `STOP_AI_DEV_ORCHESTRATOR?` **NO**
- `STOP_RALPH?` **NO**
- No migration plan is given for a stop-decision because none is warranted
  by the evidence gathered in this audit.

---

## Process note (independence)

This report was fully drafted, including this Final decision section,
**before** any Codex report's content was read. Per instructions, only
filenames were checked in advance (never content) to avoid an accidental
overwrite; that filename-only check, and the resulting
`CODEX_REPORT_NOT_AVAILABLE` / arbitration outcome, is recorded separately
in the session's final response (and, if a Codex report was found, in
`docs/OMNIROUTE_ARBITRATION.md`) — never inside this file, to keep this
document an honest, unedited record of Claude's independent conclusion.
