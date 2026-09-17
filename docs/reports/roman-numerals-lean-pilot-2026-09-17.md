# Roman Numerals — LEAN_FEATURE_FLOW real acceptance pilot (2026-09-17)

Untracked report. Not committed. Not yet a documented product claim — pending user review.

## 1. RESULT

**PASS.**

## 2. Orchestrator HEAD

`cc5728ff11bbaa111853430ee07596edf6c45398` (tag `v0.1.0`, MVP 0.1 closure). Confirmed
unchanged before and after the run, both by the driver script and independently.

## 3. Target repository

`~/projects/roman-numerals-kata` — fresh, local, no remote, `git init -b main`.

## 4. Original kata scope

Exact kata statement (Part I: int → Roman numeral, up to ~3000; Part II: Roman
numeral → int; TDD clues) reproduced verbatim in the target repo's `README.md`.
No signature, class name, module name, algorithm, data structure, or exhaustive
test list was supplied by the orchestrator/driver. No decomposition into
Part I / Part II WorkItems — exactly one WorkItem was created.

## 5. Baseline SHA

`818641b5f04b57af91432f6fda1965d907b12630` — "Initialize Roman Numerals kata".

## 6. Baseline state

Files: `README.md`, `ROADMAP.md`, `pyproject.toml` (minimal `[tool.pytest.ini_options]`
marker only — no testpaths, no package skeleton, no source file, no test file).
`pytest -q` on this baseline: exit code 5, "no tests ran in 0.00s". No RED test
prescribing the solution was manufactured.

## 7. WorkItem

- Title: "Implement the Roman Numerals kata described in this repository's
  README.md, using TDD."
- Required capability: `development`
- Acceptance criteria: none added beyond the title (the kata itself, in
  README.md, is the specification DEV A/DEV B read directly from the workspace).
- Exactly one WorkItem, no Part I/Part II split imposed.

## 8. DEV A facts

- worker_id: `alice`
- provider: `anthropic`
- backend: `claude_code`
- model/profile: `sonnet` (default profile), reasoning_effort: `null`
- execution_id: `72b9058500c2427ea1872554a8757e6e`
- duration: 82.13s (2026-09-17T21:00:09.98Z → 21:01:32.12Z)
- git_sha_before: `818641b...` (baseline)
- git_sha_after: `3ecd70d8...` (one commit: "Implement Roman Numerals kata via TDD")

## 9. DEV B facts

- worker_id: `bob`
- provider: `anthropic`
- backend: `claude_code`
- model/profile: `sonnet`, reasoning_effort: `null`
- execution_id: `fbc155c137444e078d88a24db7bcdc4a`
- duration: 21.07s (21:01:32.12Z → 21:01:53.19Z)
- git_sha_before / git_sha_after: `3ecd70d8...` / `3ecd70d8...` — **unchanged**:
  DEV B reviewed and found nothing to correct, committed nothing.
