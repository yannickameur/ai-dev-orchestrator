# Git / PR / Merge Governance (Slice 20)

Source of truth for the behavior implemented in
`src/orchestrator/git_governance.py`. This document is descriptive, not
aspirational — everything below is implemented and tested
(`tests/test_git_governance.py`, `tests/test_mvp_manager_git_governance.py`).

## Why this exists

Before Slice 20, Ralph/a worker could commit anywhere the workspace
happened to be checked out, and nothing in this codebase knew whether a
WorkItem's code had ever actually reached `main`. Slice 20 makes the
orchestrator — not Ralph, not the worker — the owner of: which branch a
WorkItem's code lives on, its base/current SHA, whether it is safe to
merge, and whether it has actually been merged. Ralph remains the
execution engine; workers still work in Git (and may still auto-commit —
that mechanism is untouched) — they just never decide branch creation,
protection, merge timing, or mergeability themselves.

## Protected branch and the work-branch lifecycle

`GitGovernancePolicy.base_branch` (default `"main"`) is always a member of
`protected_branches`. A governed WorkItem's worker is never launched
directly on a protected branch.

`GitGovernanceService.prepare_work_item(...)`, called by `MVPManager`
right before the first DEVELOPMENT execution (and again, idempotently, on
every REWORK attempt and every WAIT/RECOVERY resume — always through the
same shared `_execute_work_item` call site):

1. Verifies the repository exists and the current branch is
   `base_branch`.
2. Verifies the working tree is clean of **tracked** changes
   (`require_clean_worktree`, default `True`) — fails closed
   (`DirtyWorkingTreeError`) rather than auto-stashing, resetting, or
   cleaning anything. Untracked files (build artifacts, not-yet-added new
   source) are reported but never block preparation and are never
   touched — blocking on them would make governance impractical against
   any real repository with a `.gitignore`.
3. Captures `base_sha` = the current tip of `base_branch`. This value is
   captured exactly once and never changes for the life of the
   `GitWorkItemRecord` — it answers "on what version did this work
   start?" for good.
4. Computes a deterministic branch name — `work/<sanitized-work-item-id>`
   (see `work_branch_name`/`sanitize_branch_component`) — creates it from
   `base_sha` if it does not already exist, and checks it out.

If a `GitWorkItemRecord` already exists for this WorkItem, `prepare_work_item`
never recreates the branch or re-captures `base_sha` — it reconciles the
existing record instead (see **Restart recovery** below) and just
re-checks-out the existing branch. This is what makes the branch stable
across rework, resumed waits, and resumed recoveries: same WorkItem, same
branch, always.

## SHA-bound evidence

Every quality-gate result (`ValidationStore`, already since Slice 8) and
every review verdict (`ReviewStore`, since Slice 9) already carries the
exact `git_sha` it was produced against. `compute_merge_eligibility` uses
that fact directly: a WorkItem's governed `current_head_sha` must match
**both** the latest gate's `git_sha` and the latest review's
`git_sha_reviewed` exactly. A new commit landing after either
(a rework cycle, for example) silently invalidates the old evidence —
there is no "it passed before, so it's probably still fine". Missing
evidence, evidence for the wrong SHA, a rejected review, or a required
gate that never passed are all `NOT_MERGEABLE`, explicitly, with a reason.

`compute_merge_eligibility` never queries a store itself — it takes
already-resolved facts as parameters. `MVPManager` supplies them from
whichever live objects it has in hand (the fresh path) or falls back to
`ValidationStore.latest_gate_result_for_work_item`/
`ReviewStore.latest_for_work_item` when it doesn't (a resumed-review
completion has no live `QualityGateResult` object, only durable state).
Either way the eligibility computation itself is a pure function of its
inputs — never an LLM call, never "probably fine".

**Slice 24 addition:** `compute_merge_eligibility` gained four additive,
optional keyword parameters — `qa_required`, `qa_passed`, `qa_git_sha`,
`qa_run_terminal` — following the exact same "pre-resolved facts only"
discipline as the gate/review parameters above. When a WorkItem has QA
enabled, `MVPManager` resolves the governed `QAVerdict` (never the raw
engine-reported `QAResult`) and passes its facts in; `qa_git_sha` must
equal `current_head_sha` exactly, or eligibility is `NOT_MERGEABLE` with
an explicit stale-evidence reason, mirroring how gate/review SHA binding
already works. `GitGovernanceService` still never imports the QA domain
and never calls a QA engine itself. Omitting all four keeps pre-Slice-24
behavior byte-for-byte. Full QA workflow detail:
`docs/QA_GOVERNANCE.md`.

## Merge strategy: fast-forward-only

The default (and, for this slice, only) merge strategy is fast-forward:

```
git switch <base_branch>
git merge --ff-only <work_branch>
```

This fits the current architecture directly: exactly one sequential
worker at a time produces a linear commit chain on its own branch, so a
clean fast-forward is always achievable *when nothing else touched
`base_branch` meanwhile*. Fast-forward-only, by construction:

