# Morpion Web 3D — computer-turn regression (2026-09-18)

Untracked report. Left uncommitted for user review, per instruction.
**Status at time of writing: NOT YET FIXED.** wi-9 (first corrective
attempt) reached a genuine terminal `FAILED` status before QA ever ran;
wi-10 (second attempt, real, unforced) is durably `WAITING` because all
three real providers are genuinely unavailable right now. Session
stopped cleanly here on explicit instruction — no further worker was
launched, no manual workaround was applied.

**UPDATE (§28, later same day): FIXED AND MERGED.** wi-10's runtime
state was confirmed lost for good (§27, `DURABLE_RECOVERY_FAILURE`). A
new governed WorkItem, wi-11, was created from a persistent state root
outside `/tmp`, validated the preserved wi-9 candidate fix (never
merged/reopened/cherry-picked directly), reproduced it under normal
governance, passed real deterministic QA including a browser regression
test, and was merged to `main` and tagged. See §28 for full evidence.
wi-9 remains `FAILED` (untouched); wi-10 remains
`LOST_RUNTIME_STATE_AFTER_REBOOT` (untouched) — neither historical fact
is rewritten by this update.

## 1. Observed manual symptom

User-reported, screenshot-backed: page loads, 3×3 grid displays, human
plays a valid X, status changes to "L'ordinateur joue", the computer
never plays an O, the application stays stuck in that state
indefinitely.

## 2. Broken SHA

`edbc57612b4885359f2edec089238a6d08bc09cc` (Morpion `main`, unchanged
throughout this entire investigation — verified again at the very end,
see §23).

## 3. Exact reproduction steps

1. `cd ~/projects/morpion-web-3d && python3 -m http.server 8123`
2. Open the served page in a real browser (Chromium via Playwright,
   reusing the existing install at `/tmp/node_modules` — not a new
   framework, the same one used in WI-7).
3. Click the first empty cell (human plays X, index 0).
4. Wait ≥2s (well beyond the app's own 400ms computer-move delay).

## 4. Browser console evidence

Captured by the operator, before wi-9 was even created, against
`edbc576`:

```
BEFORE human move: board=["","","","","","","","",""] status="À vous de jouer"
RIGHT AFTER human move: board=["X","","","","","","","",""] status="L'ordinateur joue"
AFTER 2s delay: board=["X","","","","","","","",""] status="L'ordinateur joue" oCount=0
CONSOLE MESSAGES: []
PAGE ERRORS: []
BUG_REPRODUCED: true
```

Zero console/page errors — the failure is a **silent logic guard**, not
a JS exception. This ruled out an uncaught-error hypothesis before any
code change was made.

## 5. Root cause

`assets/js/main.js`'s `scheduleComputerMove()` captured
`thisGeneration` from `computerMoveGeneration` (a counter incremented on
**every** call to `scheduleComputerMove`, i.e. every computer turn) but
compared it, inside the `setTimeout` callback, against
`currentGameGeneration` (a **different** counter, incremented only in
`startNewGame()`, i.e. only on an explicit "Nouvelle partie" click).
These two counters are never the same value on a fresh page load: after
the very first human move, `computerMoveGeneration` becomes `1` while
`currentGameGeneration` is still `0`. The guard
(`thisGeneration !== currentGameGeneration`) is therefore true on the
very first computer turn of every game, unconditionally cancelling the
computer's move and leaving `boardLocked`/the status stuck. This holds
for every game, every difficulty, every time — not an intermittent race.

Independently confirmed twice: by the operator's own code inspection
(before wi-9 existed) and, separately, by DEV A (`alice`) during wi-9,
who reached the identical conclusion without being told the answer (the
WorkItem's acceptance criteria listed diagnostic leads only, never a
stated root cause).

## 6. Commit that introduced the regression

`cc179fcb5fac5e7d463b51cd5e40f0b5b186c882` — "Fix: corrige race
condition entre nouvelle partie et coup ordinateur" (WI-8,
`alice`/anthropic, 2026-09-18 11:06:51+02:00). This commit introduced
both counters and the flawed comparison in the same change,
well-intentioned (protecting against a real race between "Nouvelle
partie" and a pending computer move) but comparing two counters that
were never meant to be compared directly. Verified by diffing
`assets/js/main.js` at the WI-7 tag (`7ec97df`, no generation system at
all, computer always moved) against `cc179fc`. The two subsequent WI-8
commits (`6838f7c`, `edbc576`) added more guards around the same flawed
comparison without ever fixing it.

## 7. Why the existing 6 pytest / 43 Node tests missed it

None of them exercise `assets/js/main.js` at all. `tests/*.py` and
`tests/js/*.test.mjs` only import/exercise `engine.js` (pure game rules),
`ai.js` (move calculation), and `storage.js` (localStorage) — verified
directly by grepping every test file's imports. `main.js` — 100% of the
DOM wiring, `setTimeout` scheduling, and the buggy generation guard —
had **zero** executable coverage. `tests/test_bootstrap.py` only checks
that static files return HTTP 200 and that the HTML contains expected
strings; it never loads the page in a browser or drives any
interaction. The bug lived entirely in code that no automated test had
ever touched.

## 8. RED browser regression evidence

Two distinct pieces of evidence:

- **Operator-captured, independent** (§4 above): the diagnostic
  Playwright script run before wi-9 existed, against `edbc576`,
  `BUG_REPRODUCED: true`.
- **DEV A's own committed test** (`tests/browser/computer_turn_regression.test.mjs`,
  committed as part of `12f9079` on the abandoned `work/wi-9-computer-turn-regression`
  branch): asserts `oCells === 1` and `statusText !== "L'ordinateur joue"`
  after a human move — logically must fail against the pre-fix code
  given §4's evidence. **Not independently re-executed by the operator
  against the pre-fix commit this session** — the user's final
  instruction was to stop cleanly without launching any further run, so
  this specific re-verification was intentionally not performed. This is
  stated plainly rather than implied.

