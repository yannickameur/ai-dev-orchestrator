# Project configuration (`aido.yaml`) — P12 foundation

Source of truth for `orchestrator.project_config.ProjectConfig` — the
public project configuration format approved as part of the
productisation/onboarding cycle (ROADMAP.md §13). It solves two current
architectural problems: (A) external projects were only describable/
wired through a custom Python harness; (B) worker execution permission
mode depended entirely on machine-local CLI configuration rather than an
explicit, project-declared policy.

This document describes the configuration format and its loader
(`ProjectConfig.load(path)`), a typed, reusable API. The legacy,
internal `orchestrator.cli` module (P1, `src/orchestrator/cli.py`) is
the real, original consumer — **since P13.6, no longer installed as the
`aido` console script** (AIDO Code owns that command now; see "Engine/
library boundary" below):

- `aido init <parent-path> <project-name>` bootstraps a complete local project.
  Zero or one positional argument retains the historical config-only mode.
- `aido validate` loads/validates one and prints its facts — no side effects.
- `aido run` loads it, composes the real runtime
  (`orchestrator.project_runtime.ProjectRuntime`), bootstraps
  Project/MVP/WorkItems idempotently, and drives WorkItem Flow.
- `aido status` loads it and reads the persisted state it points at —
  no provider calls, no mutation.

See the main [`README.md`](../README.md) for CLI usage examples.

## Engine/library boundary (P13.5, P13.6)

`ai-dev-orchestrator` is an **engine/library**; the legacy `orchestrator.
cli` module documented above is its own historical, internal-only
surface (P13.6: no longer installed as a console script by this
distribution at all) — never the project's product surface, and never
its only consumer. **AIDO** (the embedding product/application — AIDO
Code, which now owns the real `aido` command) drives the same engine
through
`orchestrator.engine.OrchestratorEngine` instead, and is never required
to describe its worker pool through `aido.yaml`/`workers.registry`: it
constructs its own `orchestrator.worker_registry.WorkerRegistry` and
injects it directly —

```python
engine = OrchestratorEngine.open(
    config_path,               # or OrchestratorEngine(config, ...) for a
                                # ProjectConfig already built in Python
    worker_registry=registry,  # caller-built WorkerRegistry — never read
                                # from a `workers.registry` path when given
)
```

The engine never owns AIDO's own configuration/roadmap documents or
worker choices — it receives a typed plan (`ProjectConfig`, this
document's own schema, built either by `ProjectConfig.load(aido.yaml)`
or directly via its plain Python constructor) and a `WorkerRegistry`,
and executes them. `WorkerSelector` remains the sole owner of *which*
worker is picked; injection only supplies *what's available*. See
"Worker registry reference" below, and `ROADMAP.md` §13, sub-section
P13.5, for the full rationale.

## Guided project bootstrap (P1.1)

```bash
cd ~/projects
aido init . roadmaplab
cd roadmaplab
# Edit README.md, ROADMAP.md and aido.yaml.
aido validate
git add README.md ROADMAP.md aido.yaml
git commit -m "Define initial project"
aido run
```

Two positional arguments mean `<parent-path> <project-name>`. Paths expand
`~` and resolve to an absolute location. The name must be one directory
component (starting with a letter or digit; letters, digits, underscores,
spaces, dots and hyphens afterward; no trailing dot/space). Paths such as
`../evil` or `/tmp/evil`, and symlink targets, are rejected. The target may
be absent or empty; a nonempty target is refused without changing files.
There is no force option.

Created files:

- `README.md`: human project context, purpose, users and constraints.
- `ROADMAP.md`: versioned product vision and first milestone.
- `aido.yaml`: executable current MVP, with objective, acceptance criteria,
  WorkItems and QA TODOs. Uses the existing init template, sanitized project
  id, supplied human name, `workspace: "."`, and standard permissions by default.
- `.gitignore`: local Ralph runtime noise (`/.ralph/`) and environment files.

These roles remain separate. No roadmap parsing, automatic synchronization,
or invented product acceptance criteria. The user edits all three documents
before explicitly starting `aido run`.

The existing registry lookup is shared by both modes: explicit
`--workers-registry`, otherwise the caller's `config/workers.yaml` if present,
otherwise `$XDG_CONFIG_HOME/ai-dev-orchestrator/workers.yaml` (default
`~/.config/ai-dev-orchestrator/workers.yaml`). A missing user registry is
materialized from the packaged default; existing registries are reused.
Only a reference is written to the project, never another registry copy.

After writing files, init detects Git in PATH and executes, without a shell:

```bash
git init -b main
git add .
git commit -m "Initialize AIDO project"
```

No remote, push, Git installation or global Git configuration change occurs.
If Git is missing, or any step fails (including an unconfigured identity),
the scaffold is retained and exit status is nonzero. Output identifies the
failure and gives the absolute `cd` plus all three commands above for manual
completion, followed by onboarding. Configure your Git identity if requested,
then retry the commit. `validate` also provides Git setup instructions when
the existing workspace is not a Git repository, through a typed
`MissingGitWorkspaceError` from the existing configuration loader.

Init is entirely local: no provider resolution, LLM, probe, worker, Ralph,
runtime construction or persisted Project/MVP/WorkItem. Only an explicit
`aido run` starts development.

### Historical configuration-only mode

`aido init` or `aido init <config-path>` still writes only the configuration,
refusing to overwrite it. Existing options remain supported, including
`--workspace` and `--project-name`. In complete bootstrap mode these two
options are rejected because the positional arguments define them;
`--project-id`, `--workers-registry` and `--permission-mode` remain available.

## Schema v1

```yaml
schema_version: 1

project:
  id: my-project              # stable identifier, used for the default state dir
  name: My Project
  workspace: .                 # path to the governed Git repo; usually "." when
                                # aido.yaml lives at the project's own root
  # state_dir: ~/.local/state/ai-dev-orchestrator/projects/my-project/
  #   optional — see "Persistent state" below; this is the computed default

# Optional (P13.5) and legacy — a caller of the modern engine API
# (OrchestratorEngine/ProjectRuntime) injects its own WorkerRegistry
# instead and omits this section entirely. When present, still eagerly
# validated exactly as before. See "Engine/library boundary" above.
workers:
  registry: ../config/workers.yaml   # a WorkerRegistry-shaped YAML file — a
                                       # REFERENCE, never inline Worker definitions

execution:
  permission_mode: standard    # standard | unrestricted — see below

git:
  base_branch: main            # optional, defaults to "main"

mvp:
  id: mvp-1
  objective: "Ship the first governed feature."
  acceptance_criteria:
    - "The feature works as described."

work_items:
  - id: wi-1
    title: "Implement the thing"
    required_capabilities: [development]
    dependencies: []            # other work_items' ids in this same file
    acceptance_criteria:
      - "..."

qa:
  - id: qa-pytest
    kind: unit_test              # any orchestrator.validation.ValidationKind value
    argv: ["pytest", "-q"]       # always a list — never a shell string
    timeout_seconds: 300
    required: true

# Optional. Repo-relative paths, explicit only — see "Protected test
# paths" below. Omit entirely (or leave as []) to opt out.
qa_protected_paths:
  - tests/test_core_contract.py
```

Schema v1 is deliberately minimal (KISS/YAGNI): exactly **one** configured
MVP. Multi-MVP configuration is not built here — if that becomes a real
need, it is a new, explicitly-voted schema version, not a silent
extension of this one.

## Safe example

See [`examples/aido.yaml`](../examples/aido.yaml) — a tracked, safe
example with no machine-specific absolute paths, using
`permission_mode: standard`. `unrestricted` is never the example/default
recommendation; a user changes it intentionally, in their own copy, after
reading the security warning below.

## Path semantics

The directory containing `aido.yaml` is the config's base directory.
Every relative path in the file (`project.workspace`, `project.state_dir`,
`workers.registry`) resolves **relative to that directory**, never the
caller's current working directory. `~` is expanded. `project.workspace`
must resolve to an existing directory that is a Git repository.

## Persistent state

If `project.state_dir` is omitted, it defaults deterministically to:

```
~/.local/state/ai-dev-orchestrator/projects/<project.id>/
```

— the same convention already used elsewhere in this project
(ROADMAP.md §8). This default is never `/tmp` (a real incident on the
Morpion Web 3D pilot showed why: `/tmp` is generally cleared on reboot,
which is not the same guarantee as simple inter-process survival). If
`state_dir` is explicitly configured, that value is respected instead.

## Worker registry reference

`workers` is now **optional** (P13.5): a config with no `workers:`
section is a fully valid, modern `ProjectConfig` — see "Engine/library
boundary" above. When present (the legacy, file-based path, still fully
supported and tested), `workers.registry` is a **path to an existing**
`config/workers.yaml`-shaped file, loaded and validated eagerly (via
`orchestrator.worker_registry.WorkerRegistry.load`) as part of
`ProjectConfig.load()` — a bad reference fails configuration loading
itself, not some later step. The project configuration **never**
redefines individual `Worker` objects inline; worker pool configuration
and target-project configuration are different concerns, and schema v1
does not duplicate `config/workers.yaml`'s role.