- Ralph event log confirms a genuine corrective-review prompt was run
  (`work.start` "Corrective review of the implementation just produced by
  another developer (alice)...") and a clean `work.completed "done"` — not a
  skipped/faked review.

## 10. Worker independence

`DEV_B.worker_id (bob) != DEV_A.worker_id (alice)` — **satisfied, REQUIRED
invariant held.**

## 11. Provider selection/fallback

Real, read-only quota probe at run start: `anthropic` AVAILABLE, `openai`
**QUOTA_EXHAUSTED**. `WorkerSelector` fell back to a **second, independent
anthropic worker (bob)** for DEV B rather than waiting for `openai` — exactly
the worker-pool same-provider fallback validated offline in the MVP 0.1
closure, now observed for real. Provider diversity (PREFERRED, not REQUIRED)
was correctly *not* enforced as a blocker.

## 12. Waits

None. `wait_hit: null` — no `WAITING` state was ever entered; the fallback
resolved within the initial selection, no polling needed.

## 13. QA facts

- Phase: `FINAL_VERIFICATION` (Lean's single QA phase)
- Engine: `internal` (`InternalQAEngine`, deterministic, non-LLM)
- Attempts: **1**
- Manifest command: `python -m pytest -q` (the target project's own full
  suite, as produced by DEV A/DEV B — no pre-selected/targeted test id)
- Verdict: **PASS** — "all mandatory QA evidence present and passing for the
  current head SHA"

## 14. Final pytest

`22 passed in 0.02s`, exit code 0 (re-run independently by the driver after
orchestration, and again during post-run inspection).

## 15. Merge

`GitWorkItemStatus.MERGED`, `merged_sha == 3ecd70d8...` == final HEAD. Fast-forward,
local only, `auto_merge=True`. No PR, no push, no remote configured.

## 16. Tag

`feature/wi-roman-numerals-kata/done`, present on the merged HEAD.

## 17. Actual LLM execution count

**2** (DEV A + DEV B). Matches the Lean nominal-path invariant exactly — QA
consumed zero LLM executions.

## 18. DEV FIX count

**0.** QA passed on the first attempt; no rework cycle was ever entered.

## 19. Elapsed time

Real LLM active execution: DEV A 82.13s + DEV B 21.07s = **103.2s (~1m43s)**.
QA (deterministic pytest run) + merge + tag: sub-second. Total pilot wall-clock
(including provider quota probes, store wiring, and post-run inspection) was on
the order of a few minutes; no separate wall-clock instrumentation was added
around the whole script run, so only the execution-level durations above are
reported as hard facts.

## 20. Active execution time

**103.2s** — see above (sum of the two real `ExecutionRecord` durations).

## 21. Tokens

**NOT_AVAILABLE.** Ralph's own `iteration.summary` events report
`input_tokens=0`, `output_tokens=0`, `cost_usd=0.0`, `num_turns=0` for both
DEV A and DEV B — already known and documented elsewhere in this project as
unreliable Ralph telemetry (not a real zero). No token estimate is reported.

## 22. Public API chosen by developers

A single module `roman_numerals.py` at the repo root, two functions:
`to_roman(number)` and `from_roman(numeral)`. `to_roman` uses a static
ordered list of `(value, symbol)` pairs already including the six subtractive
pairs (`CM`, `CD`, `XC`, `XL`, `IX`, `IV`) and a greedy while-loop. `from_roman`
uses a symbol→value dict and a left-to-right scan comparing each symbol's
value to the next one to decide add/subtract. No class, no exceptions, no
CLI, no extra module — not suggested by the orchestrator, chosen entirely by
DEV A.

## 23. Tests designed by developers

22 tests in `tests/test_roman_numerals.py`: 13 example-based `to_roman`
cases (1, 2, 3, 4, 5, 9, 40, 90, 400, 900, 3000, 1989, 2023), 8 example-based
`from_roman` cases (mirroring most of the same values), and **one property
test** (`test_round_trip_for_every_number_up_to_three_thousand`) asserting
`from_roman(to_roman(n)) == n` for every `n` in `1..3000` — a genuinely
strong correctness check going beyond example-based tests, chosen by DEV A,
not suggested by the driver/orchestrator.

## 24. TDD evidence / NOT_VERIFIABLE

**TDD_STRICTNESS = NOT_VERIFIABLE.** The single commit's message claims
"Built up test-first in small steps, then verified with a round-trip
property test" — but the entire implementation and test suite landed in
**one** commit, and Ralph's own loop summary shows exactly 1 iteration for
DEV A (no granular, independently-timestamped red/green steps are present in
Git or Ralph history to confirm this claim). The commit message is the
worker's own self-report, not independently verifiable evidence.

## 25. DEV B corrections

**No.** DEV B made zero code/test changes (git_sha before/after identical).
Its review prompt and a clean `work.completed "done"` are present in the
Ralph event log, so this was a genuine (not skipped) review that found the
candidate acceptable.

## 26. Scope creep

**None observed.** Only `.gitignore`, `roman_numerals.py`, and
`tests/test_roman_numerals.py` were added (138 lines total). No unrelated
files, no framework/dependency added, no README/ROADMAP modification by the
developers, no CLI, no packaging beyond what already existed.

## 27. Target final git status

Clean (`git status --short` empty) after merge+tag. `.ralph/`, `__pycache__/`,
`.pytest_cache/` present on disk but correctly untracked/ignored (DEV A added
a `.gitignore` covering `__pycache__/` and `.pytest_cache/`; `.ralph/` was
never staged either).

## 28. Control repo integrity

`ai-dev-orchestrator` HEAD unchanged (`cc5728f...`) before and after, verified
independently by this report's author in addition to the driver script's own
check. Tracked status unchanged (only the 3 pre-existing untracked Mars Rover
artifacts, never touched). No file under `src/`, `tests/`, or `config/` was
modified during the pilot.

## 29. No-solution-lookup constraint

No web search, no fetch of the kata's reference links/videos, and no lookup
of an existing Roman Numerals implementation was performed by the driver or
by the orchestrating session while setting up or running this pilot. Whether
DEV A/DEV B's own agentic sessions consulted anything is not independently
observable from this vantage point beyond the instruction given to them (in
their prompt, via `_build_dev_instructions`... actually via the WorkItem
title/README only — no explicit "do not look up a solution" line was injected
into DEV A/DEV B's prompt by the driver, since the driver never overrides
`MVPManager`'s own instruction-building, per this pilot's own constraint not
to hand-craft prompts). The solution produced (greedy subtractive-pairs table
for `to_roman`, left-to-right compare-and-accumulate for `from_roman`) is a
standard, commonly-known approach for this exact kata, consistent with either
genuine derivation or prior model knowledge — not distinguishable from Git
evidence alone. Recorded as an **honest limitation**, not asserted as proof of
compliance.

## 30. Anomalies

- DEV A's commit carries a `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
  trailer — the worker's own standard Claude Code commit behavior inside the
  target repo, left exactly as produced (not edited during inspection).
- Ralph's `iteration.summary` token/turn telemetry is all-zero for both
  executions — known-unreliable telemetry (see item 21), not a real anomaly
  specific to this run.
- No `NoEligibleWorkerError`/`ProviderProbeError`/orchestrator exception was
  raised at any point; no orchestrator defect was discovered during this run.

## 31. Lessons learned

- The worker-pool same-provider fallback (closed as part of MVP 0.1, 2026-09-17)
  was exercised for real, unprompted by this pilot's design — `openai` happened
  to be quota-exhausted at run time, and the orchestrator transparently used a
  second `anthropic` worker (bob) for DEV B instead of waiting. This is the
  first real-world confirmation of that specific behavior outside the offline
  test suite.
- A single `run_next_work_item(...)` call executed the entire nominal chain
  (DEV A → DEV B → QA → merge → tag) synchronously, exactly as designed —
  the pilot driver never needed to reproduce or orchestrate that sequence
  itself.
- Git/Ralph evidence, as currently captured, cannot independently confirm or
  refute a "strict TDD" claim a worker makes in its own commit message — a
  real, useful finding about the limits of what this orchestrator's audit
  trail can currently prove about *how* a worker arrived at its result, as
  opposed to *what* it produced and whether it passes.
- Ralph's token/turn telemetry remains unreliable for real Claude Code
  executions (all-zero), consistent with prior findings recorded elsewhere in
  this project — `tokens` cannot currently be reported as a trustworthy pilot
  metric.
