# QA Governance (Slice 22 + Slice 23)

Source of truth for the behavior implemented in
`src/orchestrator/qa.py`, `src/orchestrator/qa_knowledge.py`,
`src/orchestrator/qa_protection.py` (Slice 22 — provider-independent
contracts/persistence/knowledge base), and
`src/orchestrator/internal_qa_engine.py` (Slice 23 — the first real
engine, Python/pytest only). This document is descriptive, not
aspirational — everything below is implemented and tested
(`tests/test_qa.py`, `tests/test_qa_knowledge.py`,
`tests/test_qa_protection.py`, `tests/test_internal_qa_engine.py`).

Plays the same role for Slice 22/23 that `docs/GIT_GOVERNANCE.md` plays
for Slice 20.

## Why this exists

Slice 21's arbitration (`docs/QA_BUILD_VS_ADOPT_ARBITRATION.md`) decided
`HYBRID-READY` as the target architecture and `BUILD_INTERNAL_MINIMAL` as
the immediate implementation: build a small, engine-independent QA
governance core first, keep it ready for an external engine to plug in
later, but require no external provider for the initial mandatory gate.
Slice 22 builds exactly that core — contracts, persistence, and a
deterministic governance decision — and nothing else. No `InternalQAEngine`
(Slice 23), no wiring into `MVPManager`'s development/review/merge cycle,
no change to `compute_merge_eligibility`/`ReleaseManager` (both Slice 24).

## QAResult vs QAVerdict — the central separation

`QAResult` is what a `QAEngine` *observed* — tests it selected/added/ran,
pass/fail/skip counts, regressions, coverage gaps, and (informationally
only) whatever status the engine itself claims
(`QAResult.engine_reported_status`).

`QAVerdict` is this project's own governance decision — `PASS`, `FAIL`,
or `INCONCLUSIVE` — computed exclusively by
`orchestrator.qa.evaluate_qa_verdict`, a pure, deterministic function of
already-persisted facts. It **never** reads `engine_reported_status`. An
external engine claiming "PASS" with the wrong SHA, empty mandatory
evidence, an unresolved regression, or an unauthorized protected-test
change still produces a governed verdict other than `PASS` —
`tests/test_qa.py::TestEngineResultNeverAuthoritative` proves this
directly, with two fake engines disagreeing about their own status while
`evaluate_qa_verdict` reaches the same governed conclusion for identical
underlying evidence either way.

`evaluate_qa_verdict` mirrors `git_governance.compute_merge_eligibility`'s
shape exactly: it takes `run`/`result` plus a handful of caller-supplied
boolean facts (`unauthorized_protected_change`, `read_only_violation`,
`read_only_unprovable`) and never queries a store or the filesystem
itself.

## QA phases

`QAPhase.TEST_AUTHORING`: may add/modify tests, fixtures, or knowledge
files per policy; must never touch production code. May change HEAD — a
Phase 1 commit means quality gates and review then apply to the new head,
exactly as any other commit would (Slice 20's existing SHA-binding, no
new mechanism needed).