- never creates an artificial merge commit,
- never rewrites history,
- preserves the exact commits a worker produced,
- is fully deterministic,
- and simply **refuses** when it can't be done cleanly — never auto-rebase,
  never force, never an automatic conflict resolution.

If `base_branch` has advanced in a way that is not a strict ancestor
relationship with the work branch's head (checked via
`git merge-base --is-ancestor`), `compute_merge_eligibility` marks the
governed WorkItem `CONFLICT` and returns `NOT_MERGEABLE` — resolving that
(rebase, re-planning, a manual merge) is a human/business decision this
slice deliberately leaves out of scope.

`auto_merge` defaults to `False`: a WorkItem that reaches eligibility
stops at `MERGE_READY` rather than automatically advancing a protected
branch, until governance itself has been trusted. The `True` path is
fully implemented and tested against real temporary repositories
(`TestAutoMergeTrueMergesInTempRepo`) — it is simply not the default.

### Head-drift hardening at merge time (Slice 21.5)

`merge_ff_only` merges by **branch name** — `git merge --ff-only
<work_branch>` always picks up whatever the branch's live tip is at the
moment it runs, not a pinned SHA. `compute_merge_eligibility` and
`merge()` are always two separate calls, so a window exists between them:
if the work branch advances again after eligibility was computed for SHA
H2 (a stray or concurrent execution lands a further commit at H3, still a
clean fast-forward child of H2), a naive `merge()` would silently fold in
H3 on the strength of gate/review evidence that only ever covered H2 —
`git` itself has no opinion here, since H3 is still perfectly
fast-forwardable from `base_branch`.

`GitGovernanceService.merge()` closes this window: before switching to
`base_branch` or merging anything, it re-reads the work branch's actual
current tip and requires it to equal `eligibility.head_sha` exactly. Any
mismatch raises `GitHeadDriftError` — fail-closed, no rebase, no reset, no
force, and `base_branch` is never touched. A `base_branch` that has
meanwhile diverged incompatibly is still caught separately, by
`merge_ff_only` itself re-validating ancestry at merge time (the existing
`MergeRefusedError`/`CONFLICT` path above) — no extra locking is
introduced, this is a minimal, fail-closed re-check, never a distributed
lock. See `tests/test_git_governance.py::TestMergeHeadDriftHardening`.

## Restart recovery

`GitGovernanceService.reconcile(...)` reconstructs state from
`GitWorkItemStore` **plus the real repository** — never from "whichever
branch happens to be checked out right now" (that is explicitly not
trusted as a source of truth, since a restarted process attaches to
whatever state a shell/CI/earlier run left behind).

- A `GitWorkItemRecord` in a terminal state (`MERGED`/`ABANDONED`) is
  returned unchanged — nothing left to reconcile.
- A missing work branch while the store still expects a non-terminal
  state raises `GitBranchMissingError` — fails closed, never silently
  recreated (recreating it would risk the wrong base).
- A HEAD that no longer matches what the store expects is only
  reconciled — the stored `current_head_sha` is updated — when the actual
  HEAD exactly matches a caller-supplied set of known, persisted
  `ExecutionRecord.git_sha_after` values for that WorkItem. Otherwise it
  raises `GitHeadDriftError` — no broad heuristic, no "close enough".

## Pull requests are informational only

`PullRequestPublisher` (abstract) + `GitHubCliPullRequestPublisher`
(wraps `gh pr create` with an explicit argv list, no GitHub SDK, no token
handling in this module — `gh` manages its own auth) let a caller
optionally publish a PR for a governed branch, recorded in the same
`GitWorkItemRecord` (`pull_request_number`/`pull_request_url`). A PR
existing **never** implies merge authorization — `compute_merge_eligibility`
and `merge` never consult a PR's existence or state, and this is covered
by an explicit test. `remote_pr_enabled` defaults to `False`; this slice
never calls `publish_pull_request` automatically from `MVPManager` — it
is available for a caller to invoke explicitly, whenever that becomes a
real need. No real PR was created and no real network call was made while
building or testing this slice — every PR test injects a fake subprocess
runner and asserts the exact argv.

## What was deliberately left out of this slice

- No remote push and no remote merge — the validated functional path is
  entirely local: a temporary repository, a governed branch, a local
  fast-forward merge.
- No generic multi-provider `Workspace` abstraction beyond
  `LocalGitWorkspace` — nothing here suggested one was needed yet.
- No second worktree manager — this slice operates on a single governed
  branch inside the same workspace `RalphExecutionEngine` already uses;
  Ralph's own parallel-loop worktrees (if ever used) are unrelated.
- No tags, GitHub Releases, changelog, or semantic versioning in
  `ReleaseManager` — it only gained one additional, opt-in check
  (`governed-work-items-merged`) so a release can't pass while governed
  code sits unmerged.
- No identity/author/committer hardcoding anywhere in this module — every
  git operation relies on the target repository's own configuration. The
  `yannickameur <yannick.ameur@gmail.com>` identity rule is this
  project's own contribution rule for commits to ai-dev-orchestrator
  itself; it has no bearing on what this module does to a governed
  target repository.
