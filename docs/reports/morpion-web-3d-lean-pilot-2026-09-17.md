# Morpion Web 3D — LEAN_FEATURE_FLOW multi-WorkItem real acceptance pilot (2026-09-17)

Untracked report. Not committed. Not yet a documented product claim — pending user review.

## 1. RESULT

**PARTIAL — 6/9 phases COMPLETED, then honest `WAITING` (quota).** No functional
failure, no orchestrator defect, no forced/faked progress. This is not
classified as a technical GLOBAL PASS (WI-0..WI-8 not all completed) nor as a
FAIL (nothing broke — the run stopped exactly where real capacity ran out,
exactly as the Lean semantics are designed to do).

## 2. Orchestrator version/tag/SHA

`ai-dev-orchestrator` HEAD `ba271a4d648419934681c0a80c5cbab4d66497dc`
(confirmed by the user as the accepted test baseline for this pilot: 1
documentation-only commit ahead of tag `v0.1.0` / `cc5728f` — `git diff
cc5728f..ba271a4 -- src/ tests/ config/` is empty, product code identical).

## 3. Target repository

`~/projects/morpion-web-3d` — pre-existing directory containing the
user-supplied `ROADMAP.md` (used verbatim, untouched), turned into a fresh
local Git repo (`git init -b main`, no remote) by this pilot.

## 4. Baseline SHA

`6ddecdff674c7b3b5d1457c0487da564b642f984` — "Initialize Morpion Web 3D pilot"
(`README.md`, `CONTRIBUTING.md`, `pyproject.toml` pytest marker, the supplied
`ROADMAP.md` verbatim — no game code, no tests).

## 5. WorkItem table WI-0..WI-8

| WI | Phase | Status | DEV A→B | QA | Merge | Tag |
|---|---|---|---|---|---|---|
| wi-0-initialisation | Initialisation | **COMPLETED** | alice→bob (no changes) | PASS (1/1) | merged | `feature/wi-0-initialisation/done` |
| wi-1-moteur-de-jeu | Moteur de jeu | **COMPLETED** | alice→bob (**fixed**) | PASS (1/1) | merged | `feature/wi-1-moteur-de-jeu/done` |
| wi-2-joueur-contre-ordinateur | Joueur contre ordinateur | **COMPLETED** | alice→bob (no changes) | PASS (1/1) | merged | `feature/wi-2-joueur-contre-ordinateur/done` |
| wi-3-interface-de-jeu | Interface de jeu | **COMPLETED** | alice→bob (**fixed**) | PASS (1/1) | merged | `feature/wi-3-interface-de-jeu/done` |
| wi-4-gestion-des-scores | Gestion des scores | **COMPLETED** | alice→bob (no changes) | PASS (1/1) | merged | `feature/wi-4-gestion-des-scores/done` |
| wi-5-design-3d | Design 3D | **COMPLETED** | alice→bob (no changes) | PASS (1/1) | merged | `feature/wi-5-design-3d/done` |
| wi-6-ux-et-animations | UX et animations | **WAITING** | — (dev selection never ran) | — | — | — |
| wi-7-responsive-et-accessibilite | Responsive et accessibilité | PLANNED (never eligible) | — | — | — | — |
| wi-8-stabilisation | Stabilisation | PLANNED (never eligible) | — | — | — | — |

## 6. DEV A/DEV B identities per WorkItem

All 6 completed WorkItems: **DEV A = `alice`**, **DEV B = `bob`** (both
anthropic/claude_code/sonnet). Worker independence (`bob != alice`) held
every time. No DEV pair ever used `victor`/`oscar` (openai) — see below.

## 7. Provider selection/fallback per WorkItem

Real, read-only quota probe at run start: `anthropic` AVAILABLE, `openai`
**QUOTA_EXHAUSTED**. This held for the entire completed portion of the run:
**same-provider fallback (anthropic→anthropic) for all 6 WorkItems**, never a
cross-provider DEV A/DEV B pair — `openai` never recovered during this run.
No `WAITING` occurred for WI-0..WI-5 (a second anthropic worker was always
available). `WAITING` was finally hit on WI-6 because **anthropic itself
became quota-exhausted** partway through (after 12 real executions on the
same provider) with `openai` still exhausted — i.e. genuinely zero eligible
provider at that point, not a diversity-seeking wait.

## 8. QA attempts/results per WorkItem

All 6 completed WorkItems: **1 QA attempt, verdict PASS**, engine `internal`
(deterministic), command `python -m pytest -q`, "all mandatory QA evidence
present and passing for the current head SHA". Zero QA FAIL, zero `DEV FIX`
cycle, across the entire run.

