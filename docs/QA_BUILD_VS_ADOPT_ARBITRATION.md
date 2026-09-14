# QA Build-vs-Adopt Arbitration — Claude vs Codex

User arbitration session, 2026-09-14. Both studies
(`docs/QA_BUILD_VS_ADOPT_REPORT_CLAUDE.md` and `docs/QA_BUILD_VS_ADOPT_REPORT.md`)
were written independently — Codex's report explicitly confirms it did not
read Claude's report as a source, only preserved it untouched on disk (see
its own "Publication roadmap et provenance" section). This document records
the **user's decision**, grounded in the facts both studies actually
establish — never decided by a vote between the two scores.

## 1. Headline recommendations, side by side

| | Claude | Codex |
|---|---|---|
| PRIMARY | `HYBRID` (80/100) | `BUILD_INTERNAL` (75/100) |
| SECOND_BEST | `BUILD_INTERNAL` (76/100) | `HYBRID` (74/100) |
| Gap between primary and second | 4 points | 1 point |
| Direct external possible architecturally | Yes, no candidate approved as sole final gate today | Yes, no candidate approved as sole final gate today |
| Best general external, autonomous web/API | TestSprite (best-evidenced SHA/diff scoping) | TestSprite, **explicitly conditional** — blocked on documented V3 target/healing gaps |
| Best repo-centric web challenger | Momentic (close second) | Momentic — SHA/recovery/policies still to qualify |
| Best real browser/mobile | BrowserStack | BrowserStack |
| Best Java unit-test generation | Diffblue Cover | Diffblue Cover |
| Slice 23 proposal | `InternalQAEngine` first, `TestSpriteQAEngine` adapter once POC 1 confirms feasibility — PENDING USER ARBITRATION | `InternalQAEngine` MVP mince — PENDING USER APPROVAL |

**The rule for this document, explicit per the user's instruction: "Ne pas
arbitrer par les scores."** An 80-vs-75 or 76-vs-75 gap on a self-constructed,
non-standardized 100-point rubric is not evidence of anything — it reflects
each model's own weighting choices on axes like "Maintainability" or "Cost"
that neither study treats as decision-critical for the actual question at
hand (which engine, if any, becomes the mandatory final QA gate today).

## 2. What both studies actually establish, independently, with different evidence

Despite the headline PRIMARY differing (`HYBRID` vs `BUILD_INTERNAL`), the
two studies converge on every fact that is actually load-bearing for this
arbitration:

1. **No external candidate — TestSprite, BrowserStack, Momentic, or
   Diffblue — passes all elimination gates to become the sole, mandatory
   final QA gate today.** Both reports state this explicitly (Claude §31,
   Codex §29). Neither treats this as a close call.
