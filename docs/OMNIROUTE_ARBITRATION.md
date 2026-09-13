# OmniRoute Arbitration — Claude vs Codex

Date: 2026-09-13. This document compares two **independent** audits of the
same question (does OmniRoute change whether `ai-dev-orchestrator`/Ralph
should keep existing), written without either author reading the other's
report first:

- Claude: `docs/OMNIROUTE_OPPORTUNITY_REPORT_CLAUDE.md` /
  `docs/reports/omniroute-opportunity-report-claude.html`
- Codex: `docs/OMNIROUTE_OPPORTUNITY_REPORT.md` /
  `docs/reports/omniroute-opportunity-report.html`

**Goal of this document is to surface disagreements, not to confirm
agreement.** Per instruction: a decision is never made by vote — "2 models
against 1" is not evidence; the better-substantiated technical claim wins,
with an explicit confidence level and, where relevant, a note on what
evidence is still missing.

Both audits agree on far more than they disagree on (see §2), but they
diverge meaningfully on **how soon and how formally** to act on OmniRoute,
and Codex's audit is materially deeper on OmniRoute's internal code (it
read `open-sse/services/*`, which Claude's audit did not open at all —
see §3, "blind spot" note). That asymmetry is treated honestly below
rather than smoothed over.

---

## 1. Headline recommendations, side by side

| | Claude | Codex |
|---|---|---|
| Primary decision | `KEEP_CURRENT_ARCHITECTURE` | `GO_INCREMENTALLY` (not one of the 8 canonical labels — see §4 mapping) |
| Architecture | A (status quo); C noted as a possible future, POC-gated option | C ("hybride sous contraintes") |
| Timing | Wait for a real, proven need (non-CLI/API-key worker) before even spiking | Start a gated spike now (`OR-0` qualification spike), before any real need is proven |
| Stop ai-dev-orchestrator? | NO | NO (implicit — never proposed, "aucun remplacement de composant métier n'est justifié") |
| Stop Ralph? | NO | NO (explicit — "Ne pas remplacer Ralph par un pipeline OmniRoute") |
| Component replacements | None | None ("Aucun FULLY REPLACE ni REMOVE n'est recommandé") |

## 2. Where the two audits agree (high confidence, not treated as a mere headcount — both reached the same conclusion via independent, non-overlapping evidence)

- OmniRoute has **no** WorkItem/MVP/quality-gate/independent-review/
  durable-cold-restart-handoff/roadmap-approval equivalent. Neither audit
  found one anywhere in the codebase.
- OmniRoute is **not** a Ralph substitute — no hats/event-bus/TDD-loop
  concept exists in it.
- No orchestrator component should be `FULLY_REPLACE`d or `REMOVE`d by
  OmniRoute today.
- Our CLI-subscription quota model (Claude Code Max / Codex ChatGPT Plus)
  must **never** be naively merged into OmniRoute's connection/API quota
  ledger — the governance unit differs.
- Context/session continuity features in OmniRoute are conversational, not
  business-fact handoff, and are not a substitute for `HandoffRecord`.
- `auto/*` routing must never be allowed to silently choose the executed
  model for a governed WorkItem — any integration must pin/certify the
  candidate set before dispatch, never delegate to open-ended scoring.
- OmniRoute's engineering/CI maturity is real and worth acknowledging
  honestly, without inflating it into a reason to adopt it structurally.
- MIT licence permits dependency use, but **not** an unconditional green
  light to redistribute the full bundle (asset/notice provenance is
  incomplete for several third-party assets — Codex traced this in detail
  via `THIRD_PARTY_NOTICES.md`; Claude only read the root `LICENSE` file
  and did not independently discover the asset-notice caveat).
- Neither audit executed OmniRoute's test suite, started its gateway,
  performed a live probe, or consumed a reset credit. Both explicitly flag
  the resulting evidence gaps rather than asserting untested claims as fact.

## 3. Where the two audits differ

### 3.1 Depth of source coverage (methodology gap, not a disagreement of opinion)