## 9. Merge/tag per WorkItem

All 6: `GitWorkItemStatus.MERGED`, fast-forward, `merged_sha == head_sha`,
feature tag present on the merged commit (see table above). Each WorkItem's
governed work branch (`work/wi-N-...`) starts from the previous WorkItem's
merged `main` — confirmed by `git log --graph`: a single linear chain,
`6ddecdf → 1208782 → 04a5952 → 7023ee1 → a684615 → dfc13ce → f385b79 →
c366fa1 → a2b21e5`, no branching artifacts left on `main`.

## 10. Commit governance per WorkItem

**Zero violations, independently verified** (not only by the driver's own
check): `git log --format="%an <%ae>|%cn <%ce>"` on all 8 real commits shows
`yannickameur <yannick.ameur@gmail.com>` as both author and committer on
every single one, and a full-message grep for
`co-authored|claude|anthropic|generated-by|assisted-by|signed-off` across the
entire history returns nothing. Unlike the Roman Numerals pilot (which had a
`Co-Authored-By: Claude Sonnet 5` trailer), this run's workers respected
`CONTRIBUTING.md`'s explicit Git identity rule end to end. (Single pilot,
n=1 — not asserted as a general guarantee, just what was observed.)

## 11. Tests evolution

| After WI | pytest (python) | underlying JS tests (via Node, wrapped by pytest) |
|---|---|---|
| wi-0 | project structure + real local HTTP server smoke (3 tests) | — |
| wi-1 | +1 (engine wrapper) | engine.test.mjs: 20 tests |
| wi-2 | +1 (AI wrapper) | ai.test.mjs: 15 tests |
| wi-3 | (interface, no new Python-level test file) | — |
| wi-4 | +1 (storage wrapper) | storage.test.mjs: 8 tests |
| wi-5 | (design, no new Python-level test file) | — |

Final: **6 pytest tests**, wrapping **43 real Node-native-test-runner tests**
(20 engine + 15 AI + 8 storage) — all green. The developers solved the
"browser project, Python QA entry point" bridge themselves, exactly as the
pilot allowed: no framework prescribed by the driver, they chose Node's
built-in `node --test` and a thin `subprocess` wrapper per pytest file
(see `tests/test_engine.py`, gracefully `skipif`-guarded if Node is absent).

## 12. Final test result

`python -m pytest -q`: **6 passed in 4.75s** (re-run independently during
inspection). `node --test` on each `.test.mjs` file independently: engine 20/20,
AI 15/15, storage 8/8 — all pass, 0 fail.

## 13. Game engine evidence

**VERIFIED_AUTOMATED.** `assets/js/engine.js` (126 lines), DOM-independent
per the roadmap constraint (explicit docstring), 20 Node tests covering grid
init, X/O placement/alternation, occupied-cell rejection, all 8 winning
lines (rows/cols/diagonals), draw detection, end-of-game detection, no move
possible after a draw, `resetGame`.

## 14. Easy AI evidence

**VERIFIED_AUTOMATED.** Always picks a free cell (asserted over 50 samples);
demonstrated *not* to systematically win or block (200-sample distribution
tests) — matches the roadmap's explicit "ne cherche volontairement ni à
gagner ni à bloquer."

## 15. Medium AI evidence