2. **`InternalQAEngine` is the only candidate that passes every gate today,
   by construction** — because this project controls its own contract, and
   the existing substrate (`QualityGateRunner`, `GitGovernanceService`
   SHA-binding, `ExecutionStore`/audit patterns) already covers most of
   what a mandatory gate needs (Claude §3/§10, Codex §2's capability table).
   Both reports independently estimate this substrate at roughly 70% built.
3. **Every external candidate has a genuine, real reason it cannot be the
   sole gate right now** — not a vague "needs more research" hedge:
   - TestSprite: Codex's audit went further and found the sharper evidence
     — pinned to a specific public commit
     (`125872fd1b19e948528177b6f6a2ac25c83dfd89`) of `testsprite-cli`,
     it documents explicit V3 advisory code paths where the requested
     target URL is not honored and a healing-disable request is not
     respected (Codex §6, citing `src/lib/v3-advisory.ts`,
     `src/lib/runs.types.ts`, `src/commands/test.ts` — `CODE_CONFIRMED`).
     Claude's study reached the same practical conclusion (`PARTIAL`,
     not a standalone Phase 2 gate) from public documentation alone,
     without finding this specific code-level confirmation.
   - BrowserStack: a documented, named self-healing risk (assertion
     repair instead of defect detection) — found independently by both
     studies (Claude §7/§16 citing an independent analysis; Codex §15
     citing the same class of risk against its own gating criteria).
   - Momentic: promising (repo-friendly YAML tests, runs inside the
     customer's network) but SHA-binding depth and a guaranteed
     read-only mode are unconfirmed in both studies — `UNKNOWN`, not a
     negative finding, in both.
   - Diffblue: both studies independently flag the same structural
     concern — its "unit regression tests… reflect the current behavior
     of your code" design principle is the same shape as the semantic
     self-healing pattern this project's invariants forbid, when pointed
     at pre-existing human-authored tests. Both classify it as usable
     only as a human-gated generator for genuinely new/legacy code, never
     as an autonomous gate.
4. **Both studies independently identified that this project's own
   internal evidence/SHA-binding guarantees had real gaps before they
   could safely host a mandatory QA gate at all.** Codex's audit is the
   one that named the four concrete technical findings (empty mandatory
   manifest, policy/manifest replay drift, no read-only/ref-invariance
   check, merge TOCTOU on work-branch head) — each independently reread
   and reproduced against the real code in this same session (see
   Slice 21.5, `ROADMAP.md`) before being trusted or fixed. This
   materially strengthens the case for `BUILD_INTERNAL_MINIMAL` as the
   immediate next step: the mandatory-gate primitives needed hardening
   regardless of which engine ends up behind them.
5. **Neither study recommends committing to a specific external vendor
   today.** Both explicitly defer that decision behind a POC.

## 3. Provider-by-provider arbitration

| Provider | Claude's status | Codex's status | Arbitration |
|---|---|---|---|
| **TestSprite** | `PARTIAL` — strongest SHA/diff-scoping evidence of the four, but read-only mode and job persistence unconfirmed, does not appear to run this project's own test suite | `PARTIAL`, but explicitly **blocked for the final gate on documented V3 target/healing gaps**, code-cited against a pinned commit | **Direct challenger, currently non-admissible as final gate.** Codex's code-level citation is the stronger, more specific evidence — adopted as the operative status. A POC is only worth running **after** documentation or a vendor attestation confirms these V3 gaps are resolved; re-discovering an already-documented blocker is not a useful spike. |
| **Momentic** | `PARTIAL` — best privacy/repo-friendliness story of the three web generalists (YAML tests in-repo, runs in customer's own network); SHA-binding/read-only depth unconfirmed | `PARTIAL` — repo-centric web challenger, SHA/recovery/policies still to qualify | **Serious repo-centric challenger; replacement POC candidate if TestSprite stays blocked.** Both studies agree it is `PARTIAL` for the same underlying reason (unconfirmed SHA/read-only depth), not a disagreement — kept as the second POC candidate specifically because its position (customer-network execution, versioned YAML tests) is architecturally the best fit for this project's SHA-bound, repo-durable evidence model if TestSprite's blockers are not lifted. |
| **BrowserStack** | `PARTIAL` — strongest real browser/device/visual E2E evidence and the only one that runs *existing* Selenium/Playwright suites, but self-healing risk disqualifies it as sole gate | `PARTIAL` against the same gating criteria, same self-healing concern | **Browser/mobile/device specialist, not a general QA gate.** Both agree. Held in reserve, to be activated specifically when a real target project needs browser/device/visual coverage — never as this project's own default gate, since `ai-dev-orchestrator` itself is a Python CLI/library with no browser surface today. |
| **Diffblue Cover** | `NO` (general) / `CONDITIONAL` (Java-only, human-gated) | `NO` (general), same structural self-healing-shaped concern | **Java-unit specialist only, strictly human-gated.** Full agreement. Never a general engine; useful only inside a future `MULTI_ENGINE_BY_STACK` scenario for a Java target project, and only with every generated/regenerated assertion routed through human review before being trusted as regression protection. |

## 4. Confidence levels

- **HIGH** — no external candidate passes all elimination gates today. Both
  studies reach this independently, via non-overlapping evidence chains
  (Claude via public docs, Codex via public docs *and* pinned-commit code
  reading). This is the single most decision-relevant fact in both reports
  and it is not in dispute.
- **HIGH** — `BUILD_INTERNAL_MINIMAL` is the correct immediate next step,
  regardless of which PRIMARY label each report chose. Codex's PRIMARY
  literally is `BUILD_INTERNAL`; Claude's `HYBRID` PRIMARY, read past the
  label, also starts with "build the small, engine-independent governance
  core… as the mandatory internal Phase 2 gate" (Claude §37) before any
  external engine contributes anything — i.e. Claude's own recommended
  *first build step* is the same internal core Codex recommends outright.
  The apparent disagreement is about where to draw the Slice boundary
  (Claude folds "optional external complement, later" into the same
  headline label; Codex keeps it out of the headline label and defers it
  explicitly) — not about what to build first.
- **MEDIUM** — TestSprite's specific V3 target/healing blocker as the
  reason it's inadmissible today. Codex's citation is code-level and
  commit-pinned, which is stronger evidence than Claude's public-docs-only
  research reached — but this document has not independently re-fetched
  and re-verified that commit's exact file contents this session (the
  category of finding is adopted; the byte-for-byte code detail is
  Codex-reported, matching this project's own established standard for
  citing a source without re-verifying it live — see
  `docs/OMNIROUTE_ARBITRATION.md` §"Process note" for the same convention
  used previously).
- **MEDIUM** — Momentic as the designated second/replacement POC candidate
  over BrowserStack or Diffblue for that role. Both studies rate it
  `PARTIAL` for genuinely unconfirmed (not negative) reasons; the
  designation as "second POC" is this arbitration's own synthesis of an
  architectural-fit argument (repo-durable YAML, customer-network
  execution) that neither individual report stated as a ranked
  recommendation in those exact terms — flagged here as synthesis, not as
  a fact either study independently confirmed.
- **LOW-MEDIUM** — the exact numeric scorecard gap (4 points vs. 1 point)
  between PRIMARY and SECOND_BEST in each report. As stated in §1, this is
  not treated as evidence of anything by this arbitration — explicitly not
  used to break the tie, per the user's instruction.

## 5. Final arbitration

| Element | Decision |
|---|---|
| **TARGET ARCHITECTURE** | `HYBRID-READY` — the QA core stays engine-independent by contract (`QAEngine`/`QARequest`/`QAResult`, Slice 22); `ExternalQAEngine` is a first-class extension point from day one, never a later bolt-on requiring a contract rewrite. |
| **IMMEDIATE IMPLEMENTATION** | `BUILD_INTERNAL_MINIMAL` — `InternalQAEngine` is the first (and, for now, only) concrete engine built. No external provider is required, or approved, for the initial mandatory gate. |
| **Why not `HYBRID` as the immediate implementation** | Both studies agree no external candidate is admissible as a final-gate contributor *today* — a Hybrid architecture with no qualified external leg to plug in is, in practice, `BUILD_INTERNAL` wearing a different label. Building the internal engine first, behind a contract that stays Hybrid-ready, captures Claude's own recommended first step without prematurely committing engineering effort to an external adapter for a candidate (TestSprite) with a documented, unresolved blocker. |
| **Why not `BUILD_INTERNAL` (closed, no external ever)** | External providers bring genuinely specialized capabilities this project has no reason to reconstruct from scratch — real browser/device farms and visual regression (BrowserStack), a mature Java unit-generation engine (Diffblue), and a repo-native, customer-network-executed web E2E model (Momentic) that fits this project's evidence philosophy unusually well. Closing the door architecturally would guarantee a future rewrite the moment any of those becomes genuinely needed. |
| **Slice 23 (decided, not started this session)** | `InternalQAEngine` MVP, minimal, **Python/pytest first** — reusing `QualityGateRunner`/`AdaptiveExecutionSelector`/the Slice 21.5 hardening primitives, never a second implementation of what already exists. |
| **External providers** | Remain **future optional adapters** behind the `QAEngine` contract. None approved today as a mandatory final gate. TestSprite/Momentic are the two POC-worthy candidates, if and when a POC is actually run (not this session) — TestSprite first, contingent on its documented V3 blockers being independently confirmed resolved; Momentic as the designated fallback/second candidate. BrowserStack and Diffblue are held in reserve for their specialized domains (browser/mobile/visual; Java units), to be activated only when a real target project's need is established — never speculatively. |
| **POC policy** | No POC executed this or a prior session. At most one baseline (`InternalQAEngine`) and one external challenger POC, per both studies' own recommendation (Claude §32, Codex §30) — conditional, not scheduled here. |

## Process note

Per the same standard already used for `docs/OMNIROUTE_ARBITRATION.md`:
this document does not re-run either study's underlying research. It reads
both reports as given, cross-checks them against each other and against
this project's own real code (independently reread this session for the
Slice 21.5 hardening, see `ROADMAP.md`), and arbitrates on facts and
evidence quality — never on the numeric scorecards, and never by treating
"2 conclusions agree" as proof by headcount where the underlying evidence
does not actually establish the same thing.