Codex's audit reads and cites dozens of specific files under
`open-sse/services/*`, `open-sse/executors/*`, `open-sse/translator/*`
(e.g. `autoStrategy.ts`, `engine.ts`, `circuitBreaker.ts`, `contextHandoff.ts`,
`codexQuotaFetcher.ts`, `semanticCache.ts`) with exact commit-pinned GitHub
links and, in several cases, line numbers. Claude's audit explored
`src/lib/*`, `src/app/*`, `docs/*`, and representative source files, but
**never opened the `open-sse/` directory** — a real, material blind spot
this document surfaces rather than hides.

Concrete consequences of that gap, found by cross-checking Codex's citations
against what Claude's report claimed:

| Claim | Claude's report | Codex's report | Who's better substantiated |
|---|---|---|---|
| Circuit breaker states | "`closed`/`open`/`half_open`" (3 states, sourced from a documentation file, `docs/OMNIROUTE_PROVIDER_FAILOVER.md`) | "`CLOSED`/`DEGRADED`/`OPEN`/`HALF_OPEN`" (4 states, sourced directly from `src/shared/utils/circuitBreaker.ts`) | **Codex** — direct code citation vs. a doc file that turns out to under-describe the actual implementation. **CONFIRMED** by the nature of the sources; Claude's doc-only source was insufficient here. |
| Reset-credit consumption | Described only as a documented *idempotent, read-only-adjacent* `ensurePool`/status concept (`docs/OMNIROUTE_ALLOCATION_HANDOFF.md`) — did not identify an actual consume endpoint | Identifies a concrete `POST` endpoint that consumes Codex/Grok reset credits with an idempotency key (`src/app/api/usage/codex-reset-credit/route.ts`) | **Codex** — this is a materially important, concrete security-relevant finding (a real consume action exists and must be explicitly blocked) that Claude's audit did not surface at all. |
| Context/session handoff persistence | Characterized OmniRoute's context/memory system as RAG-style, `src/lib/memory/*` only (vector store, decay, summarization) | Identifies a **separate**, SQLite-persisted `contextHandoffs` table with summary/decisions/progress/entities and a 5-hour default TTL (`open-sse/services/contextHandoff.ts`), distinct from `src/lib/memory/*` | **Codex** — more complete; this is genuinely a different, more structured mechanism than the one Claude described, though Codex is careful to still classify it as conversational, TTL-bound, and not a substitute for `HandoffRecord` (a conclusion both audits reach, just via different, complementary evidence). |
| Number of routing strategies | Accepted the README's own "19 routing strategies" figure at face value | Counted the actual typed strategy registry in code: **20**, plus an internal `quota-share` not exposed publicly, and flagged the README/marketing figure as stale | **Codex** — code-counted vs. marketing-quoted; Codex's number is more trustworthy, though both agree the exact count is a minor, non-decision-relevant detail. |

**Confidence: HIGH** that Codex's OmniRoute-side evidence is more complete
on these four specific points, because each is independently verifiable by
opening the exact file Codex cites (this document did not re-fetch GitHub
to re-verify every citation, so treat the underlying file *contents* as
Codex-reported until independently re-checked — but the *category* of gap
["Claude didn't look at `open-sse/`"] is self-evidently correct from
Claude's own report's absence of any `open-sse/` reference).

### 3.2 Depth of source coverage on OUR OWN codebase (the reverse gap)

Codex's audit also corrects several claims about our *own* code that
neither report's predecessor sessions had stated precisely, and that this
arbitration independently re-verified this session (commands run directly
against the working tree, not taken on faith from either report):

