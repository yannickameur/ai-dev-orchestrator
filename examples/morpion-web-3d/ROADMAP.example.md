# Morpion Web 3D — example roadmap

Educational summary of the real WorkItem sequence used in this example
(see [`README.md`](README.md) for the full narrative). This is not a
dump of internal pilot-run logs — it is the shape a roadmap for a
project like this actually takes under `LEAN_FEATURE_FLOW`.

| WorkItem | Goal | Outcome |
|---|---|---|
| WI-0 | Static project structure, runnable locally | `DONE` |
| WI-1 | Game engine (grid, win/draw detection, reset) | `DONE` |
| WI-2 | Computer AI: Facile (random), Moyen (heuristic), Difficile (minimax) | `DONE` |
| WI-3 | Playable game UI | `DONE` |
| WI-4 | Score/difficulty persistence (`localStorage`) | `DONE` |
| WI-5 | 3D visual identity for the grid/symbols | `DONE` |
| WI-6 | UX polish and animations | `DONE` |
| WI-7 | Responsive layout and accessibility | `DONE` |
| WI-8 | Stabilization | `DONE` |
| WI-9 | Fix: computer turn stuck on "L'ordinateur joue" (1st attempt) | **`FAILED` before QA** — real candidate fix produced, independent review hit an execution-time limit without a verdict; never merged; branch/commit preserved as evidence |
| WI-10 | Fix: computer turn stuck (2nd attempt, same bug) | **`WAITING`** on real provider quota, then its own runtime state was lost to a machine reboot (state lived under `/tmp` — see README §8) |
| WI-11 | Fix: computer turn stuck (3rd attempt, from persistent state) | **`DONE`/`MERGED`** — reproduced the WI-9 candidate fix under normal governance, added it as mandatory browser-regression QA evidence, merged and tagged `feature/WI-11/done` |

## What a real acceptance criteria list looks like

For WI-11, roughly:

- After a human plays a valid move, the computer plays exactly one
  response within the app's own think-delay, and the status leaves
  "L'ordinateur joue" — proven with real browser automation over HTTP.
- Starting a new game while a computer move is pending never produces a
  stale/phantom move or a double move.
- No computer move is ever played after the game has already ended.
- A browser regression test covering the above exists, passes, and
  produces zero console/page errors.
- The existing `pytest` and Node unit test suites still pass unmodified.
- No unrelated redesign — only the scheduling bug and its regression
  coverage are touched.

## Why WI-9 and WI-10 are not deleted from this history

`LEAN_FEATURE_FLOW` never rewrites a WorkItem's terminal outcome to
make a roadmap look cleaner. WI-9's `FAILED` status and preserved
branch, and WI-10's lost-runtime-state finding, are both real facts
about this project's history — a future reader (human or agent) needs
them to understand *why* WI-11 exists at all, and what it means to
"resume" governed work safely.
