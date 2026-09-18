# Real example — Morpion Web 3D

This is a real, end-to-end validated example of AI Dev Orchestrator
governing an external project: a 3×3 tic-tac-toe ("morpion") web app,
built from a roadmap, by AI developer workers, under the default
`LEAN_FEATURE_FLOW` workflow (see the [main README](../../README.md)) —
not a synthetic demo.

## 1. Objective

A static, client-side web app:

- 3×3 tic-tac-toe, human vs. computer;
- three difficulties: Facile (easy, random), Moyen (medium, heuristic),
  Difficile (hard, minimax);
- score persistence (`localStorage`);
- a 3D-styled visual board/UI;
- runnable locally with nothing beyond a static file server
  (`python3 -m http.server`).

## 2. Orchestration

The project was decomposed into a roadmap of WorkItems (see
[`ROADMAP.example.md`](ROADMAP.example.md)) and driven through the
standard flow:

```
roadmap → WorkItems → DEV A → DEV B (independent corrective review) → QA (deterministic) → merge → tag
```

Each WorkItem was picked up by `MVPManager.run_next_work_item(...)`
against the real `WorkerSelector`/`RalphExecutionEngine`/
`GitGovernanceService`/QA stack — the same components this repository
ships, never a special demo path.

## 3. Provider routing observed

Across the project's WorkItems, DEV A/DEV B pairs were genuinely
selected by `WorkerSelector` — never forced:

- Anthropic (`alice`/`bob`) and Mistral/Vibe (`milo`/`juno`) both
  authored real, committed changes, in different pairings across
  different WorkItems, depending on which providers were actually
  available at the time.
- **Same-provider fallback** was observed directly: when a
  cross-provider pair wasn't available, a second worker on the *same*
  provider as DEV A still satisfied the `DEV_B.worker_id !=
  DEV_A.worker_id` invariant and produced a valid review — exactly the
  documented fallback behavior (a different provider is *preferred*,
  never *required*).
- Mistral/Vibe's availability signal is intentionally reported as
  `EXECUTION_PROBE_ONLY` (no distinguishable quota window exists for
  it) — this shows up in this project's history as `mistral: unknown`
  rather than a fabricated quota percentage.

## 4. Real QA

Every merge to `main` in this project required a real, deterministic
quality gate — never an LLM's own claim that "it works":

- `pytest` — the project's own Python-side checks;
- a Node.js unit test suite (`node --test tests/js/*.test.mjs`) for the
  game engine, AI move logic, and score storage;
- a **Playwright browser regression test** (`node --test
  tests/browser/*.test.mjs`) driving a real Chromium page over real
  HTTP (never `file://`).

## 5. The regression that mattered

Static/unit tests passing was not enough: the game engine, AI logic,
and storage layer were each fully unit-tested and green — but the
*deployed page* could get stuck. After a valid human move, the status
showed "L'ordinateur joue" ("the computer is playing") and stayed there
forever; the computer never actually played.

**Root lesson:** the DOM-wiring/`setTimeout`-scheduling module
(`main.js`) had **zero executable coverage** — none of the unit tests
imported it at all, only the pure logic modules underneath it. A bug
living entirely in that wiring layer was structurally invisible to a
test suite that only exercised engine/AI/storage. This is a project
test-coverage gap, not evidence that the orchestrator's own QA
primitives are insufficient: the exact same `ValidationCommand`
mechanism ran the fix once a browser-level command was configured, no
new orchestrator code required.

## 6. The correction

A browser regression test covering exactly this scenario (human move →
computer response → status leaves "L'ordinateur joue", plus a
new-game-during-AI-delay race check and a no-move-after-game-over
check) became **mandatory QA evidence** for this project going forward
— configured the same way as `pytest`, through
`ValidationStore.set_project_commands(...)`, no new QA architecture.

The fix itself was small and root-caused precisely: `scheduleComputerMove()`
was comparing two different generation counters (one incremented per
scheduled AI turn, one incremented only on "Nouvelle partie") that were
never equal on a game's first AI turn — the comparison was corrected to
use a single, consistent counter.

## 7. Final state

- `main` HEAD: `593c615e66e6a2cb585fb465ded0185da46a3319`
- Tag: `feature/WI-11/done`
- Post-merge browser verification (real Playwright automation): Facile,
  Moyen, and Difficile all confirmed human-move → computer-response;
  starting a new game during the AI's think-delay produced no stale/
  phantom move; no double computer move; no move after the game had
  already ended.

## 8. The persistence lesson

A governed WorkItem can legitimately end up `WAITING` (e.g. on provider
quota). That wait state must be *durable* — safe to recover from days
later, by a different process. This project's own history is direct
proof that **cross-process durability under `/tmp` is not the same
guarantee as machine-reboot durability**: `/tmp` is cleared on reboot,
and a WorkItem's runtime state that only lived there was lost that way,
for real, once.

The lesson: for any harness/example expected to support
`WAITING`/recovery across a real gap in time, point its runtime SQLite
stores (`ProjectStateStore`, `WaitStore`, `ExecutionStore`,
`GitWorkItemStore`, ...) at a persistent path outside `/tmp` — e.g.
`~/.local/state/ai-dev-orchestrator/projects/<project-id>/` — using the
`Path` argument these stores already accept. No new persistence
framework, no product architecture change: just where the state lives.
Purely disposable working copies/worktrees (a throwaway checkout used
only to reproduce a bug, torn down at the end of the same run) are a
different thing entirely and can still live under `/tmp`.

## 9. Historical honesty

This project's roadmap includes a real, documented failure — preserved
rather than erased:

- an earlier corrective WorkItem produced a real candidate fix but
  reached a terminal `FAILED` status *before* QA ever ran (its
  independent reviewer hit an execution-time iteration limit without
  producing a verdict) — it was never merged, and its branch/commit
  were preserved as historical evidence, not deleted or silently
  reopened;
- the next attempt at the same fix was legitimately `WAITING` on real
  provider quota, and then lost its own runtime state to the
  reboot-durability gap described above;
- only the WorkItem after that one actually reproduced the fix under
  normal governance, passed real QA, and merged.

Nothing above was rewritten to look cleaner after the fact. See
`ROADMAP.example.md` for the same sequence expressed as a roadmap.