`ProjectConfig.load_worker_registry()` re-loads this legacy path; it
raises `NoWorkerRegistryConfiguredError` (a `WorkerRegistryError`
subclass) when no `workers:` section exists — the modern engine API
never calls it in that case, since the caller injects its own
`WorkerRegistry` directly into `OrchestratorEngine`/`ProjectRuntime`
instead.

## QA commands

`qa` entries are exactly `orchestrator.validation.ValidationCommand`/
`ValidationKind` — the same structured, deterministic validation command
shape used elsewhere in this project, never a second, parallel concept.
`argv` is always a list of strings, executed with `shell=False` — never a
shell string.

## Protected test paths

`qa_protected_paths` (optional, defaults to `[]`) is a plain list of
repo-relative file paths a project wants protected from silent weakening
(`src/orchestrator/qa_protection.py`, Slice 22): once populated, DEV
A/DEV B/DEV FIX can still change these files, but any content change
(modification or deletion, hashed with SHA-256 against the exact
`base_sha` each WorkItem started from) is treated as an unauthorized
protected-test change and fails QA
(`orchestrator.qa.evaluate_qa_verdict`'s `unauthorized_protected_change`)
— never a silent `PASS` on a test that was quietly weakened or removed to
make QA pass.

This is deliberately **explicit only**, never auto-detected from a
`tests/`-style naming convention: different ecosystems (Python, JS, Go,
...) disagree on what a "test file" is, and guessing would either miss
real regression tests or protect unrelated files by accident. A project
lists exactly the files it wants protected. Omitting this field (or an
empty list) opts a project out entirely — the same behavior as before
this field existed.

`aido run` (`ProjectRuntime.bootstrap()`) always passes this list
through to the real `MVPManager` it constructs; there is no separate
"protected QA" engine or code path — this is the existing
`qa_protection.py` mechanism, wired to the one real `aido run` path.

## Execution permission mode

`execution.permission_mode` is `orchestrator.execution_policy.ExecutionPermissionMode`:

- **`standard`** — AIDO does **not** request unrestricted/bypass
  execution. Where the backend supports an explicit safe/default
  permission mechanism, AIDO explicitly requests it — never silently
  omitted, never left to whatever the host machine happens to already be
  configured as.
- **`unrestricted`** — AIDO explicitly requests verified unattended/
  bypass-permission ("YOLO") execution, where the backend honestly
  supports it.

**Security warning:** `unrestricted` mode can let a worker process run
shell commands and touch the filesystem broadly, as the current OS user,
without per-action approval. Only use it in a workspace you trust and
that is appropriately isolated (a disposable clone/VM/container is safer
than your primary machine). It is always explicit opt-in — never a
default, never inferred from the host machine's own configuration, never
silently upgraded from `standard`.

Neither mode causes AIDO to store or manage a provider credential —
authentication stays entirely with the provider CLI/environment. This is
policy, never a secret.

### Verified backend mapping

The generic mode is translated into real CLI arguments exclusively at the
execution/backend boundary (`orchestrator.ralph_execution_engine`) —
`MVPManager`/`WorkerSelector` know nothing about any of this. Verified
against the actually installed CLIs (never invented); each row cites the
exact flags used.

| Backend | CLI/version verified | `standard` (verified) | `unrestricted` (verified) | Limitation |
|---|---|---|---|---|
| `claude_code` | Claude Code 2.1.277 | `--permission-mode manual --permission-prompts none` — explicit approval-required mode, with a would-be-prompted action denied automatically (deterministic, never hangs waiting for an answer nothing can give) | `--dangerously-skip-permissions` — the single dedicated bypass flag | None known |
| `codex` | codex-cli 0.157.0 | `--sandbox workspace-write` — restrictive sandbox; `--ask-for-approval` is not passed because `codex exec` (the non-interactive subcommand this engine always invokes) rejects it outright as an unrecognized argument, exit code 2 — confirmed against a real failed execution (GitLabPluginRoadmap WI-S0A-01) and reproduced directly against the installed CLI. `codex exec` never has an interactive user to prompt, so the flag was already redundant for it; `--sandbox workspace-write` alone is what keeps `standard` non-`danger-full-access` | `--dangerously-bypass-approvals-and-sandbox` — the single dedicated bypass flag, unaffected by this change | The 0.155.0-era `--ask-for-approval never` combination documented previously was verified against the top-level, interactive `codex` command, not `codex exec` — a real upstream CLI divergence between the two, not a local config drift |
| `vibe` | Vibe 2.25.4 | `--trust --agent ask` — the real, documented `ask` builtin agent requires approval per tool call | `--trust --auto-approve` (equivalently `--yolo`) — "Approves all tool calls without prompting" | `--trust` is **unconditional** for every mode — VERIFIED to only skip the one-time directory-trust prompt ("Use this for non-interactive automation"), never tool-call approval; omitting it would make even `standard` hang on a prompt nothing can answer. In non-interactive (`-p`) mode with the `ask` agent, an approval that cannot be answered is expected to fail/deny rather than silently proceed — an honest `standard` limitation, not a bug |

If a backend cannot honestly represent a requested mode, `RalphExecutionEngine`
raises `UnsupportedPermissionModeError` **before** any subprocess is
launched — never a silent downgrade, upgrade, or ignore.

An `ExecutionPermissionMode`-unconfigured `RalphExecutionEngine` (the
default, for backward compatibility with every existing caller that
predates P12) adds **no** permission-related arguments at all — identical
to this project's behavior before this feature. New callers should always
pass an explicit mode.

## Execution audit

The requesting `RalphExecutionEngine`'s configured permission mode is
persisted on every new `ExecutionRecord` (`ExecutionStore`, P12) — one
engine instance owns one explicit policy, applied uniformly to every
execution it runs; this is never set per-WorkItem. A v0.1.1-era database
created before this field existed is migrated in place, idempotently,
by adding the column if missing; its historical rows decode honestly as
`permission_mode=None` — never fabricated as `"standard"`/`"unrestricted"`
after the fact.

## No secrets

`aido.yaml` never contains API keys, tokens, passwords, or provider
credentials — authentication remains entirely the provider CLI's own
concern, exactly like `config/workers.yaml`. Loading rejects a handful of
obviously-wrong key names (`api_key`, `token`, `secret`, `password`,
`credential`, ...) anywhere in the file as a cheap guard-rail.

**DeepSeek/Kimi exception, stated explicitly:** unlike Claude Code/Codex/
Vibe (authenticated entirely outside this project, no secret ever handled
here), the `deepseek`/`kimi` providers are reached through a real API key
(`DEEPSEEK_API_KEY`/`KIMI_API_KEY`), still never in `aido.yaml`/
`config/workers.yaml`, always read from the process environment only, at
the moment `orchestrator.providers.deepseek_adapter`/`kimi_adapter` is
actually invoked for a provider an enabled worker requires. See
`ROADMAP.md` §7/§13.
