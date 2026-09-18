# Contributing

Thanks for your interest in AI Dev Orchestrator. This project is young
and opinionated — please read this before opening a PR.

## Development setup

Requires Python 3.10+ (per `pyproject.toml`; developed/tested against
3.14).

```bash
git clone https://github.com/yannickameur/ai-dev-orchestrator.git
cd ai-dev-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

The full suite is offline — no real Anthropic/OpenAI/Mistral calls, no
provider credentials required, no quota consumed. It is what CI runs
(`.github/workflows/ci.yml`).

A few scripts under `scripts/` are deliberately **excluded** from the
offline suite (their own docstrings say so explicitly, e.g. "NEVER run
this via pytest") — they launch real provider workers, consume real
quota, and are meant for manual, opt-in verification only. Do not add
these to CI.

There is currently no configured linter/formatter/type-checker in this
repository — don't assume one; if you want to propose adding one, do
it as its own PR with its own justification rather than folding it
into an unrelated change.

## Contribution workflow

1. Open an issue first for anything non-trivial (new capability,
   architecture change) — this project follows **REUSE FIRST**: before
   building something new, we look for an existing mechanism it can
   extend, and a PR that duplicates existing machinery will likely be
   asked to reuse it instead.
2. Keep changes focused. A bug fix doesn't need an accompanying
   refactor.
3. Add or update tests for behavior you change. Tests should exercise
   the real code path they claim to cover — a test that can't actually
   fail isn't worth adding.
4. Open the PR against `main`.

## Architecture principles

These are load-bearing product decisions, not style preferences — see
`ROADMAP.md` for the full rationale behind each:

- **REUSE FIRST**: before implementing a new capability, look for an
  existing mechanism (in this codebase or in Ralph, which this project
  deliberately sits on top of rather than reimplements) that already
  does it or can be extended to.
- **KISS / YAGNI**: prefer the smallest change that satisfies a proven
  need. Don't build for a hypothetical future requirement.
- **`WorkerSelector` owns worker selection.** Capability match →
  governance (author exclusion, `DEV_B.worker_id != DEV_A.worker_id`)
  → provider availability → priority. Nothing else in the codebase
  picks a worker.
- **Ralph owns fine-grained execution.** `RalphExecutionEngine` invokes
  the real `ralph` CLI as a subprocess; this project never
  reimplements Ralph's own iteration/hats/TDD loop.
- **`LLM IS NOT ORACLE`**: a single AI developer's output is never
  trusted on its own. DEV B is a genuinely independent second
  developer (`DEV_B.worker_id != DEV_A.worker_id`, a different
  provider preferred but not required) who can correct DEV A's work
  directly, and deterministic QA — not an LLM's self-report — decides
  PASS/FAIL.
- **Deterministic QA is required for `COMPLETED`.** QA runs real,
  configured commands (pytest, project-specific test suites, browser
  regression tests, ...) and computes the verdict itself; a worker's
  own claim that "the tests pass" is never sufficient.

## Git rules

- No secrets, ever — not in code, not in test fixtures, not in
  committed reports/logs. If you're not sure something is safe to
  commit, ask first in the PR/issue rather than committing it.
- No provider-specific hardcoding in core orchestration code
  (`src/orchestrator/`). Provider-specific behavior belongs in a
  `ProviderAdapter` (`src/orchestrator/providers/`) or in
  `config/workers.yaml` — never as an `if provider == "..."` branch
  deep inside `MVPManager`/`WorkerSelector`/`RalphExecutionEngine`.
- Commit messages describe the change; no AI-attribution trailers
  (`Co-Authored-By`, `Generated-by`, etc.) unless your own tooling
  requires them for your own commits — this project's own history
  intentionally does not carry them.

## Documentation expectations

If you change observable behavior, update the docs that describe it:
`README.md` for anything a newcomer needs, `ROADMAP.md` for the
functional source of truth, `docs/status.md` for the short factual
status summary. Don't duplicate `ROADMAP.md`'s content into `README.md`
— link to it instead.