## 9. DEV A worker/provider (wi-9, first attempt)

`alice` / anthropic / `claude_code`, execution `e904d45d7da44196bd74acc288856a9c`,
`status=succeeded`, 185.6s, `edbc576` → `12f9079` (real code change).
Selected naturally by the real `WorkerSelector` from the full 6-worker
pool — never forced.

## 10. DEV B worker/provider (wi-9, first attempt)

`juno` / mistral / `vibe`, execution `2fa14874203e45eaad46030f8283de3a`,
`status=failed`, 223.7s, `git_sha_before == git_sha_after` (no code
change). Selected naturally (Anthropic was available, OpenAI was
`quota_exhausted` at that moment) — never forced.

## 11. Corrections made by DEV B

**None.** DEV B's execution never reached a review verdict at all: its
Ralph loop (`primary-20260918-094229`) ran 5 iterations of pure model
reasoning (`iteration.summary` events only, no `ralph emit` of any
topic) and hit the orchestrator's pre-existing, unrelated
`DEFAULT_MAX_ITERATIONS = 5` cap, terminating with
`reason=max_iterations`, `exit_code=2`. Per `RalphExecutionEngine`'s
fail-closed design (no recognized terminal event = FAILED), this
correctly produced `ExecutionStatus.FAILED`. This is a real technical
execution failure (likely the review task's real complexity — a
multi-file diff plus a new 161-line browser test — combined with a
review-role budget that every worker so far had comfortably finished
within on smaller diffs), not a content judgment on DEV A's fix, and
not caused by anything changed in this or the prior session. Per "no
blind retry for model/quota behavior," this was not retried.

## 12. Fix diff summary (wi-9, unmerged — evidence only, not landed)

`assets/js/main.js`: removed the separate `computerMoveGeneration`
counter; `thisGeneration` is now captured directly from
`currentGameGeneration` at schedule time, so the guard compares the
correct pair of values (2 lines changed). Added
`tests/browser/computer_turn_regression.test.mjs` (161 lines): a
self-contained Playwright test that starts its own ephemeral local HTTP
server, drives a real Chromium page, and checks (1) a human move is
followed by exactly one computer O and the status leaving "L'ordinateur
joue", with zero JS errors, and (2) starting a new game during the
computer's think-delay never produces a phantom O or a double move.
**This commit (`12f9079`) sits only on the abandoned
`work/wi-9-computer-turn-regression` branch. It was never reviewed by
DEV B, never QA'd, never merged to `main`, and this session did not
cherry-pick or manually merge it** — exactly per instruction.