| Codex's claim about our own code | Independently re-verified this session | Verdict |
|---|---|---|
| `config/workers.yaml` declares `code_review` as a capability, but `MVPManager.REVIEW_CAPABILITY = "reviewer"` is the string actually required when selecting a reviewer — a real naming mismatch, not just a documentation nit | `grep -n "code_review\|REVIEW_CAPABILITY" config/workers.yaml src/orchestrator/mvp_manager.py` confirms: `config/workers.yaml` lines 35/68 declare `code_review`; `mvp_manager.py` line 211 defines `REVIEW_CAPABILITY = "reviewer"`; no alias/mapping exists anywhere in `src/orchestrator/` (checked via repo-wide grep) | **CONFIRMED.** As configured today, a reviewer-capability WorkItem selection (`required_capabilities={"reviewer"}`) would find zero eligible workers against the current `config/workers.yaml`, since neither `alice` nor `victor` declares the literal string `"reviewer"`. This is a real, pre-existing gap in this repository, unrelated to OmniRoute, worth fixing in a near-future slice. Not caused by, and not fixed by, anything in this audit. |
| No `CRITICAL`-tier profile is configured for either worker | `grep -n "quality_tier:" config/workers.yaml` confirms only `SIMPLE`/`STANDARD`/`COMPLEX` profiles exist for both `alice` and `victor` | **CONFIRMED.** Consistent with the fail-closed design (`NoCapableProfileError`, never a fabricated `WAITING`) — an operational fact worth knowing, not a defect. |

**Confidence: HIGH** on both (directly re-run against the current working
tree in this session, not merely trusted from either report).

### 3.3 The real strategic disagreement: timing and formality, not direction

Both audits land in the same *category* of the 8-option list once Codex's
own vocabulary is mapped onto it (see §4) — neither recommends adopting
OmniRoute broadly, replacing Ralph, or stopping either project. The actual
disagreement is:

- **Claude**: do nothing now; OmniRoute's optional value is real but
  currently *hypothetical* (we have no non-CLI, API-key-based worker
  today), so even the narrow POC should wait for a concrete, proven need.
- **Codex**: start a gated, reversible qualification spike **now**
  (`OR-0`), specifically because OmniRoute's infra-level resilience/quota-
  telemetry/multi-provider breadth are "coûteux à reconstruire" and the
  cost of *investigating* (not adopting) is low and bounded, provided the
  validation gates in Codex §24.2 are respected and nothing is adopted
  until they pass.

**Confidence: MEDIUM** on which timing is better. Codex's position is
better substantiated on the specific claim that these capabilities
(circuit breaker, quota telemetry breadth, cost/token visibility) are
"coûteux à reconstruire" in Python — this is plausible and consistent with
what both audits found, but neither audit produced a build-cost estimate
for the *narrow* pieces we'd actually want (e.g., a simple circuit breaker
around subprocess retries is a small, well-understood pattern; we may not
need OmniRoute's full breadth to get it). Claude's position is better
substantiated on risk-minimization: no measured cost/reliability problem
in the current architecture was found by *either* audit to justify
spending engineering time now, even on a bounded spike, and Codex's own
`OR-0`→`OR-7` roadmap is itself a non-trivial, multi-slice undertaking
(P0/P1 items spanning quota-subject modeling, candidate-set certification,
resilience wiring, and cost ledgers) that competes directly for the same
engineering time as the already-planned Slice 19/20. **Missing evidence**:
a concrete trigger event (a real non-CLI worker request, a measured cost
problem, or Ralph's own health degrading) that would make the spike's
opportunity cost clearly worth paying now rather than later — neither
audit has one.

### 3.4 Secondary disagreements

| Topic | Claude | Codex | Agreement? | Arbitration |
|---|---|---|---|---|
| Reasoning-effort transport for Claude Code (native, non-OmniRoute) | Not examined as a gap in our own engine | Found that `_build_backend_args` only ever passes `--model` for the Claude Code backend — `reasoning_effort` is not actually transmitted to the Claude Code CLI today, independent of OmniRoute entirely | No — Claude simply didn't check this | **Codex**, confirmed as plausible and independently checkable (not re-verified in this session due to scope — flagged as a good candidate for a quick follow-up check, unrelated to the OmniRoute decision itself). Confidence: MEDIUM (Codex's own citation is specific — `a-engine` — but this arbitration did not re-run the grep). |
| WorkerSelector's economic ranking | Described as "capability > governance > quota > cost" per the module's own docstring, without further scrutiny | Clarifies precisely: ranking is priority-descending then `worker_id` lexical; `cost_rank` only matters *within* an already-chosen worker's own profile selection (i.e., no cross-worker global cost optimum) | Not a contradiction — Claude's language was imprecise/aggregate, Codex's is exact | **Codex**'s formulation is more precise and should be treated as the accurate description going forward. |
| OmniRoute's own historical-outcome/eval-based routing | Not investigated in depth | Investigated in detail (EWMA operational-quality signal, `evalRouting` persisted-eval ranking) and proposes a concrete, safe cooperation model (labels from our own gates/reviews feeding a *preference*, never a downgrade) | Not a contradiction — depth difference | Codex's treatment is the more usable one if this is ever pursued; Claude's report did not cover this facet in comparable depth. |

