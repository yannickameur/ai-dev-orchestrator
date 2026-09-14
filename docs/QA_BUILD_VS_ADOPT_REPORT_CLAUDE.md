# QA Build-vs-Adopt Report — Claude (Slice 21)

Independent architecture study, written before any comparison with a
Codex audit (none was consulted, none was searched for, per explicit
instruction for this session). Documentation/research only — no code
under `src/`/`tests/`/`config/`, no dependency installed, no external
service actually invoked, zero reset credit consumed beyond this
session's own reasoning/research.

Date: 2026-09-13. Author: Claude (this session), independent of any
Codex QA study.

---

## 1. Executive summary

`ai-dev-orchestrator` needs a QA/regression-testing layer that sits
between "Independent Code Review" and "Merge Eligibility" (Slice 20) and
answers, with executable proof, whether a change is correct and
regression-free. This report audits five options — an internal QA agent,
four external products (TestSprite, BrowserStack AI Agents, Momentic,
Diffblue Cover), and two composite architectures (Hybrid,
Multi-engine-by-stack) — against 13 non-negotiable invariants (§4) and a
weighted 100-point scorecard (§30).

**No external candidate cleanly passes every elimination gate (§31) to
become the sole, mandatory final QA gate today.** TestSprite and Momentic
are the most promising general-purpose candidates (diff/commit-aware,
CI-integrable, repo-friendly) but neither has a *documented* strict
read-only verification mode or a fully confirmed base_sha/head_sha
binding contract — both are currently `UNKNOWN`, not `NO`, and both
deserve a POC (§32). BrowserStack is the clear leader for real
browser/device/visual E2E but its self-healing agent has a **documented,
named risk of repairing assertions instead of flagging a real defect**
(§16) — unacceptable as the *sole* final gate without a strict
technical-only self-heal policy. Diffblue Cover is Java-only and its
core design principle — tests "reflect the current behavior of your
code" (regenerated on every change) — is structurally close to the
*semantic self-healing* pattern this project explicitly forbids (§17);
it is usable only as a human-reviewed, gated component, never
autonomously as a final gate.