## 13. GREEN browser regression evidence

**Not yet obtained.** wi-9 never reached QA (DEV B failed first), so
the fix above is unverified by the orchestrator's own deterministic
gate. wi-10 (second attempt, fresh WorkItem, same real mechanism) was
created to carry this forward but is currently `WAITING` on real
provider availability (§18) — GREEN evidence is pending that resume.

## 14. Existing pytest result

Unaffected: `main` is still `edbc576`, unchanged throughout. The 6/6
pytest result recorded in the previous report
(`docs/reports/morpion-vibe-continuation-2026-09-18.md`) still describes
`main`'s actual current state.

## 15. Existing Node result

Unaffected, same reasoning: 43/43 on `main`, unchanged.

## 16. Browser/E2E result

Not yet obtained for the fix itself (§13). The only real browser/E2E
result obtained this session is the RED reproduction in §4/§8.

## 17. Easy/Medium/Hard response verification

**Not performed.** This is a post-fix verification step (per the
brief's own "POST-FIX MANUAL-LIKE VERIFICATION" section) and no fix has
landed yet.

## 18. Stale-timer/new-game verification

Not independently performed by the operator this session (no fix has
landed to verify). DEV A's own unmerged test (§12) includes an assertion
for this exact scenario, untested against a landed fix.

## 19. QA result

wi-9: QA never ran (`qa_runs: []` — DEV B failed before the flow reached
QA). wi-10: not yet reached (currently `WAITING`).

## 20. Merge

**None.** `main` was never touched by either WorkItem.

## 21. Tag

**None** created for wi-9 or wi-10 (only WI-0..WI-8's pre-existing tags
remain, unchanged: `feature/wi-0-initialisation/done` through
`feature/wi-8-stabilisation/done`).

## 22. Final SHA

`main` = `edbc57612b4885359f2edec089238a6d08bc09cc` — identical to the
broken SHA in §2. The bug is **still present on `main`** as of this
report.

## 23. Target cleanliness

Verified read-only, at the very end of this session, with no further
run launched: `git status --short` empty (clean working tree) on the
currently checked-out branch (`work/wi-9-computer-turn-regression`,
left checked out as-is rather than switching branches, to avoid any
further repository action beyond what was requested); `main`'s own
commit content is provably unchanged (`git rev-parse main` =
`edbc57612b4885359f2edec089238a6d08bc09cc`, and its tip commit's own
content cannot differ regardless of which branch is currently checked
out). All 10 branches preserved:
`main`, `work/wi-0-initialisation` .. `work/wi-8-stabilisation`,
`work/wi-9-computer-turn-regression` (preserved intact, commit
`12f9079`, never amended, never deleted).

## 24. Orchestrator (`ai-dev-orchestrator`) cleanliness

Unchanged throughout this entire investigation: HEAD
`d2889acb6c6a0d9ac89d64bd9a1eac9ff181a12e` before and after every driver
run (verified by each driver script itself, and consistent with the
previous session's final state). No product code was touched in this
investigation.

## 25. Commit governance

No new commits were created on the Morpion repo by this session's
process (wi-9's single real commit, `12f9079`, was made by `alice`
during her real execution — author/committer
`yannickameur <yannick.ameur@gmail.com>`, no AI-attribution trailer,
verified). No commit was cherry-picked, merged, or amended by the
operator.

## 26. Lessons learned for QA coverage

**Why did deterministic QA declare PASS (in the prior report) while the
user-visible game was broken?** Not merely "there was no Playwright
test." The exact gap: **`main.js` — the only module containing DOM
wiring, `setTimeout`-based turn scheduling, and the generation-token
guard where the bug actually lived — had zero executable coverage of
any kind**, unit or browser-level. `engine.js`/`ai.js`/`storage.js`
(fully covered) contain no scheduling logic at all; the regression is
structurally invisible to any test that only imports those three
modules. Additionally, the previous session's own QA gate
(`ValidationStore` for this project) only ever registered
`pytest-full-suite` as a required command — the 43 Node unit tests were
run manually by the operator afterward as an extra check, **never wired
into the deterministic QA gate itself**. This session's wi-9/wi-10 QA
configuration fixes that specific gap by also registering
`node-unit-tests` and `browser-computer-turn-regression` as required
gate commands (§ driver scripts) — a data-configuration change via the
existing `ValidationCommand`/`set_project_commands` primitive, not a new
QA architecture.

**Classification: (A) target-project test gap, primarily** — the
missing coverage is specific to this project's own module boundaries
(a DOM/timer-integration layer with no test target), not evidence that
`AI Dev Orchestrator`'s QA primitives are architecturally insufficient:
the existing `ValidationCommand` mechanism was fully capable of running
a browser-level check the moment one was configured, with zero new
orchestrator code required. There is a secondary, smaller **(B)**
observation worth recording without acting on it: nothing in the
orchestrator's QA configuration *by default* nudges a project toward
including DOM/browser-level coverage for projects that have a browser
UI — that was purely an operator/worker judgment call each time,
project by project. Whether that's worth a future, broader
"technology-aware QA capability" is a real open question, **not decided
or implemented here**, per instruction.

---

## Appendix: wi-9 → wi-10 sequence, exactly as it happened

1. wi-9-computer-turn-regression created (dependency: `wi-8-stabilisation`,
   full worker pool, no forced worker/provider).
2. DEV A = `alice` (anthropic): succeeded, real candidate fix + new
   browser regression test, committed as `12f9079` on
   `work/wi-9-computer-turn-regression`.
3. DEV B = `juno` (mistral/vibe): **failed** — exhausted Ralph's
   pre-existing 5-iteration loop cap without ever emitting a business
   verdict. Not a content rejection; a technical execution failure.
4. wi-9 reached terminal `status=failed`. Never merged. Never amended.
   Never reopened. Branch and commit preserved exactly as produced.
5. Per "no blind retry of the same execution" but "the product is not
   done until fixed," wi-10-computer-turn-regression was created as a
   **new** WorkItem (same acceptance criteria, same dependency on
   `wi-8-stabilisation`, full pool again, nothing forced) — not a reopen
   of wi-9.
6. Real, unforced quota snapshot at that moment: **all three providers
   unavailable** — `anthropic: quota_exhausted`, `openai:
   quota_exhausted`, `mistral: unknown` (Mistral's honest
   `EXECUTION_PROBE_ONLY` classification — no distinguishable quota
   signal exists for it, so any real probe failure is reported as
   `unknown`, never fabricated as `quota_exhausted`).
7. `run_next_work_item` correctly, honestly returned wi-10 as `WAITING`
   — never forced through. Durable wait record: `phase=development`,
   `reason=quota_reset`, `status=PENDING`, `providers=('anthropic',
   'openai')` (Mistral is not listed in the wait record's provider tuple
   — consistent with it never producing a `reset_at`-bearing quota
   signal to wait on), **`eligible_at=2026-09-18T11:30:00+00:00`**.
8. Session stopped cleanly on explicit instruction: no further worker
   launched, no cherry-pick, no manual merge, no wi-11, no provider
   availability modified, no reset credit consumed. Verified read-only,
   one final time, immediately before writing this report.

## 27. Resume attempt (new session) — `DURABLE_RECOVERY_FAILURE`

A later session was instructed to resume wi-10 from its real durable
state (never recreate it). Before touching anything, target cleanliness
was re-verified read-only: `git rev-parse main` = `edbc57612b4885359f2edec089238a6d08bc09cc`
(unchanged), `git status --short` empty, all 10 branches present
including `work/wi-9-computer-turn-regression` at `12f9079` (unchanged,
untouched).

The wi-10 driver's own store directory (per the previous session's
Vibe-continuation report, §Timing/config context: same
`tempfile.mkdtemp(prefix="morpion-3d-lean-pilot-")` pattern used by the
sibling Mars Rover driver, i.e. a fresh directory under `/tmp` per run,
never a fixed path) could not be located:

- `find / -iname "morpion-3d-lean-pilot-*"` — zero matches anywhere on
  the filesystem.
- `/tmp` itself contains no pilot/orchestrator artifacts of any kind —
  only current-session runtime sockets/locks. System `uptime -s` reports
  a boot time of `2026-09-18 13:29:48`, i.e. **after** wi-10's recorded
  `eligible_at` (`2026-09-18T11:30:00+00:00`) and after the scratchpad
  session activity recorded under
  `/var/tmp/claude-1000/-home-jarvis-projects-morpion-web-3d/*` (latest
  timestamp `2026-09-18 11:39`) — consistent with an intervening reboot
  clearing `/tmp` (a tmpfs/systemd-cleaned tmp is the standard cause; not
  independently confirmed beyond this timing evidence).
- `/home/jarvis/projects/pilot-evidence/` — the directory this project
  uses to durably preserve pilot sqlite stores — contains only
  `mars-rover-run*` evidence; no morpion entry exists there.
- The `morpion-web-3d` Claude-session scratchpad directories under
  `/var/tmp/claude-1000/-home-jarvis-projects-morpion-web-3d/` were
  enumerated read-only; every one of them is an empty directory tree (no
  files), so they hold no store copy either.
- No `project.sqlite3` / `waits.sqlite3` / `executions.sqlite3` /
  `git_governance.sqlite3` matching the wi-10 pilot run exists anywhere
  searched.

**Conclusion: `DURABLE_RECOVERY_FAILURE`.** wi-10's real persisted state
(`ProjectStateStore`/`WaitStore`/`ExecutionStore`/`GitWorkItemStore` for
this pilot run) no longer exists on disk. Per explicit instruction, this
was **not** worked around: wi-10 was not recreated, no WI-11 was
created, no store was fabricated, no WaitRecord was invented, and no
status was manually edited. The Morpion `main` branch, `work/wi-9-*`
branch, and the `ai-dev-orchestrator` repository were left completely
untouched by this resume attempt. The bug described in §1–§11 remains
**unfixed on `main`** (`edbc576`); wi-9 remains `FAILED`, unmerged,
preserved exactly as before.

## 28. WI-11 — validation/integration of the preserved candidate fix

A third, later session was instructed to validate and integrate the
preserved wi-9 candidate (`12f9079`), which the user had by then manually
tested (checked out `work/wi-9-computer-turn-regression`, played the
game, confirmed the computer responds) — `MANUAL_USER_BROWSER_PASS`,
recorded as supporting evidence only, never a substitute for deterministic
QA.

**Pre-write verification** (read-only, before any change): `git
rev-parse main` = `edbc57612b4885359f2edec089238a6d08bc09cc` (unchanged),
`git status --short` empty, all 10 pre-existing branches present
including `work/wi-9-computer-turn-regression` @ `12f9079` untouched.

**Candidate inspection**: `git diff edbc576..12f9079` confirmed exactly 2
files — a 4-line change to `assets/js/main.js` (removes the separate
`computerMoveGeneration` counter; `thisGeneration` now captured directly
from `currentGameGeneration`) and a new 161-line
`tests/browser/computer_turn_regression.test.mjs` — no unrelated
redesign.

**RED/GREEN evidence, re-executed independently this session** (disposable
`git worktree`s, `main` never touched): the wi-9 browser regression test,
run via `node --test` (Playwright, Chromium 1243, resolved through the
npm `_npx` cache's surviving `playwright` package since the browser
binaries under `~/.cache/ms-playwright` survived the reboot but the prior
`/tmp/node_modules` copy did not) —
- Against `edbc576`: **RED**. `not ok 1 - ... l'ordinateur joue son coup
  O`, `error: 'page.waitForFunction: Timeout 5000ms exceeded.'`, exit
  code 1.
- Against `12f9079`: **GREEN**. 2/2 pass, exit code 0.

**Governance-safe candidate reuse**: `src/orchestrator/git_governance.py`
was read in full. `GitGovernanceService.prepare_work_item()` always
branches a new WorkItem from `base_branch`'s own current tip
(`base_sha = ws.head_sha(self._policy.base_branch)`) — no
cherry-pick/adopt/seed-SHA primitive exists anywhere in this module or
elsewhere in `src/orchestrator/`. **Candidate-SHA reuse is NOT supported
by the existing governance API.** Per instruction, this was not worked
around with a manual branch/cherry-pick bypass; the governed
reapplication path was used instead: WI-11 was created normally from
`main`, and DEV A was given `12f9079`'s root cause and diff shape as an
explicit historical reference (the only channel available —
`WorkItem.title`/`acceptance_criteria`, which is all
`_build_dev_instructions`/`_build_dev_b_instructions` ever consume) and
instructed to reproduce the equivalent minimal correction under normal
governance, never to redesign.

**Persistent state root**: `ProjectStateStore`/`HandoffStore`/
`ExecutionStore`/`ValidationStore`/`QARunStore`/`GitWorkItemStore`/
`RealizationReportStore`/`WaitStore` constructors already accept an
arbitrary `Path` — no product code change was needed. New driver
`scripts/run_morpion_wi11.py` (untracked, same status as the earlier
external-pilot harness) points every store at
`~/.local/state/ai-dev-orchestrator/projects/morpion-web-3d/` instead of
`/tmp`, so this run's state survives a future reboot.

**Provider snapshot at the moment of real, unforced resume**: `anthropic`
available (5h window 7-18% utilized across the run, 7d window
72-73%), `openai` `quota_exhausted`, `mistral` `unknown`
(`EXECUTION_PROBE_ONLY`, honest). `WorkerSelector` was never forced.

**DEV A**: `alice` / anthropic / `claude_code`, execution
`6296a99e3cfb4a23b844b00e866857e5`, succeeded. Produced one real commit,
`593c615e66e6a2cb585fb465ded0185da46a3319`, on `work/wi-11` — the
`assets/js/main.js` hunk is **byte-identical** to `12f9079`'s (DEV A
reapplied the equivalent minimal fix from the reference, not a
rediscovered/different mechanism), plus a self-contained 3-scenario
Playwright regression test (the same two wi-9 scenarios, plus a third —
"no computer move after game end" — genuinely broader than wi-9's own
test), plus `package.json`/`package-lock.json`/`.gitignore` updates
declaring `playwright` as a real `devDependency` (wi-9's candidate had
relied on an ad hoc `NODE_PATH`; this is a small, directly-required
hygiene improvement, not scope creep). Commit author/committer:
`yannickameur <yannick.ameur@gmail.com>`, no AI attribution trailer.