**VERIFIED_AUTOMATED.** Takes an immediate winning move when available;
blocks an immediate opponent win; demonstrated to diverge from Hard on a
fork position (non-optimal, matching the roadmap's "conserve une part de
comportement non optimal").

## 16. Hard AI evidence

**VERIFIED_AUTOMATED — meaningfully strong.** Real Minimax (`minimaxScore`,
exhaustive, in `ai.js`). Executable proof of the roadmap's exact
requirements: takes immediate wins; blocks immediate losses; deterministic
at an equivalent position; **Hard-vs-Hard self-play always draws** (the
canonical optimality proof for tic-tac-toe); **Hard never loses vs Easy**
(30 games, both sides) nor **vs Medium** (10 games, both sides). This is
convincing, not `AI_BEHAVIOR_EVIDENCE_INSUFFICIENT`.

## 17. Interface evidence

**VERIFIED_BY_CODE_INSPECTION** (functional wiring), **MANUAL_VISUAL_REVIEW_REQUIRED**
(actual look/feel). `assets/js/main.js` (147 lines) is the only DOM-touching
module, wires engine+AI to a clickable 3×3 grid, difficulty selector, live
status text, score display, "Nouvelle partie"/"Réinitialiser les scores".
`index.html` declares `role="grid"`/`aria-live="polite"`/labeled controls.
No automated end-to-end browser interaction test exists (no Playwright/
Selenium was added — not prescribed, and the developers did not add one
either) — a real full game session was not automatically exercised end to
end through the DOM; only the engine/AI/storage units and the static-serving
smoke test were.

## 18. Score evidence

**VERIFIED_AUTOMATED** (logic) — score bookkeeping is exercised indirectly
through the storage tests (save/load round-trips) but there is no dedicated
UI-level test proving the on-screen counters increment exactly once per
game outcome (that would need DOM automation, not added).

## 19. Local persistence evidence

**VERIFIED_AUTOMATED.** `assets/js/storage.js` (82 lines) takes its storage
backend as a parameter rather than reading `window.localStorage` directly —
explicitly to stay testable without a browser and resilient if storage is
unavailable/corrupted (developers' own design choice, not prescribed). 8
Node tests: save/load scores, save/load difficulty, invalid-value fallback.

## 20. 3D visual evidence

**MANUAL_VISUAL_REVIEW_REQUIRED.** Real CSS 3D technique confirmed by code
inspection (`perspective`, `rotateX`, `translateZ`, layered `box-shadow`,
`transition`) in `assets/css/style.css` (297 lines) — no Three.js, no
dependency added (`package.json` has zero dependencies). Whether it actually
*looks* good, colorful, and readable is not something pytest/Node tests can
establish — no screenshot was captured (no browser-automation tool was
available to this driver without adding new capability, which was out of
scope).

## 21. UX/animation evidence

**MANUAL_VISUAL_REVIEW_REQUIRED / NOT_VERIFIED for most of it.** A
`prefers-reduced-motion` media query exists (accessibility-conscious), but
Phase 6 (UX et animations) itself is the WorkItem that hit `WAITING` and
never ran — "léger délai avant le coup de l'ordinateur", "désactivation des
interactions pendant le tour de l'ordinateur", transition on new game, and
explicit button feedback are **not yet implemented**, only whatever
incidentally exists from earlier phases.

## 22. Responsive/accessibility evidence

**NOT_VERIFIED (phase not reached).** WI-7 never started. Incidental
accessibility groundwork exists from earlier phases (`lang="fr"`, labeled
control, `aria-live`, `role="grid"`, `aria-label`s, `prefers-reduced-motion`),
but no responsive breakpoint (`@media (max-width: ...)`) exists yet, and
touch-target sizing/mobile layout was never addressed — expected, since this
is exactly WI-7's undone scope.

## 23. Manual visual review items

Desktop rendering quality/colorfulness/legibility of the 3D grid and symbols
(Phase 5); win-highlight animation appearance (Phase 5); any UX/animation
polish (Phase 6, not implemented); responsive/mobile layout (Phase 7, not
implemented). No screenshot evidence was produced — flagged honestly rather
than skipped silently.

## 24. Number of real LLM executions

**12** (6 completed WorkItems × DEV A + DEV B). Zero for QA (deterministic).

## 25. DEV FIX count

**0.** Every completed WorkItem passed QA on the first attempt.

## 26. Waits

**1** — WI-6 (`wi-6-ux-et-animations`), phase `development` (DEV A selection
for a fresh WorkItem), reason `quota_reset`, `eligible_at =
2026-09-18T00:50:00+00:00`. Not polled, not forced, not converted into a
functional failure — the pilot stopped here exactly as instructed.

## 27. Active execution time

Sum of all 12 real `ExecutionRecord` durations: **1641.5 s (≈ 27 min 22 s)**.
Per WorkItem: wi-0 145.3s, wi-1 270.9s, wi-2 237.1s, wi-3 377.9s, wi-4 202.5s,
wi-5 407.8s.

## 28. Wall-clock time

First execution started 2026-09-17T21:17:50Z, last one (WI-5 DEV B) finished
21:45:51Z — **≈ 28 minutes** from first real execution to hitting `WAITING`
on WI-6, closely tracking the active-execution sum above (the sequential
dependency chain left little idle time between phases).

## 29. Tokens / NOT_AVAILABLE

**NOT_AVAILABLE.** Every sampled Ralph `iteration.summary` event (checked
across 3 of the 12 executions) reports `input_tokens=0`, `output_tokens=0`,
`cost_usd=0.0` — the same known-unreliable telemetry already documented for
the Roman Numerals pilot. No token estimate is reported.

## 30. Final SHA

`a2b21e5a83b4702cf688b23b93011e7330e2c68e` (merged result of `wi-5-design-3d`,
current `main` HEAD of the target repo).

## 31. Target git status

Clean (`git status --short` empty). Tracked files: `README.md`,
`ROADMAP.md`, `CONTRIBUTING.md`, `pyproject.toml`, `.gitignore`,
`index.html`, `package.json`, `assets/css/style.css`, `assets/js/{engine,ai,main,storage}.js`,
`tests/test_{ai,bootstrap,engine,storage}.py`, `tests/js/{ai,engine,storage}.test.mjs`.
`.pytest_cache/`, `__pycache__/`, `.ralph/` present on disk but correctly
untracked/ignored.

## 32. Orchestrator integrity

`ai-dev-orchestrator` HEAD unchanged (`ba271a4...`) before and after,
verified both by the driver script and independently by this report's
author. Tracked status unchanged (only the 3 pre-existing untracked Mars
Rover artifacts, never touched). `git diff -- src/ tests/ config/` empty
throughout — no orchestrator code was ever modified during this pilot.

## 33. Scope creep

**None observed** in the 6 completed phases. Each WorkItem's diff stayed
within its phase's declared scope (engine-only in wi-1, AI-only in wi-2,
interface-only in wi-3 plus one genuine DEV B bugfix, scores/persistence-only
in wi-4, visual-only in wi-5). No later-phase functionality (animations,
responsive breakpoints, deployment tooling) was implemented early. No
dependency was added beyond the zero-dependency `package.json`.

## 34. Anomalies

- None of the 12 executions produced a commit-governance violation (see §10)
  — a positive anomaly relative to the previous pilot, not a problem.
- WI-5's DEV A execution ran 2 Ralph iterations (4m36s) instead of the
  single-iteration pattern seen everywhere else (11 of 12 executions) — the
  only execution to do so; not independently explained beyond "more complex
  phase," and not proof of anything beyond one more agentic loop pass.
- No `NoEligibleWorkerError`/`ProviderProbeError`/uncaught exception was
  raised at any point; the `WAITING` outcome was produced by the
  orchestrator's own documented mechanism, not a crash.

## 35. Product limitations discovered

- **None requiring an orchestrator code change.** The QA contract
  (`python -m pytest -q`, deterministic `InternalQAEngine`) worked for a
  browser/JS project without any modification — the developers bridged
  Python↔Node themselves. No `QA_STACK_LIMITATION`, no
  `PRODUCT_USABILITY_LIMITATION` was hit: `MVPManager.run_next_work_item`
  orchestrated all 9 WorkItems' dependency chain, worker fallback, and Git
  governance without the driver needing to implement any missing behavior.
- The one real limitation this pilot surfaces is **not a bug**: with only 2
  workers on the only available provider (anthropic) and a longer,
  multi-phase project, that provider's own quota was exhausted by real,
  legitimate use after 12 executions — a capacity/duration observation
  about running a large multi-phase pilot on a single available provider,
  not a defect in the worker-pool fallback design itself (which behaved
  exactly as documented: prefer cross-provider, fall back to same-provider,
  only ever `WAITING` when truly nothing is left).

## 36. Lessons learned

- The dependency-chained, one-WorkItem-per-roadmap-phase decomposition
  worked exactly as designed: each phase started from the previous phase's
  real merged code, no WorkItem was ever re-created, and `MVPManager`
  required zero manual intervention across 6 full DEV A→DEV B→QA→merge→tag
  cycles.
- DEV B's genuine corrective commits (wi-1: pinned `engine.js` as an ES
  module via `package.json`; wi-3: fixed a pending-computer-move bug on
  "new game") are real, substantive evidence that the Lean corrective
  review step catches and fixes real integration issues, not just rubber-
  stamps DEV A's work — 2 of 6 completed WorkItems needed and got a real
  fix from DEV B, without ever needing a second DEV FIX/QA cycle.
- A multi-phase, single-available-provider pilot is a meaningfully
  different real-world load than the single-WorkItem Roman Numerals pilot:
  it exhausted real capacity partway through, which is exactly the
  scenario the worker-pool/`WAITING` design exists to handle honestly
  rather than mask.
- TDD strictness remains `NOT_VERIFIABLE` from Git evidence across all 6
  completed WorkItems (each phase landed as 1-2 commits, never a granular
  red/green sequence) — the same structural limitation observed in the
  Roman Numerals pilot, now confirmed across a much larger sample.
- The developers' own choice to wrap Node-native tests behind a thin,
  `skipif`-guarded pytest subprocess call is a genuinely good, unprescribed
  solution to the "QA entry point must be `python -m pytest -q`" constraint
  — worth remembering as evidence that this constraint does not force
  awkward engineering for non-Python targets.