**Primary recommendation: `HYBRID`** (score 80/100, §30) — build the
small, engine-independent governance core that is already ~70% present
in this codebase (`QualityGateRunner`, `ExecutionStore`, `GitGovernance`,
Slice 20's SHA-binding), use it with our own `pytest`/lint/mypy suite as
the mandatory internal Phase 2 gate (§9 of `docs/QA_STRATEGY.md`), and
treat one external engine (TestSprite first, per its diff/SHA-scoping
evidence) as an **optional Phase 1 complement** for target projects with
a real web surface — never as the sole authority for PASS. **Second
best: `BUILD_INTERNAL`** alone (76/100) — deferring external QA entirely
remains fully viable since most of the mandatory internal core already
exists. See §37 for the full decision block.

---

## 2. Scope and method

- Internal base: full re-read of `README.md`, `ROADMAP.md`,
  `docs/status.md`, `docs/QA_STRATEGY.md`, `docs/ADAPTIVE_EXECUTION.md`,
  `docs/GIT_GOVERNANCE.md`, `docs/SPIKE_RALPH.md`, plus source inspection
  of `validation.py`, `review.py`, `mvp_manager.py`, `git_governance.py`,
  `realization_report.py`, `release_manager.py`, `worker_selector.py`,
  `adaptive_execution.py`, `execution_store.py`, `handoff.py`,
  `recovery.py`, `wait.py`, `complexity_estimation.py` (class inventories
  only — this report never proposes rebuilding something already
  present).
- External research: `WebSearch`/`WebFetch` against official
  documentation, npm/GitHub packages, official pricing pages, and the
  vendors' own blogs where no other source existed. Independent
  secondary sources (Pure AI, SiliconANGLE, Bug0, Sacra) used only to
  corroborate, never as the primary basis for a technical capability
  claim.
- Every capability claim below carries a classification:
  `CODE_CONFIRMED` (verified in a real repo/package), `DOC_CONFIRMED`
  (stated in official docs), `MARKETING_ONLY` (stated only on a
  marketing/landing page, unconfirmed elsewhere), `NOT_FOUND` (actively
  searched for, absent), `UNKNOWN` (not enough public information either
  way — never defaulted to a favorable assumption).
- **No account was created, no API key was requested, no MCP server was
  connected, no test was actually executed against any of these
  products.** All findings are from public documentation as of
  2026-09-13.
- **No Codex QA report was searched for or read** before this document
  was finalized, per explicit instruction.

## 3. Current orchestrator capabilities (do not rebuild)

Already built and tested (876/876 passing) — any QA architecture must
reuse these, never duplicate them:

| Component | Role | Reuse for QA |
|---|---|---|
| `QualityGateRunner`/`ValidationStore` (`validation.py`) | Runs deterministic commands (pytest, lint, etc.), persists `ValidationResult`/`QualityGateResult` keyed to a git SHA | This **is** the executable-evidence backbone (invariant §4.1) — a QA engine's "PASS" must ultimately reduce to gate results here, never a parallel mechanism |
| `ReviewStore`/`ReviewPolicy` (`review.py`) | Independent human/AI code review, author≠reviewer enforced | QA is a distinct concept from code review — never merged into one record type |
| `GitGovernanceService`/`GitWorkItemStore` (`git_governance.py`) | Governed branch, immutable `base_sha`, tracked `current_head_sha`, `MergeEligibilityResult`, ff-only merge | The exact SHA-binding mechanism QA must extend (§8 of `docs/QA_STRATEGY.md`) — never reimplemented |
| `WorkerSelector`/`AdaptiveExecutionSelector` (`worker_selector.py`, `adaptive_execution.py`) | Capability/governance/quota-based worker choice, `QualityTier`-adaptive profile resolution | Reusable **only** for an `InternalQAEngine`'s own LLM-driven steps (test authoring) — never for external-engine selection, which is capability/stack-based (§3.3 `QA_STRATEGY.md`) |
| `ExecutionStore`/`RecoveryCoordinator` (`execution_store.py`, `recovery.py`) | Durable execution audit, cold-restart recovery | Pattern to reuse for a future `QARun`/`QAResultStore` — never a new parallel recovery mechanism |
| `RealizationReport` (`realization_report.py`) | Deterministic, insert-only, non-LLM WorkItem projection | Extension point for QA facts (§16 `QA_STRATEGY.md`), not a new report type |
| `ReleaseManager` (`release_manager.py`) | MVP-level release checks, incl. `governed-work-items-merged` (Slice 20) | Extension point for a future `qa-verdict-pass` check |

**Conclusion**: the "internal minimum differentiator" (§27) is not a
green-field build — the durable-evidence, SHA-binding, and audit
substrate already exists. What's actually missing is QA-specific:
`QAEngine`/`QARequest`/`QAResult`, `FailureClassification`, Test Impact
Analysis, and the `.qa/` knowledge base.

## 4. QA invariants (evaluation baseline)

The 13 invariants from the user's brief, unmodified, used verbatim as
the evaluation grid for every candidate in §6-10:

1. PASS relies on real test execution. 2. An LLM verdict alone can never
produce PASS. 3. Existing regression tests are protected. 4. No existing
test can be automatically weakened/deleted/skipped/semantically
rewritten/adjusted to accept new behavior just to go green. 5. An
expected test change requires versioned justification. 6. QA PASS is
bound to the exact HEAD SHA. 7. PASS on SHA A never authorizes merging
SHA B. 8. QA FAIL is persistent and auditable. 9. INCONCLUSIVE != PASS.
10. External QA unavailable != PASS. 11. No silent fallback to a weaker
QA engine. 12. Final QA Verification must be able to be READ-ONLY. 13.
Durable regression knowledge must travel with the repository.

## 5. Candidates

TestSprite, BrowserStack AI Agents, Momentic, Diffblue Cover, Internal
QA Agent, Hybrid, Multi-engine-by-stack. No `OTHER_RELEVANT_CANDIDATE`
was added — none surfaced during research as objectively more relevant
than these seven within the time available; this is a finding of this
session's research depth, not a claim that no such product exists.

## 6. TestSprite audit

**Version/freshness**: no version number published; "Spring Release
(May 2026)" and CLI "now live as open source on GitHub" referenced in
2026 blog content — `DOC_CONFIRMED` recency, exact semver `UNKNOWN`.
SaaS (cloud sandbox execution), Node.js ≥22 for the MCP server
(`@testsprite/testsprite-mcp` on npm). [TestSprite MCP npm package](https://www.npmjs.com/package/@testsprite/testsprite-mcp), [MCP for AI Coding Agents](https://www.testsprite.com/solutions/mcp), [Are there MCP servers for software testing?](https://www.testsprite.com/blog/are-there-mcp-servers-for-software-testing).

**Summary**: an AI testing agent that generates a PRD/test-plan/test-code
(Playwright/Cypress-style) from a running application or diff, executes
in TestSprite's own cloud sandbox, and reports back — via MCP (IDE-native
use) or a GitHub Actions workflow triggered on PR events. It explicitly
supports a `testScope` parameter of `"codebase"` or `"diff"`, and
placeholders `{pr}`/`{branch}`/`{sha}`/`{short-sha}` resolved per run —
`DOC_CONFIRMED`. [GitHub PR Testing](https://www.testsprite.com/blog/github-pr-testing-the-missing-step-in-every-ai-development-workflow), [Does TestSprite Support GitHub Actions for Pull Request Testing?](https://www.testsprite.com/blog/does-testsprite-support-github-actions-for-pull-request-testing).

**MCP tools** (`DOC_CONFIRMED` via `docs.testsprite.com/mcp/core/tools`):
8 tools — `testsprite_bootstrap`, `_generate_code_summary`,
`_generate_standardized_prd`, `_generate_frontend_test_plan`,
`_generate_backend_test_plan`, `_generate_code_and_execute` (the only
one that actually starts a run), `_open_test_result_dashboard`,
`_check_account_info`. **No polling/cancel tool, no documented
`run_id`/job-persistence contract** — `NOT_FOUND` in the tools
reference. This matters directly for invariant §12 (forceable read-only
run) and for a future `RecoveryCoordinator` integration (job
persistence): both currently `UNKNOWN`/weak.

**Test execution**: generates and runs *its own* tests against a
*deployed, running application* — it does not appear to execute this
repository's existing `pytest` suite; no evidence found either way for
arbitrary-existing-test execution — `NOT_FOUND` (searched specifically,
absent from all fetched docs). **This is a material gap against
invariant §1**: TestSprite's own generated tests are real executable
evidence, but they are not proof that *our* existing 876-test suite
still passes — a PASS from TestSprite alone does not satisfy this
project's actual regression-protection need without also running our own
suite (which `QualityGateRunner` already does).

**Existing-test preservation / self-healing**: no update/repair/heal/
delete-obsolete-test feature documented for pre-existing, non-TestSprite
tests — `NOT_FOUND`. Classified `SAFE_WITH_POLICY` provisionally (it
appears to generate new tests, not touch existing ones) but this is
`UNKNOWN` rather than positively confirmed, since no explicit "we never
modify your existing tests" statement was found.

**Security/privacy** (`docs.testsprite.com`/privacy policy,
`DOC_CONFIRMED`): TestSprite states it interacts with the *running
application*, not the source code — "your private code doesn't have to
go anywhere." [Is TestSprite Safe to Use with Private Codebases?](https://www.testsprite.com/blog/is-testsprite-safe-to-use-with-private-codebases-or-internal-applications). Privacy policy: single
subprocessor **AWS**; all personal-data processing in the **US**, SCCs
for EEA/UK transfers; retains "service data" for the duration of the
business relationship. **No explicit statement on whether code/test
artifacts are used for model training** — `UNKNOWN`. [TestSprite Privacy Policy](https://www.testsprite.com/privacy).

**Pricing** (`DOC_CONFIRMED`, `testsprite.com/pricing` via search):
credit-based — Free ($0, 150 credits), Starter ($19/mo, 400 credits),
Standard ($69/mo, 1,600 credits), Enterprise (custom). [TestSprite Pricing 2026](https://bug0.com/knowledge-base/testsprite-pricing).

**Direct-QAEngine feasibility**: **PARTIAL**. Strong on diff/SHA
scoping and PR-native workflow (the most concrete SHA-binding evidence
of any candidate studied); weak/unconfirmed on read-only forcing, job
persistence, and — critically — does not run this project's own test
suite. Best positioned today as a **Phase 1 (test authoring/E2E
complement)** engine, not a standalone Phase 2 gate.

## 7. BrowserStack audit

**Version/freshness**: AI Agents suite launched publicly ~June 2025,
Self-Healing Agent added ~November 2025 (`DOC_CONFIRMED` via press
releases); actively documented through 2026. [BrowserStack Launches Suite of AI Agents](https://www.browserstack.com/press/browserstack-launches-suite-of-ai-agents-to-redefine-software-quality-at-scale), [BrowserStack Launches AI Self-Healing Agent](https://pureai.com/blogs/the-pure-ai-blog/2025/11/browserstack-launches-ai-self-healing-agent.aspx). SaaS
(device/browser cloud farm); MCP server (20 tools, `DOC_CONFIRMED`).
[BrowserStack MCP tools & workflows](https://www.browserstack.com/docs/browserstack-mcp-server/tools).

**Summary**: an established real-device/browser cloud (Automate, App
Automate, Percy visual, Accessibility, Low-Code Automation) now layered
with AI agents: **Test Case Generator** (PRD/user-story → test cases,
claims >90% authoring-time reduction), **Self-Healing Agent** (repairs
broken locators live during execution), **Test Failure Analysis Agent**
(log root-causing, claims up to 95% faster debugging) — all
`DOC_CONFIRMED` but efficiency percentages are vendor-stated,
`MARKETING_ONLY` for the specific numbers. [Comparing the Best AI Testing Tools in 2026](https://www.browserstack.com/guide/ai-testing-tool).

**Self-healing risk — the most important finding for this candidate**:
independent analysis explicitly documents the exact failure mode this
project's invariants forbid — *"without clear rules, an agent might see
contradictory values and 'repair' the assertion instead of recognising a
product defect."* [Self-Healing Tests with AI: Triage Before Repair](https://www.awesome-testing.com/2026/07/self-healing-tests-with-ai) — `DOC_CONFIRMED`
as a documented, named risk (not merely a hypothetical this report
invented). BrowserStack's own docs describe self-heal as locator-repair
only (`DOC_CONFIRMED`, [Self healing in Low Code Automation](https://www.browserstack.com/docs/low-code-automation/test-recording/browserstack-ai/ai-self-heal)), but the
independent source above shows the *general* self-healing-agent pattern
(which BrowserStack's Self-Healing Agent explicitly extends toward
failure *repair*, not just locator repair) can drift into assertion
territory without an explicit guardrail. **Classification:
`UNSAFE_FOR_FINAL_GATE` without a strict, verified,
locator-only-self-heal policy switch — not found as a guaranteed,
enforced configuration option in the docs reviewed** (`UNKNOWN` whether
BrowserStack exposes a hard "never touch assertions" toggle).

**Test execution / languages**: real browsers (3,000+ browser/OS
combinations, `DOC_CONFIRMED`), real mobile devices, Playwright/Selenium/
Cypress integration for *existing* customer test suites (this is a
genuine plus — BrowserStack Automate runs *your* Selenium/Playwright
tests, not only generated ones) — `DOC_CONFIRMED`. No backend
unit/integration/contract test generation found — `NOT_FOUND`.

**Security/privacy**: enterprise SaaS; no SOC2/ISO/data-residency
statement was found in the sources reviewed for this report —
`UNKNOWN` (not negative, simply unverified here; BrowserStack likely
publishes this on a dedicated trust page not reached in this session's
research).

**Pricing**: **not public** for AI Agents / enterprise tiers —
`UNKNOWN`, consistent with the MCP server itself being free but
requiring a paid BrowserStack plan underneath.

**Direct-QAEngine feasibility**: **PARTIAL**. The strongest single
candidate for real browser/mobile/visual E2E with genuine execution
evidence (video, logs, real devices), and the only one that runs
*existing* Selenium/Playwright suites rather than only generated ones —
but the self-healing risk (§4 invariant) and the absence of unit/backend
coverage make it unsuitable as the sole general QA gate; strong as a
**specialized E2E/browser component** in a Hybrid or
Multi-engine-by-stack architecture, with self-heal locked to
locator-level only.

## 8. Momentic audit

**Version/freshness**: actively documented through 2026 (`momentic.ai/
docs`); no semver found — `UNKNOWN`. Hybrid: local CLI + cloud dashboard.
[Momentic Features 2026](https://bug0.com/knowledge-base/momentic-features), [Welcome to Momentic](https://momentic.ai/docs).

**Summary**: web + iOS/Android testing platform, natural-language tests
compiled to **readable YAML stored in the target repository** — a
meaningfully different posture from TestSprite/BrowserStack's
cloud-sandbox model. The same CLI runs "on your laptop, in CI, or in a
cloud sandbox," explicitly positioned to run inside the customer's own
network — no tunnel, no IP allowlist — which is a genuine privacy/
architecture advantage. [Test Automation Infrastructure](https://momentic.ai/infrastructure). Positioned as
"repository-friendly agentic QA... closes coverage gaps around pull
requests and commits" — `DOC_CONFIRMED` PR/commit-awareness, but the
*exact* base_sha/head_sha binding contract (vs. just "PR-aware") was not
found in the fetched documentation excerpts — `UNKNOWN`, flagged for
POC.

**CI/CD**: GitHub Actions, CircleCI, GitLab, Jenkins, plus REST API with
cursor-based pagination for pulling run results by date/status/platform
— `DOC_CONFIRMED`. No explicit run_id/polling contract text was
retrieved verbatim, but the REST API description strongly implies one —
`DOC_CONFIRMED` at a coarse level, exact shape `UNKNOWN`.

**Self-healing**: "self-healing locators and intent-based checks
automatically adapt to DOM changes" — `DOC_CONFIRMED`, and the framing
(locators + intent-based *checks*, not raw pixel/value assertions)
suggests a more conservative, technical-level design than BrowserStack's
framing, but no explicit "never touches assertion values" guarantee was
found — `PARTIAL`/`UNKNOWN`, not `SAFE_WITH_POLICY` outright without
deeper documentation review (recommended as part of the POC, §32).

**Security/privacy**: "separate test and production environments,
redact secrets, use scoped accounts, define retention and access
controls" — `DOC_CONFIRMED` at a policy-description level; specific
data-residency/subprocessor list `NOT_FOUND` in this session's research.

**Pricing** (`DOC_CONFIRMED`): credit-based — Free tier 2,000 credits/
month; $125/month for 10,000 credits (pay-as-you-go); Enterprise custom;
one credit = one executed test step; no per-seat fee. [Momentic Pricing 2026](https://bug0.com/knowledge-base/momentic-pricing).

**Direct-QAEngine feasibility**: **PARTIAL**, but the *most promising*
of the three general-purpose web/E2E externals for this specific
project's philosophy — versioned YAML tests in the repo align naturally
with §13 of `docs/QA_STRATEGY.md` (durable knowledge travels with the
repo), and the "runs in your network" positioning is the strongest
privacy story of the three. Held to `PARTIAL` rather than `YES` purely
because SHA-binding and a guaranteed read-only mode were not found
explicitly documented — genuinely `UNKNOWN`, not a negative finding.

## 9. Diffblue audit

**Version/freshness**: "Cover" product, actively documented
(`cover-docs.diffblue.com`), four editions (Community/Developer/Teams/
Enterprise) as of 2026 pricing pages — `DOC_CONFIRMED`. CLI + CI + IDE
agent; on-prem/self-hosted posture implied by CLI-first design but not
explicitly confirmed — `UNKNOWN`.

**Summary**: fully autonomous generator of **Java JUnit unit tests**
only — `DOC_CONFIRMED`, 94% claimed generation-accuracy rate
(`MARKETING_ONLY` for the specific number). [Diffblue Cover](https://www.diffblue.com/diffblue-cover/).

**The central finding for this candidate**: Diffblue's own documentation
states its "unit regression tests" **"reflect the current behavior of
your code"** and are updated as code changes — `DOC_CONFIRMED`, this is
the product's stated design principle, not a bug report. [Introducing Unit Regression Tests](https://www.diffblue.com/resources/introducing-unit-regression-tests-a-new-type-of-test-created-by-diffblue-cover/), [Uplifting Test Coverage Out of the Box](https://www.diffblue.com/resources/uplift-java-test-coverage-out-of-the-box/).
**This is structurally the same shape as the SEMANTIC_SELF_HEALING
pattern this project's invariants forbid** (§5.1 of `QA_STRATEGY.md`:
"expected=100, new result=80 → test changed to accept 80") — except
Diffblue applies it by design to characterize *new or changed* code
(useful, legitimate for onboarding legacy code with zero tests) rather
than to silently paper over a regression in code that already had
human-authored expectations. **Classification: `UNSAFE_FOR_FINAL_GATE`
for any test Diffblue can regenerate without a human reviewing the
assertion diff** — it must never be pointed at this project's own
human-authored regression suite; it is only safe as a bounded,
human-gated *generator* for genuinely new/legacy Java code with no prior
test, and its generated-test diffs must go through the same test-
protection review as any other test change (§5 `QA_STRATEGY.md`).

**Scope**: Java only, unit-level only — no integration/API/E2E
(`NOT_FOUND`, and this report does not evaluate it as a universal
engine, per the user's explicit instruction).

**Pricing** (`DOC_CONFIRMED`): free Community Edition; Developer Edition
from ~$1,500 (5,000 net-new lines of coverage, ≈$0.30/line); Teams/
Enterprise quote-based. [Diffblue Cover Software Pricing](https://www.capterra.com/p/214366/Diffblue-Cover/).

**Direct-QAEngine feasibility**: **NO** as a general engine (single
language, unit-only, and the characterization-test default behavior is
incompatible with unattended final-gate use). **CONDITIONAL** as a
specialized Java-unit-coverage *generator* inside `MULTI_ENGINE_BY_STACK`
— only if every generated/regenerated assertion is routed through human
review before being trusted as regression protection.

## 10. Internal QA assessment

Honest self-assessment, per §3: `QualityGateRunner` already provides
real executable evidence (invariant §1), SHA-binding already exists via
`GitGovernanceService`/`ValidationStore` (invariant §6-7), and
`ExecutionStore`/audit patterns already provide persistent, auditable
FAIL records (invariant §8). What is genuinely **not** built: Test
Impact Analysis (diff → affected-test selection), any browser/mobile/
visual E2E capability, AI-driven test authoring for coverage gaps, and
the `.qa/` knowledge base. Building E2E/browser capability from zero
in-house is a multi-month undertaking with no current evidence this
project needs it for *itself* (a Python CLI/library, not a web app) —
though target projects the orchestrator governs for other users could
need it. An `InternalQAEngine`, if built, composes with the existing
adaptive mechanism (`role=qa_testing`, Slice 16/17/19 pattern) purely
for its own LLM-driven steps (test authoring, failure triage) — the
actual PASS/FAIL still comes from `QualityGateRunner`, never an LLM
verdict.

## 11. Capability matrix

| Axis | TestSprite | BrowserStack | Momentic | Diffblue | Internal |
|---|---|---|---|---|---|
| Test Impact Analysis | PARTIAL (diff testScope) | NOT_FOUND | PARTIAL (PR/commit-aware) | NOT_FOUND | NATIVE (buildable on existing `git_governance.py`/SHA facts) |
| Test generation | Strong (frontend/backend/E2E) | Moderate (PRD→cases) | Strong (NL→YAML E2E) | Strong (Java unit only) | Buildable, none yet |
| Runs existing repo tests | NOT_FOUND | YES (Selenium/Playwright) | Likely (CLI runs your tests too) — UNKNOWN detail | YES (JUnit, but regenerates) | YES (pytest, already built) |
| Existing-test protection | UNKNOWN (no evidence either way) | UNSAFE (documented assertion-repair risk) | PARTIAL/UNKNOWN | UNSAFE by design | Full (native to our own policy) |
| SHA-binding | Best evidenced ({sha}/{short-sha}, testScope) | UNKNOWN | UNKNOWN (PR/commit-aware, not confirmed exact) | UNKNOWN | Full (Slice 20, built) |
| Read-only final-verification mode | UNKNOWN | UNKNOWN | UNKNOWN | NO (writes test files) | YES (buildable, gates are already read-only) |
| E2E/browser/mobile | Strong (web) | Strongest (device farm) | Strong (web+mobile) | None | None |
| Languages | Web/API-centric | Web/mobile | Web/mobile | Java only | Whatever target repo uses |

## 12. Test Impact Analysis

TestSprite: `testScope=diff|codebase` is the most concrete evidence of
diff-awareness among externals — `DOC_CONFIRMED` but coarse (whole-diff
scope, not symbol/dependency-level impact analysis). Momentic:
PR/commit-aware positioning, mechanism unconfirmed — `PARTIAL`.
BrowserStack/Diffblue: no diff-impact mechanism found — `NOT_FOUND`.
None of the four does true dependency-graph impact analysis (changed
file → changed symbol → affected capability → recommended regression
scope, §6 `QA_STRATEGY.md`) — this remains a genuine internal-build gap
regardless of engine choice.

## 13. Test generation

TestSprite and Momentic both generate full test code from
specs/natural-language; Diffblue generates Java unit tests exclusively;
BrowserStack generates test *cases* from PRDs (a lighter authoring aid,
not full executable test code by default). **Generation vs. execution
must not be conflated**: generation quality is not evidence of PASS —
only real execution is (invariant §1) — and only TestSprite, BrowserStack
(via existing suites), and Diffblue were confirmed to actually *execute*
what they generate.

## 14. Test execution

Real execution confirmed (`DOC_CONFIRMED`) for all four, but via
different mechanisms: TestSprite (own cloud sandbox, Playwright/Cypress-
style, against a *deployed app*), BrowserStack (real browser/device
farm, runs both generated and pre-existing suites), Momentic (real
browser, local or CI network), Diffblue (real JVM, JUnit). None was
confirmed to invoke `pytest` directly on this project's own suite — for
a Python-backend project like `ai-dev-orchestrator` itself, none of the
four is a drop-in replacement for `QualityGateRunner`; they are
complements for stacks/surfaces the internal gate cannot cover (E2E,
browser, mobile).

## 15. Existing-test protection

This is where every external candidate is weakest relative to this
project's actual invariants. No candidate publishes an explicit,
verifiable "we never modify your pre-existing human-authored tests"
guarantee. Ranked from least to most concerning: TestSprite/Momentic
(no evidence of touching pre-existing tests, `UNKNOWN` but no documented
risk either) < BrowserStack (documented, named assertion-repair risk,
mitigable only by policy discipline this project cannot verify is
enforced) < Diffblue (regeneration-to-current-behavior is the *stated
design*, the highest-risk pattern of the four).

## 16. Self-healing

See §7 for the sourced BrowserStack finding (independent analysis names
the exact failure mode this project's invariant forbids) and §9 for
Diffblue's structurally similar characterization-test behavior.
Classification per candidate: TestSprite `UNKNOWN` (no self-heal feature
found — may simply not apply), BrowserStack `UNSAFE_FOR_FINAL_GATE`
(unless locator-only mode is verified/enforced), Momentic `PARTIAL`
(locator+intent framing suggests lower risk, unconfirmed), Diffblue
`UNSAFE_FOR_FINAL_GATE` (by design, for any pre-existing human test).

## 17. Failure analysis

BrowserStack's Test Failure Analysis Agent is the most explicitly
documented failure-analysis feature (log root-causing, `DOC_CONFIRMED`
existence, `MARKETING_ONLY` for the "95% faster" figure). TestSprite
reports include logs/screenshots/videos per its dashboard description.
Momentic and Diffblue: no dedicated failure-classification feature found
— `NOT_FOUND`. **None of the four maps failures into this project's
`REGRESSION`/`EXPECTED_CHANGE`/`TEST_DEFECT`/`FLAKY_TEST`/
`ENVIRONMENT_FAILURE`/`UNKNOWN` taxonomy** (§4 `QA_STRATEGY.md`) — this
classification step is a genuine internal-build requirement regardless
of engine choice, likely consuming the raw evidence each engine
produces.

## 18. Flakiness

Momentic's cloud dashboard is documented to include "test quarantining"
— `DOC_CONFIRMED`, the only candidate with an explicit flaky-management
feature found. No detail retrieved on retry policy or historical-
failure-rate tracking for any candidate — `UNKNOWN` across the board.
Quarantine-never-means-ignore-forever (§8 `QA_STRATEGY.md`) is not
verifiable as enforced by any external tool from public docs; if
adopted, this project's own `.qa/known-flaky.yaml` (§15 `QA_STRATEGY.md`)
should remain the authoritative record regardless of what a vendor
dashboard shows.

## 19. SHA/audit

TestSprite is the only candidate with concrete, named SHA placeholders
in its own documentation (`{sha}`, `{short-sha}`) — `DOC_CONFIRMED`.
Momentic's PR/commit-awareness is `DOC_CONFIRMED` at a coarse level.
BrowserStack and Diffblue: no SHA-binding contract found —
`NOT_FOUND`/`UNKNOWN`. **None of the four was confirmed to expose a
result object carrying `base_sha`+`head_sha` together** (only a single
commit/PR reference) — this is the single most important open question
for a POC (§32): can a candidate's result be trusted to say "this
verdict is for exactly this diff, not just the latest branch state"?

## 20. Read-only verification

**No candidate publishes a documented, forceable "observe/analyze/
report only, touch nothing" run mode.** All four are architected around
either generating artifacts (tests, PRDs) or self-healing (which by
definition writes). This is classified `UNKNOWN` (not `NO`) for all
four, because the absence of a *documented* feature does not prove the
underlying API can't be constrained to read-only use (e.g., "generate
tests" could simply be skipped, running only a previously-authored
Phase-1 suite in Phase 2) — but no vendor claims this explicitly, so it
cannot be assumed and must be verified by POC before any candidate is
trusted for Phase 2 (§7.1 `QA_STRATEGY.md`).

## 21. Autonomous/headless

TestSprite (MCP + GitHub Actions, no human-in-the-loop required for a
run once configured — `DOC_CONFIRMED`), BrowserStack (MCP, 20 tools,
CI/CD-integrable — `DOC_CONFIRMED`), Momentic (CLI identical on laptop/
CI, GitHub Actions/CircleCI/GitLab/Jenkins — `DOC_CONFIRMED`, the
broadest CI matrix of the four) all pass this bar in principle. Diffblue
(CLI + CI-pipeline integration — `DOC_CONFIRMED`) also passes for its
narrow Java scope. **None of the four is UI-only** — all are
automatable, satisfying the baseline autonomy requirement, though
polling/cancel/resume completeness is `UNKNOWN` or `NOT_FOUND` for all
except a coarse REST pull for Momentic.

## 22. Persistence/recovery

The weakest area across all four for this project's specific future
need (`RecoveryCoordinator`-style resumption after our own process
restarts). TestSprite: no run_id/job-persistence contract found in its
MCP tools reference — `NOT_FOUND`. Momentic: REST API can list/filter
past runs by cursor — `DOC_CONFIRMED` at a coarse level, but whether a
specific in-flight run can be resumed/polled by ID was not confirmed —
`UNKNOWN`. BrowserStack/Diffblue: `UNKNOWN`. **This means any external
QA integration should assume "fire and eventually receive a webhook/
poll a listing," not "resume a specific run_id after our own crash," pending
a POC that proves otherwise.**

## 23. Security/privacy

Ranked from most to least documented privacy-favorable posture, with
explicit caveats: **Momentic** (runs inside the customer's own network,
no tunnel/allowlist — the strongest architectural privacy story,
`DOC_CONFIRMED`, though specific subprocessor/residency detail
`NOT_FOUND`) > **TestSprite** (interacts with the running app rather
than reading source, `DOC_CONFIRMED`; but data processed in the US only,
single subprocessor AWS, no model-training statement found — `UNKNOWN`)
> **BrowserStack** (`UNKNOWN` — no trust/security page reached in this
session's research; likely exists but not verified here) > **Diffblue**
(`UNKNOWN` — CLI-first design suggests less SaaS data flow, but no
explicit statement found). **No candidate should be treated as
privacy-cleared for a private/sensitive target repository without a
project-specific legal/security review** — this report supplies
evidence, not clearance.

## 24. Cost

TestSprite and Momentic both use transparent, public credit-based
pricing with real free tiers — cheapest to POC. BrowserStack: no public
AI-Agent-tier pricing found — likely bundled into existing paid plans,
`UNKNOWN`, probably the most expensive at scale (device-farm SaaS).
Diffblue: highest per-unit cost structure found (~$0.30/line, $1,500
minimum for the paid tier) but has a genuinely free Community Edition
for evaluation.

## 25. Vendor lock-in

Momentic (`LOW`): tests are plain, readable YAML committed to the target
repo — survives the vendor disappearing, satisfies §13 of
`QA_STRATEGY.md` (durable knowledge travels with the repo) almost by
default. Diffblue (`LOW`-`MEDIUM`): generates plain JUnit/Java source,
exportable, but its *maintenance* value (auto-regeneration) disappears
with the vendor. TestSprite (`MEDIUM`): generates Playwright/Cypress
code (portable format) but orchestration/PRD/plan artifacts are
proprietary-format, and the execution sandbox itself isn't portable.
BrowserStack (`MEDIUM`-`HIGH`): device-farm access and Percy visual
baselines are not portable at all if the subscription ends.

## 26. QA knowledge base compatibility

None of the four external candidates was found to read or write a
`.qa/`-style repo-local YAML knowledge base (`NOT_FOUND` across the
board) — this is exclusively an internal-build concern regardless of
engine choice. Momentic's own YAML-in-repo test storage is the closest
philosophical fit and could plausibly coexist with a `.qa/` directory
without friction; the other three keep their state SaaS-side by design,
requiring an internal `QAResultStore`/`.qa/qa-history` sync step to
extract durable facts out of them (a real, non-trivial integration cost
— classified `MEDIUM` normalization cost per §13).

## 27. Internal minimum differentiator

Regardless of engine choice, this project must still build: the
`QAEngine`/`QARequest`/`QAResult` contracts (§28); `qa.mode`/
`qa.preferred_engine`/fallback policy; SHA-binding extension of
`MergeEligibility` to require `QAVerdict.PASS` (§8 `QA_STRATEGY.md`);
`FailureClassification` normalization (no candidate provides this
taxonomy natively, §17); bounded QA/rework cycle logic (reusing
`ReviewPolicy.max_review_cycles`'s pattern); `RealizationReport`/
`ReleaseManager` extension; and the `.qa/` knowledge base. This list is
**unchanged from the premise in `docs/QA_STRATEGY.md`** — research
confirmed rather than corrected it, because no external candidate offers
any of these governance primitives natively.

## 28. Architecture A/B/C/D

**A — BUILD_INTERNAL**: benefits — full control, zero SaaS dependency,
zero data-leaves-network risk, reuses ~70% already-built substrate (§3).
Gaps — no E2E/browser/mobile without a large new build; test-generation
quality untested against TestSprite/Momentic's specialized models.
Engineering/maintenance cost: ongoing, ours alone.

**B — DIRECT_EXTERNAL**: `ai-dev-orchestrator` keeps governance
(WorkItem/SHA/policy/audit/merge) and delegates test impact/generation/
execution/E2E/failure-analysis to one external engine. Viable in
principle (§11.1 `QA_STRATEGY.md` criteria) but **no single candidate
passes all elimination gates today** (§31) — this architecture is not
recommended as a *sole* choice until a POC resolves the read-only-mode
and SHA-binding unknowns.

**C — HYBRID**: internal engine (reusing `QualityGateRunner`) as the
mandatory unit/integration/contract Phase 2 gate; external engine
(TestSprite first) as an **optional** Phase 1/E2E complement, feeding
its findings through the same `FailureClassification`/SHA-binding layer
before any weight is given to it. Highest score (§30) because it
combines what's already reliable (internal, invariant-compliant) with
what's genuinely missing (E2E) without making an unverified external
product the sole gate.

**D — MULTI_ENGINE_BY_STACK**: internal for Python/backend, an external
web-E2E engine, Diffblue for Java (gated per §9). Most flexible for a
multi-target-project future, but highest engineering/maintenance/vendor
complexity — only justified once ≥2 target-project stacks with real
divergent QA needs actually exist (currently speculative for this
repository).

## 29. Direct external-engine feasibility

Restated from §6-9: TestSprite `PARTIAL`, BrowserStack `PARTIAL`,
Momentic `PARTIAL`, Diffblue `NO` (general)/`CONDITIONAL` (Java-only,
human-gated). **No candidate is `YES`** — none can today fully replace
the need for the internal governance core, though several can
meaningfully reduce what that core has to build itself (test generation,
E2E execution).

## 30. Scorecard /100

Weights fixed per brief: Autonomous/headless 15, Test quality/coverage
15, Existing-test protection 10, SHA/auditability 10, Execution evidence
10, Languages/stacks 10, E2E/browser/mobile 10, Security/privacy 8, Cost
5, Maintainability 4, Vendor independence 3.

| Axis (weight) | INTERNAL | TESTSPRITE | BROWSERSTACK | MOMENTIC | DIFFBLUE | HYBRID | MULTI_ENGINE |
|---|---|---|---|---|---|---|---|
| Autonomous/headless (15) | 15 | 11 | 12 | 12 | 10 | 12 | 11 |
| Test quality/coverage (15) | 9 | 8 | 8 | 8 | 7 | 12 | 12 |
| Existing-test protection (10) | 10 | 5 | 4 | 6 | 2 | 9 | 8 |
| SHA/auditability (10) | 10 | 8 | 6 | 7 | 4 | 9 | 8 |
| Execution evidence (10) | 10 | 8 | 9 | 8 | 9 | 10 | 10 |
| Languages/stacks (10) | 5 | 5 | 5 | 5 | 3 | 7 | 9 |
| E2E/browser/mobile (10) | 1 | 7 | 10 | 6 | 0 | 8 | 8 |
| Security/privacy (8) | 8 | 5 | 4 | 6 | 4 | 6 | 5 |
| Cost (5) | 3 | 4 | 2 | 4 | 3 | 3 | 2 |
| Maintainability (4) | 2 | 3 | 3 | 3 | 3 | 2 | 1 |
| Vendor independence (3) | 3 | 2 | 1 | 2 | 3 | 2 | 1 |
| **Total /100** | **76** | **66** | **64** | **67** | **48** | **80** | **75** |

No `UNKNOWN` axis was scored at its maximum — every `UNKNOWN` finding
(e.g. security/privacy for BrowserStack/Diffblue, SHA-binding depth for
Momentic) was scored conservatively below the midpoint of the axis
rather than assumed favorable, per the explicit instruction.

## 31. Elimination gates

Minimal conditions to be the **mandatory, sole** final QA gate: (a)
headless/automatable; (b) real execution evidence; (c) reliable
SHA-binding (base+head, not just "latest branch"); (d) structured,
auditable result; (e) a verifiable read-only mode; (f) semantic
self-healing controllable/disableable; (g) acceptable privacy for the
target repo; (h) private-repo compatible.

| Candidate | Fails | Verdict |
|---|---|---|
| TestSprite | (c) partial-only, (e) unconfirmed, doesn't run existing suite | Complement, not sole gate |
| BrowserStack | (f) documented risk, (c) unconfirmed | Complement (E2E), not sole gate |
| Momentic | (c) unconfirmed, (e) unconfirmed | Complement, promising POC candidate |
| Diffblue | (f) fails by design for pre-existing tests | Human-gated Java generator only, never autonomous gate |
| Internal | passes all — by construction, since we control the contract | Can be the sole mandatory gate today |

A candidate failing a gate can still remain a valuable **complement**
(Phase 1 authoring, E2E-only Phase 2 addendum) — none is disqualified
from the architecture entirely, only from being the *sole* authority.

## 32. Recommended POCs (not executed)

**POC 1 — SHA-binding & read-only verification, TestSprite vs. Momentic
vs. internal baseline.** Same tiny reproducible scenario as the Slice
17/18 real smoke test (`add()` bug, TDD fix, `docs/reports/
real-cross-worker-resume-2026-09-13.html`): run each candidate against
base_sha A and head_sha B of the same fix, confirm (1) the result names
both SHAs unambiguously, (2) a "verify only, generate/write nothing" run
is actually achievable, (3) evidence (logs/artifacts) is retrievable
after the run completes without polling a specific run_id (given §22's
finding). This directly resolves the single largest open uncertainty in
this report and would change §29's verdicts from `PARTIAL` to `YES`/`NO`
for at least one candidate.

**POC 2 — BrowserStack self-heal boundary.** Deliberately introduce a
regression (change a UI-visible computed value, not a selector) in a
disposable demo app, run BrowserStack's Self-Healing Agent against it,
and confirm whether the run reports a genuine FAIL or silently
"repairs" the assertion. This directly tests the single most concrete,
independently-documented risk found in this study (§16) and would move
BrowserStack from `UNSAFE_FOR_FINAL_GATE` to either confirmed-safe (if a
real, enforced locator-only mode exists) or confirmed-unsafe.

Neither POC was run this session, per explicit instruction.

## 33. Recommended target architecture

**HYBRID** (§28-C): mandatory internal Phase 2 gate (reusing
`QualityGateRunner`, extended with `FailureClassification` and SHA-bound
`QAVerdict`), optional external Phase 1/E2E complement — TestSprite as
the first candidate to POC (best-evidenced SHA/diff-scoping), Momentic
as the close second (best privacy/repo-friendliness story, promising but
less-evidenced SHA-binding). BrowserStack held in reserve specifically
for future target projects with a real browser/device/visual surface,
gated to locator-only self-heal pending POC 2. Diffblue held in reserve
specifically for a future Java target project, always human-gated.

## 34. Slice 22 implications

Slice 22 should build the **engine-independent** governance layer only:
`QAEngine`/`QARequest`/`QAResult` contracts, `FailureClassification`,
the Test Impact Analysis contract (not full implementation if it's
large), test-protection policy enforcement, `QAResultStore`/SHA-binding
persistence, and the `.qa/` knowledge-base scaffold — **explicitly not
coupled to TestSprite/BrowserStack/Momentic/Diffblue**, so the Slice 21
recommendation above can be revisited after the POCs without touching
Slice 22's code.

## 35. Slice 23 recommendation — PENDING USER ARBITRATION

Proposed (not approved): `InternalQAEngine` first (fastest to build
given §3's existing substrate, satisfies invariants unconditionally),
with a `TestSpriteQAEngine` adapter as the second deliverable once POC 1
confirms feasibility. This is a recommendation only — **Slice 23's
actual scope must be decided by the user after reading this report,
independently repeating this study with Codex, and arbitrating any
disagreement**, exactly as instructed.

## 36. Risks

Committing to any single external vendor before POC 1/2 risks building
governance code around unverified SHA-binding/read-only assumptions that
later prove false. Conversely, deferring all external QA risks
under-investing in E2E coverage this project may need once it governs
web-facing target projects. Diffblue's characterization-test behavior,
if ever misapplied to a human-authored regression suite by a future
implementer unaware of this report's §9 finding, would directly violate
this project's central test-protection invariant — this risk should be
called out explicitly in any future Slice 23/24 implementation.

## 37. Final decision

**Primary: `HYBRID`.** **Second best: `BUILD_INTERNAL`** — remains
fully viable alone (76/100, second-highest score) since the mandatory
governance substrate is already ~70% built; choosing this would simply
mean deferring all external QA (E2E/browser) indefinitely rather than
gating it behind a POC. **`WHAT_WOULD_CHANGE_MY_MIND`**: (1) POC 1
confirming a candidate's read-only mode and SHA-binding are real and
reliable would justify promoting that candidate from "optional
complement" to "conditionally-trusted Phase 2 contributor" for its
specific domain (E2E); (2) discovering that `ai-dev-orchestrator` will
soon govern a target project with a genuine browser/device/visual-
regression surface would raise `MULTI_ENGINE_BY_STACK`'s priority; (3)
discovering that BrowserStack does publish a verified, enforced
locator-only self-heal toggle would remove its single biggest
disqualifying concern and could make `HYBRID` lean toward BrowserStack
over TestSprite for the E2E slot.

## 38. Sources

- [Are there MCP servers for software testing? — TestSprite](https://www.testsprite.com/blog/are-there-mcp-servers-for-software-testing)
- [@testsprite/testsprite-mcp — npm](https://www.npmjs.com/package/@testsprite/testsprite-mcp)
- [MCP for AI Coding Agents — TestSprite](https://www.testsprite.com/solutions/mcp)
- [MCP Tools References — TestSprite Documentation](https://docs.testsprite.com/mcp/core/tools)
- [API Keys & MCP Integration — TestSprite Documentation](https://docs.testsprite.com/web-portal/admin/api-keys)
- [GitHub PR Testing — TestSprite](https://www.testsprite.com/blog/github-pr-testing-the-missing-step-in-every-ai-development-workflow)
- [Does TestSprite Support GitHub Actions for Pull Request Testing? — TestSprite](https://www.testsprite.com/blog/does-testsprite-support-github-actions-for-pull-request-testing)
- [Is TestSprite Safe to Use with Private Codebases or Internal Applications? — TestSprite](https://www.testsprite.com/blog/is-testsprite-safe-to-use-with-private-codebases-or-internal-applications)
- [TestSprite Privacy Policy](https://www.testsprite.com/privacy)
- [TestSprite Pricing 2026 — Bug0](https://bug0.com/knowledge-base/testsprite-pricing)
- [BrowserStack Launches Suite of AI Agents to Redefine Software Quality at Scale](https://www.browserstack.com/press/browserstack-launches-suite-of-ai-agents-to-redefine-software-quality-at-scale)
- [BrowserStack Launches AI Self-Healing Agent — Pure AI](https://pureai.com/blogs/the-pure-ai-blog/2025/11/browserstack-launches-ai-self-healing-agent.aspx)
- [Comparing the Best AI Testing Tools in 2026 — BrowserStack](https://www.browserstack.com/guide/ai-testing-tool)
- [Self healing in Low Code Automation — BrowserStack Docs](https://www.browserstack.com/docs/low-code-automation/test-recording/browserstack-ai/ai-self-heal)
- [Self-Healing Tests with AI: Triage Before Repair — Awesome Testing](https://www.awesome-testing.com/2026/07/self-healing-tests-with-ai)
- [BrowserStack MCP tools & workflows](https://www.browserstack.com/docs/browserstack-mcp-server/tools)
- [Momentic Features 2026 — Bug0](https://bug0.com/knowledge-base/momentic-features)
- [Welcome to Momentic](https://momentic.ai/docs)
- [Test Automation Infrastructure — Momentic](https://momentic.ai/infrastructure)
- [Momentic Pricing 2026 — Bug0](https://bug0.com/knowledge-base/momentic-pricing)
- [Diffblue Cover](https://www.diffblue.com/diffblue-cover/)
- [Introducing Unit Regression Tests — Diffblue](https://www.diffblue.com/resources/introducing-unit-regression-tests-a-new-type-of-test-created-by-diffblue-cover/)
- [Uplifting Test Coverage Out of the Box with Diffblue Cover](https://www.diffblue.com/resources/uplift-java-test-coverage-out-of-the-box/)
- [Diffblue Cover Software Pricing — Capterra](https://www.capterra.com/p/214366/Diffblue-Cover/)
- `docs/QA_STRATEGY.md`, `docs/GIT_GOVERNANCE.md`, `docs/ADAPTIVE_EXECUTION.md`, `ROADMAP.md`, `docs/status.md` (this repository, internal sources)