**DEV B**: `bob` / anthropic / `claude_code` (naturally selected —
`openai` was quota-exhausted and `mistral`'s priority, 60, is below
`bob`'s, 90; cross-provider is preferred, not required, per instruction,
and was not forced), execution `b8ed6a6b213c41468b3b2cda021107fb`,
succeeded, **no code change** (`changed_code=False`, same head SHA
before/after) — a valid no-issue-found corrective review, explicitly
acceptable per instruction. `DEV_B.worker_id (bob) != DEV_A.worker_id
(alice)` — invariant held.

**Deterministic QA** (real `QualityGateRunner`/`InternalQAEngine`, 3
required commands, exact-SHA evidence, run `33f8b0367b4249ea8e98b55936e6e0d3`
against head `593c615`):
| command | result |
|---|---|
| `python3 -m pytest -q` | PASSED — `6 passed in 4.55s` |
| `node --test tests/js/ai.test.mjs tests/js/engine.test.mjs tests/js/storage.test.mjs` | PASSED — 43/43 |
| `node --test tests/browser/computer_turn_regression.test.mjs` | PASSED — 3/3, 0 console/page errors |

Verdict: **PASS** — "all mandatory QA evidence present and passing for
the current head SHA".

**Merge/tag — governance policy defect found and fixed in the driver
script (not in orchestrator product code)**: the first run of
`scripts/run_morpion_wi11.py` left WI-11 `COMPLETED` (QA PASS) but its
git record stuck `IN_PROGRESS` — `compute_merge_eligibility` correctly
refused merge ("required quality gate has not passed") because the
driver had constructed `GitGovernancePolicy(auto_merge=True,
base_branch="main")`, leaving `require_required_gates`/`require_review`
at their real default of `True`, while LEAN_FEATURE_FLOW's own
`MVPManager` wiring never configures a `quality_gate_runner` or
`review_store` (by design — the QA phase's own commands ARE the gate;
DEV B's corrective review replaces the review step). This exact
misconfiguration also exists, unfixed, in the pre-existing
`scripts/run_external_project_pilot.py` (its own `mars-rover-run1..4-failed`
evidence directories are consistent with the same root cause).
`tests/test_mvp_manager_lean_feature_flow.py`'s own fixture already
documents the correct wiring:
`GitGovernancePolicy(auto_merge=True, require_review=False,
require_required_gates=False)`. This was fixed in
`scripts/run_morpion_wi11.py` only (a disposable, untracked harness
script — not `src/orchestrator/*`); no orchestrator product code was
touched. A second run then used the real
`GitGovernanceService.compute_merge_eligibility`/`.merge()` primitives
(no manual branch/cherry-pick/fast-forward) against the already-persisted
QA evidence — no DEV A/DEV B/QA re-run was needed or performed.

**Merge**: `eligibility.mergeable=True` →
`git_service.merge("WI-11", ...)` → fast-forward `main` from `edbc576`
to `593c615e66e6a2cb585fb465ded0185da46a3319`.

**Tag**: `feature/WI-11/done` at `593c615e66e6a2cb585fb465ded0185da46a3319`,
via the existing tagging convention (no new convention invented).

**Post-merge browser verification** (real Playwright/Chromium against the
now-fixed `main`, live checkout, read-only otherwise):
| scenario | result |
|---|---|
| Facile: human X → computer O | PASS — 1 O, status `À vous de jouer`, 0 JS errors |
| Moyen: human X → computer O | PASS — 1 O, status `À vous de jouer`, 0 JS errors |
| Difficile: human X → computer O | PASS — 1 O, status `À vous de jouer`, 0 JS errors |
| New game during AI think-delay | PASS — board fully empty, no stale O, status `À vous de jouer` |
| No double computer move (2s wait after one human move) | PASS — exactly 1 O |
| No move after game end (full game played to a computer win on Difficile, then 1s extra wait) | PASS — O count unchanged (3 before/after wait) |

**Final SHAs**: `main` before = `edbc57612b4885359f2edec089238a6d08bc09cc`;
`main` after = `593c615e66e6a2cb585fb465ded0185da46a3319`. `work/wi-9-*`
still points at `12f9079`, untouched. `git status --short` on the target
repo: empty throughout and at the end.

**Orchestrator (`ai-dev-orchestrator`) product code**: unchanged — only
this untracked report and the new untracked `scripts/run_morpion_wi11.py`
harness were added/edited; nothing under `src/orchestrator/` was touched.

**Historical sequence, preserved exactly**: `edbc576` broken → wi-9
`FAILED` before QA (candidate evidence preserved, unmerged) → wi-10
`WAITING` on quota, then `LOST_RUNTIME_STATE_AFTER_REBOOT` (§27) → wi-11
created fresh, validated the wi-9 candidate under normal governance, real
QA PASS, merged + tagged → `main` fixed at `593c615`.

**Not started, per instruction**: Mammouth, Ollama, provider-ecosystem
study, MVP 0.2, project-specific worker priorities, a generic
product-level persistent-state-root redesign (this run only changed
where *this one harness script's* stores live — no
`ProjectStateStore`/etc. default path was changed, no new configuration
surface was added to the product). No push. No reset credit consumed
beyond real, unforced execution usage.