## 4. Mapping Codex's vocabulary onto the required 8-option list

Codex's report does not use the canonical 8-label vocabulary
(`KEEP_CURRENT_ARCHITECTURE`, `ADD_OMNIROUTE_OPTIONALLY`, etc.) verbatim —
it uses its own `GO_INCREMENTALLY` / "Architecture C hybride" / "Scénario
HYBRID (démarré par MINIMAL)" language. Mapping it honestly onto the
required list, based on what it actually recommends (no component
replacement, native path stays fully available and is the fallback if
gates fail, OmniRoute only ever sits underneath and only for a strictly
certified, pinned, non-`auto` candidate set): the closest canonical label
is **`ADD_OMNIROUTE_OPTIONALLY`** — with the caveat that Codex wants the
qualification spike (`OR-0`) started now rather than deferred to a proven
future need, which is a timing distinction the 8-label vocabulary does not
itself capture.

## 5. Confidence summary for every significant decision

| Decision point | Confidence | Why |
|---|---|---|
| OmniRoute has no WorkItem/MVP/review/gate/handoff/roadmap equivalent | **HIGH** | Both audits independently searched the entire relevant surface (Claude: `src/lib`, `src/app`; Codex: same plus `open-sse/`) and found nothing; absence is consistent across two independent, broad searches. |
| OmniRoute is not a Ralph substitute | **HIGH** | Same as above; no hats/event-bus/TDD-loop concept found by either audit. |
| No component should be FULLY_REPLACE'd or REMOVE'd today | **HIGH** | Unanimous, and each audit reached it via different reasoning paths (functional-coverage gap vs. component-by-component migration-cost analysis), which strengthens rather than merely repeats the conclusion. |
| Our quota model must not be naively merged with OmniRoute's | **HIGH** | Both independently identified the same root cause (different governance unit: CLI-subscription-window vs. per-connection/API billing), via non-overlapping evidence (Claude: general architecture read; Codex: a six-population table A–F cross-checked against specific OmniRoute fetcher code). |
| Whether to start a gated spike now vs. wait for a proven need | **MEDIUM** | Real, substantive disagreement (§3.3) neither audit fully resolves with hard evidence; the "missing evidence" is a concrete trigger event, absent from both reports. |
| Exact internal capability count/detail of OmniRoute's resilience/quota/context subsystems | **MEDIUM-HIGH favoring Codex** | Codex's citations are code-level and more complete (§3.1); this arbitration spot-checked the *category* of the gap but did not re-fetch every GitHub citation this session, so treat Codex's specific numbers as the better-sourced default, pending independent re-verification if this ever becomes decision-relevant. |
| `config/workers.yaml`/`REVIEW_CAPABILITY` naming mismatch is real | **HIGH** | Independently re-verified in this session via direct `grep` against the current working tree — not taken on either report's word. |

## 6. Final arbitration

### CLAUDE_RECOMMENDATION
`KEEP_CURRENT_ARCHITECTURE` (do nothing now; revisit if a concrete non-CLI
worker need or a measured cost/reliability problem ever arises).

