# QA Governance (Slice 22)

Source of truth for the behavior implemented in
`src/orchestrator/qa.py`, `src/orchestrator/qa_knowledge.py`, and
`src/orchestrator/qa_protection.py`. This document is descriptive, not
aspirational — everything below is implemented and tested
(`tests/test_qa.py`, `tests/test_qa_knowledge.py`,
`tests/test_qa_protection.py`).

Plays the same role for Slice 22 that `docs/GIT_GOVERNANCE.md` plays for
Slice 20.

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

## What was deliberately left out of this slice

- No `InternalQAEngine`, `TestSpriteQAEngine`, `MomenticQAEngine`,
  `BrowserStackQAEngine`, or `DiffblueQAEngine` — Slice 23 (decided:
  `InternalQAEngine` MVP, Python/pytest first) and beyond.
- No wiring into `MVPManager`'s development → review → merge cycle at
  all — Slice 24.
- No change to `compute_merge_eligibility` or `ReleaseManager` to require
  `QAVerdict.PASS` — also Slice 24. `QAVerdict` already carries every
  fact that integration will need; nothing here is connected to it yet.
- No `QAEngineSelector` — Slice 22 defines capabilities and contracts
  only; real selection logic is Slice 23+.
- No `RealizationReport` extension — evaluated and deferred; it would
  have meaningfully grown this slice's scope for no behavior this slice
  needs to prove. A future, genuinely opt-in extension (mirroring how
  Slice 20/21.5 extended it) remains straightforward later.
- No full human-approval workflow for `TestChangeAuthorization` — only
  the contract (`ExpectedChangeSource` + justification + timestamp).
- No AST/dependency-graph/semantic Test Impact Analysis — only the
  minimal, directly-derivable-from-`.qa/` prefix-matching analyzer.