`QAPhase.FINAL_VERIFICATION`: must be read-only. `evaluate_qa_verdict`'s
rule is precise: a **policy violation** (a mutation was actually
observed during a declared-read-only run) is `FAIL`; an **infrastructure
inability to even prove read-only** (e.g. the workspace isn't a git
repository, so there's no SHA to compare) is `INCONCLUSIVE`. Neither case
ever produces `PASS`.

Detection reuses Slice 21.5's `validation.py` primitives verbatim —
`orchestrator.qa.run_final_verification_gate` calls
`QualityGateRunner.run_gate(require_nonempty_mandatory_manifest=True,
verify_repository_unchanged=True)` and translates a caught
`ReadOnlyValidationViolationError` into `read_only_violation=True`, or a
`None` captured git SHA into `read_only_unprovable=True`. This is a real
reuse against a real temporary git repository in
`tests/test_qa.py::TestReadOnlyFinalVerificationRealGitRepo` — never a
second implementation of read-only detection.

## Persistence — QARunStore

`QARunStore` (sqlite3) persists one mutable current-state row per
`QARun` plus an insert-only audit event log — the exact same shape as
`GitWorkItemStore` (Slice 20). After a cold restart, the store alone
answers: what was requested (`project_id`/`mvp_id`/`work_item_id`,
`engine_id`, `expected_base_sha`/`expected_head_sha`), with what policy
and evidence manifest (both snapshotted — see below), whether the run
finished (`QARunStatus`), and what verdict was produced
(`QAVerdict`, recorded at most once).

`QARunStatus` (`CREATED`/`RUNNING`/`COMPLETED`/`FAILED`/`INTERRUPTED`) is
strictly the *execution* status — distinct from `QAVerdictStatus`
(`PASS`/`FAIL`/`INCONCLUSIVE`), the *governance* decision. A technically
`FAILED` or `INTERRUPTED` run can lead to an `INCONCLUSIVE` verdict, but
never an automatic `PASS` — see
`tests/test_qa.py::TestInfrastructureFailureInconclusive`.

Both `QARunStore.record_result` and `QARunStore.record_verdict` are
insert-only for a given run: a second call raises
`ResultAlreadyRecordedError`/`VerdictAlreadyRecordedError` — a run's own
historical facts are never silently replaced, the same insert-only
philosophy used throughout this project (`ExecutionRecord`,
`ExecutionRecommendation`, `AdaptiveExecutionDecision`, ...).

## Policy/manifest snapshot

Exactly Slice 21.5's `ValidationStore.record_manifest`/
`get_manifest_for_run` pattern, generalized to QA: `QARun.policy`
(`QAPolicy`) and `QARun.manifest` (`QAEvidenceManifest`) are captured at
run-creation time and stored as part of the run's own row — never
recomputed from a possibly-since-changed *live* policy on replay. A
policy change made after a run completed can never retroactively flip
what that run's own verdict meant.

## Evidence manifest — the empty-mandatory-evidence invariant

`QAEvidenceManifest` describes what MUST be verified for a run (required
test ids, required invariant ids, required engines, required test
levels). `QAPolicy.require_nonempty_evidence` (default `True`) is the
same invariant Slice 21.5 established for `QualityGateRunner`
(`require_nonempty_mandatory_manifest`): a manifest with nothing
mandatory in it can never produce `PASS` when this flag is set — "no
mandatory checks were even configured" must never be indistinguishable
from "all mandatory checks passed".

## Wrong SHA

`QAPolicy.require_exact_sha` (default `True`): `evaluate_qa_verdict`
rejects a `QAResult.observed_head_sha` that doesn't exactly match
`QARun.expected_head_sha` — `PASS` on SHA A never authorizes anything for
SHA B, the same discipline `compute_merge_eligibility` already applies to
quality-gate/review evidence (Slice 20).

## Protected test baseline

`orchestrator.qa_protection` answers one question: "did a protected
file's content change since a captured baseline?" — via deterministic
SHA-256 hashing (`hash_file`), never guessing semantics. The protected
path set is always caller-supplied (from a manifest, policy, or the
knowledge base's regression map) — this module never hardcodes a
`"tests/"` convention.

`ProtectedTestChange.is_unauthorized` is `True` only when a change was
detected **and** no `TestChangeAuthorization` was supplied for that path.
A brand-new file outside the captured baseline is never inspected at
all — "new unprotected test" is not a violation by construction, not by
a special case (`tests/test_qa_protection.py::TestNewUnprotectedTestNotViolation`).

`TestChangeAuthorization` requires an `ExpectedChangeSource`
(`ACCEPTANCE_CRITERIA`/`SPECIFICATION`/`ROADMAP_DECISION`/
`ARCHITECTURE_DECISION`/`HUMAN_APPROVAL`) and a non-empty justification —
this slice implements only the contract, not a full human-approval
workflow. `has_unauthorized_change(...)` is the direct input to
`evaluate_qa_verdict`'s `unauthorized_protected_change` kwarg — an
unauthorized protected-test mutation always produces `FAIL`, never
`PASS`, regardless of what any engine reports.

## Regression knowledge base — Git vs SQLite boundary

`orchestrator.qa_knowledge` reads/writes **only** `.qa/*.yaml` in the
*target* repository — durable product knowledge that travels with the
project. It has no `sqlite3` import at all. `orchestrator.qa`'s
`QARunStore` is the opposite: orchestrator runtime/audit state, never
product knowledge. The two are never mixed.

Four files, each optional — a project with no `.qa/` directory (or a
missing individual file) remains fully valid, yielding an empty section,
never an error, and nothing here ever auto-creates a file on read:

- `.qa/invariants.yaml` — `invariant_id`/`description`/`criticality`/
  `source`/`introduced_by_work_item`/`related_tests`/`active`. No id
  prefix (e.g. `ORCH-*`, `AIDO-*`) is required by the loader — any prefix
  a project uses is its own convention.
- `.qa/regression-map.yaml` — `id`/`paths`/`related_tests`/
  `invariant_ids`. Links functional/technical paths to what must be
  re-run when they change.
- `.qa/critical-paths.yaml` — `id`/`paths`/`required_tests`/
  `required_invariant_ids`. Paths considered critical enough to impose
  specific required coverage whenever touched.
- `.qa/known-flaky.yaml` — `test_id`/`evidence`/`suspected_cause`/
  `first_seen_at`/`status`/`retry_policy`. **Never** a skip list: no
  field on `KnownFlakyEntry` can be read as "skip this" or "treat as
  passed", and nothing anywhere in this project converts a `FAIL` into a
  `PASS` because a test is listed here. A critical flaky test stays fully
  visible.

All I/O is `yaml.safe_load`/`yaml.safe_dump`, UTF-8, atomic
(`tempfile.mkstemp` + `os.replace`, the same pattern used by
`RoadmapApplicationService`/`RealizationReportService` elsewhere in this
project — never a partially-written file observable on disk).

This repository seeds its own small, real `.qa/invariants.yaml`
(`AIDO-QA-001..004`) — but only for invariants already demonstrated by
existing tests (SHA-bound evidence, author≠reviewer, no silent quality
downgrade, the Slice 21.5 merge-head-drift fix). This is not a migration
of the full test suite into a regression map — that remains explicitly
out of scope for this slice.

## Test Impact Analysis — deterministic only

`TestImpactRequest`/`TestImpactResult` are a provider-independent
contract: pluggable from a future internal engine, external engine, or
(what this slice actually implements)
`orchestrator.qa_knowledge.analyze_test_impact_deterministic` — changed
file path prefix-matched against `regression-map`/`critical-paths`
entries, nothing more. No AST parsing, no multi-language dependency
graph, no semantic analysis — an unrelated change (no path prefix
matches) invents nothing: an empty result, never a guess. Deeper impact
analysis is explicitly future work (Slice 23+ or an external engine's own
capability).

## Engine independence — proven, not just asserted

`QAEngine` is a bare `Protocol` (`def run(self, request: QARequest) ->
QAResult`). `QAEngineCapabilities` describes what an engine can do
(test levels, stacks, `can_author_tests`, `can_execute_existing_tests`,
`supports_read_only`, `supports_resume`, `supports_external_artifacts`) —
never who it is; no vendor name appears in any contract field anywhere in
this module (`tests/test_qa.py::TestNoProviderSpecificSchema`).

`tests/test_qa.py::TestEngineIndependence` runs two structurally
different fake engines (one shaped like a future `InternalQAEngine`, one
shaped like a future external adapter) through the exact same
`evaluate_qa_verdict` and gets the same governed conclusion for
equivalent evidence — concrete proof, not just a documentation claim,
that `QARun`/`QAVerdict`/`QAPolicy`/the knowledge base never need to
change to add a new engine.

## What Slice 22 deliberately left out (now addressed by Slice 23)

- ~~No `InternalQAEngine`~~ — built in Slice 23, see below.
  `TestSpriteQAEngine`/`MomenticQAEngine`/`BrowserStackQAEngine`/
  `DiffblueQAEngine` remain unbuilt, per the Slice 21 arbitration
  (`docs/QA_BUILD_VS_ADOPT_ARBITRATION.md`: `HYBRID-READY` architecture,
  `BUILD_INTERNAL_MINIMAL` immediate implementation).
- ~~No `RealizationReport` extension~~ — added in Slice 23 (opt-in
  `qa_run_store`, mirrors the Slice 20/21.5 pattern exactly).
- No `QAEngineSelector` — Slice 23 has exactly one engine
  (`InternalQAEngine`); real multi-engine selection logic remains
  future work once a second engine exists.
- No full human-approval workflow for `TestChangeAuthorization` — only
  the contract (`ExpectedChangeSource` + justification + timestamp).
- No AST/dependency-graph/semantic Test Impact Analysis — only the
  minimal, directly-derivable-from-`.qa/` prefix-matching analyzer.

## What Slice 23 deliberately still leaves out (Slice 24)

- No wiring into `MVPManager`'s development → review → merge cycle at
  all — confirmed unmodified.
- No change to `compute_merge_eligibility` or `ReleaseManager` to require
  `QAVerdict.PASS` — confirmed unmodified. `QAVerdict` already carries
  every fact that integration will need.
- No automatic QA-FAIL → coding-agent → rework loop — `InternalQAEngine`
  only ever returns `requires_coding_agent=True` with evidence; it never
  launches a coding agent itself.

## Slice 23 — InternalQAEngine (Python/pytest MVP)

`src/orchestrator/internal_qa_engine.py` is the first real `QAEngine`
implementation (Slice 22's `Protocol`, satisfied by a synchronous
`run()` that internally wraps its own async pipeline). Honestly scoped:
Python/pytest only — a project with none of `pyproject.toml`/
`pytest.ini`/`setup.cfg`/`tox.ini` is an unsupported stack, and the
engine never attempts a command for it (the run resolves to
`INCONCLUSIVE`, never `PASS`). No JavaScript/Java/mobile/browser/
Playwright/BrowserStack/TestSprite/Momentic claim anywhere.

### Composition, never reimplementation

| Piece | Reused from | Never |
|---|---|---|
| Test execution | `validation.QualityGateRunner` — a targeted pytest command is just one more `ValidationCommand`, run under a synthetic, per-run `project_id` so it never mutates a project's durable config | A second subprocess test runner |
| Deterministic test selection | `qa_knowledge.analyze_test_impact_deterministic` (Slice 22) | A new impact analyzer |
| Read-only Final Verification | `qa.run_final_verification_gate` (Slice 21.5's `verify_repository_unchanged`) | A second read-only detector |
| Protected-test enforcement | `qa_protection` (Slice 22) | A second baseline mechanism |
| Governed verdict | `qa.evaluate_qa_verdict` (Slice 22) | The engine computing PASS/FAIL/INCONCLUSIVE itself |
| QA Test Authoring worker selection/execution | `ComplexityEstimationRequest` → `ExecutionRecommendationService` → `AdaptiveExecutionSelector` → `RalphExecutionEngine` (Slice 16/17/19) | A second worker-selection or execution mechanism |

### `InternalQAPlan` and test selection

`build_plan()` loads `.qa/` (via `load_qa_knowledge_base`), runs the
deterministic Test Impact analyzer, and produces an `InternalQAPlan`
(impacted areas, selected tests, required invariants, targeted/
regression `ValidationCommand`s, rationale) — included in evidence, not
a hidden intermediate.

Known-flaky selected tests (`.qa/known-flaky.yaml`, with a
`retry_policy`) get their **own** `ValidationCommand`, never bundled
into the main targeted-pytest invocation — a bundled failure can't be
attributed to one specific test without parsing pytest's own output,
which this MVP deliberately does not do. `_run_with_flaky_retry` then
re-runs just that command, bounded by the retry budget parsed from
`retry_policy` (never infinite); every attempt — pass or fail — is a
real, separately persisted `QualityGateRunner` run, and the full attempt
history is recorded in `QAResult.risks`, never silently dropped. A test
that fails every attempt stays a real failure despite being listed in
`known-flaky.yaml` — that file is never a skip list.

Two-phase strategy: `TEST_AUTHORING` runs the targeted command only
(fast feedback); `FINAL_VERIFICATION` runs the targeted command **and**
the project's full configured regression suite (regression confidence).
If nothing is selected and no regression command is configured, the
engine raises `NoEvidenceAvailableError` — the caller (`run_qa_cycle`)
turns this into a `FAILED` run, which `evaluate_qa_verdict` already maps
to `INCONCLUSIVE` — never a `COMPLETED` run with an empty manifest that
would otherwise be forced to `FAIL`.

### Failure classification

Deterministic-first (`classify_validation_status`): `TIMEOUT`/`ERROR` →
`ENVIRONMENT_FAILURE`; a plain pytest failure stays `UNKNOWN` without
more context — never guessed into `REGRESSION`/`TEST_DEFECT`/
`EXPECTED_CHANGE` from a bare exit code. Conservative, and explicitly
acceptable.

### QA Test Authoring

`InternalQATestAuthor.select_worker` requires `developer_worker_id`
exclusion (forwarded as `author_worker_id` to the existing adaptive
selector — the same governance `WorkerSelector` already enforces for
review independence, never reimplemented) whenever a real development
author exists; it is `None` only for a standalone QA analysis with no
author at all (the Part R smoke). A distinct `reviewer_worker_id` is
only *preferred*: tried first via `excluded_worker_ids`, and — only if
that leaves zero eligible candidates — retried without excluding the
reviewer, so a two-worker configuration is never blocked over a soft
preference. `config/workers.yaml` gained a `qa_testing` capability on
both `alice` and `victor` — no dedicated worker fabricated.

`run_authoring` executes a real `ExecutionRequest` via
`RalphExecutionEngine` with a `qa.authoring.start`/
`qa.authoring.completed`/`qa.authoring.failed` application-level event
(never a reserved Ralph topic). The payload is parsed strictly
(`parse_qa_authoring_event`) — absent or malformed fails closed
(`InvalidQAAuthoringEventError`), unlike `review.parse_findings`'s
lenient raw-text fallback. The worker's own claim is **never** trusted
alone: `verify_authoring_git_facts` independently diffs the workspace
against `base_sha` and flags any file outside `tests/`/`fixtures/`/
`.qa/` **and** outside pytest's own `test_*.py`/`*_test.py` filename
convention (a real smoke run against `~/projects/ralph-spike` — a flat
repo with no `tests/` directory — found this gap directly) **and**
outside recognized runtime noise (`.ralph/`, `__pycache__`,
`.pytest_cache`, matched as a path segment at any depth — the same real
smoke run found a nested `__pycache__/*.pyc` wrongly flagged). Any
remaining flagged file is an `AuthoringViolationError` condition — fail
closed, no auto-repair. A protected existing test's mutation, detected
via the Slice 22 baseline, is blocked unless a valid
`TestChangeAuthorization` is supplied; a brand-new test is always
allowed.

### `RealizationReport` extension (opt-in)

`RealizationReportService` gained an optional `qa_run_store` parameter
(same pattern as Slice 20/21.5's `git_work_item_store`). When supplied,
the latest `QARun` for a WorkItem surfaces its engine/phase/verdict/
expected-vs-observed SHA/counts/regressions in the report summary, and
its events (`qa_run_created`, `qa_verdict_recorded`, …) appear in the
timeline. Omitted entirely (`qa_run_id is None`) when no store is
configured — fully backward compatible, verified against the existing
Slice 18/20/21.5 test suite. HTML stays a pure, deterministic projection
— never re-parsed as a source of truth anywhere in this codebase.

### Real validation

`scripts/smoke_internal_qa_real.py` (manual, never run by pytest): a
real `qa_testing`-capable worker, chosen by the real adaptive mechanism
(never hardcoded), analyzes a disposable copy of `~/projects/ralph-spike`
(whose `review_candidate.py::add()` has a known, pre-existing bug),
adds a real regression test proving it (RED, real pytest execution),
never touches production code, and the governed verdict comes back
`FAIL` + `requires_coding_agent=True` — proving the engine never treats
"a real bug exists and is now provably covered" as a reason to fabricate
a `PASS`. Run 2026-09-15, result: **PASS**, with `alice`
(anthropic/claude_code/sonnet). `~/projects/ralph-spike` verified
byte-identical before/after.

`scripts/self_dogfood_dev_qa_real.py` (manual, never run by pytest): a
full disposable copy of **this repository itself**, a real controlled
defect (`classify_validation_status(TIMEOUT)` deliberately mapped to
the wrong classification), a confirmed real negative control (RED before
any fix), a real DEVELOPMENT worker that genuinely fixes the defect
without touching any test file, then a real, *distinct* QA Test
Authoring worker selection — enforced via the same mandatory
author-exclusion described above. Run 2026-09-15: the development phase
completed successfully and for real (`alice`, haiku); the QA phase
correctly and honestly reported `BLOCKED_BY_PROVIDER` (only `anthropic`
was available this session — `openai`/Codex was quota-exhausted, so no
second, distinct `qa_testing` worker existed) rather than falling back
to the same worker as the developer. The control plane
(`~/projects/ai-dev-orchestrator`) was verified byte-identical before
and after (`git rev-parse HEAD` and `git status --short` both
unchanged) — no historical report was generated for this run, since the
scenario never reached that point. This is the accepted, non-failing
outcome the task brief explicitly sanctions for a missing provider —
full self-dogfood acceptance (`QAVerdict.PASS` end-to-end plus the
negative control) remains to be re-run once a second real provider is
available.

Both real smoke scripts independently surfaced and led to fixing two
production-code gaps in `verify_authoring_git_facts` (the `tests/`-only
prefix assumption, and the root-level `__pycache__` prefix-only match) —
concrete evidence that the "reproduce, don't fake" discipline this
project applies throughout (Slice 21.5, the OmniRoute/QA audits) extends
to its own real-worker smoke tests too.