### CODEX_RECOMMENDATION
Closest canonical mapping: `ADD_OMNIROUTE_OPTIONALLY`, but with an
immediate, gated qualification spike (`OR-0`) rather than a deferred one —
see §4.

### EVIDENCE_BASED_ARBITRATION

Both reports agree on every structural question that actually decides
whether either project should stop, be replaced, or be reduced (§2) — that
agreement is not being used as a tie-breaker here (per the explicit
no-vote rule), it is simply the actual, independently-reached, evidence-
backed shape of the problem: **OmniRoute does not threaten
`ai-dev-orchestrator` or Ralph, and no component should be replaced or
removed today.** On that — the question this whole audit exists to answer
— there is no real disagreement to arbitrate.

The one substantive disagreement (§3.3, timing of a spike) is resolved as
follows, on the evidence actually presented rather than by preferring
either author: **`ADD_OMNIROUTE_OPTIONALLY`, deferred until a concrete
trigger, remains the better-supported position, but the bar for "concrete
trigger" should explicitly include the kind of low-cost, bounded
qualification step Codex describes as `OR-0`** (a throwaway, isolated,
non-`auto`, upstream-pinned instance, no production credentials, no
integration into `WorkerSelector`) — i.e., Codex's `OR-0` step is cheap and
reversible enough that it does not actually require waiting for a "real"
production need to be worth running once, specifically to convert the
current, honestly-labeled **UNKNOWN**s (real p50/p95 latency overhead,
real compression benefit on our own workload, real operational cost of
running the service) into measured facts — but everything past `OR-0`
(the `OR-1`...`OR-7` roadmap, i.e. actually wiring OmniRoute into
`WorkerSelector`/`AdaptiveExecutionSelector`/`QuotaManager`) should remain
gated on a proven need, exactly as Claude's report argues, and exactly as
Codex's own validation gates (§24.2 of the Codex report) already require
before any of that can happen.

In other words: this arbitration synthesizes the two reports into a
narrower position than either stated in isolation — **run the smallest
possible `OR-0`-style measurement spike opportunistically, in a future
session, with no code in `src/orchestrator/` touched and no credentials
exposed; do not commit to `OR-1` or beyond without a concrete need.** This
is not a new 9th option; it collapses cleanly onto
`ADD_OMNIROUTE_OPTIONALLY` in the 8-option vocabulary, with the
qualification-spike detail preserved as an implementation note rather than
a different category of decision.

**Confidence in this synthesis: MEDIUM-HIGH.** High on the "no replacement,
no removal, no stop" core (§2, §5); medium on the exact trigger threshold
for even the narrow spike, because neither audit measured OmniRoute's
actual overhead/benefit on our real workload — that remains the
single largest piece of missing evidence in both reports.

### STOP PROJECT — arbitrated final answer

- Can OmniRoute replace `ai-dev-orchestrator`? **NO** (both audits, HIGH confidence)
- Can OmniRoute replace Ralph? **NO** (both audits, HIGH confidence)
- Can OmniRoute replace both? **NO** (both audits, HIGH confidence)
- `STOP_AI_DEV_ORCHESTRATOR?` **NO**
- `STOP_RALPH?` **NO**

No migration or stop plan is given, because none is warranted by either
audit's evidence, independently or combined.

---

## Process note

This arbitration was written after, and only after, both
`docs/OMNIROUTE_OPPORTUNITY_REPORT_CLAUDE.md` (finalized first, before any
Codex content was read) and `docs/OMNIROUTE_OPPORTUNITY_REPORT.md` (Codex's
pre-existing report, discovered by filename check and read for the first
time only after Claude's own report was already complete) existed on disk.
Per instruction, disagreement-hunting — not agreement-confirming — was the
explicit goal; where the two reports turned out to agree on the structural
questions, that is reported as what the evidence actually showed, not
smoothed over to manufacture false balance, and where they disagreed
(§3, §3.3), that disagreement is preserved rather than resolved by simple
majority.
