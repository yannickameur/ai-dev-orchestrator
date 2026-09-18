# Project configuration (`aido.yaml`) — P12 foundation

Source of truth for `orchestrator.project_config.ProjectConfig` — the
public project configuration format approved as part of the
productisation/onboarding cycle (ROADMAP.md §13). It solves two current
architectural problems: (A) external projects were only describable/
wired through a custom Python harness; (B) worker execution permission
mode depended entirely on machine-local CLI configuration rather than an
explicit, project-declared policy.

This document describes the configuration format and its loader
(`ProjectConfig.load(path)`), a typed, reusable API. The public `aido`
CLI (P1, `src/orchestrator/cli.py`) is the real consumer:

- `aido init` writes a starting `aido.yaml`.
- `aido validate` loads/validates one and prints its facts — no side effects.
- `aido run` loads it, composes the real runtime
  (`orchestrator.project_runtime.ProjectRuntime`), bootstraps
  Project/MVP/WorkItems idempotently, and drives WorkItem Flow.
- `aido status` loads it and reads the persisted state it points at —
  no provider calls, no mutation.

See the main [`README.md`](../README.md) for CLI usage examples.

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

`workers.registry` is a **path to an existing** `config/workers.yaml`-
shaped file, loaded and validated eagerly (via
`orchestrator.worker_registry.WorkerRegistry.load`) as part of
`ProjectConfig.load()` — a bad reference fails configuration loading
itself, not some later step. The project configuration **never**
redefines individual `Worker` objects inline; worker pool configuration
and target-project configuration are different concerns, and schema v1
does not duplicate `config/workers.yaml`'s role.

## QA commands

`qa` entries are exactly `orchestrator.validation.ValidationCommand`/
`ValidationKind` — the same structured, deterministic validation command
shape used elsewhere in this project, never a second, parallel concept.
`argv` is always a list of strings, executed with `shell=False` — never a
shell string.

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
| `codex` | codex-cli 0.155.0 | `--sandbox workspace-write --ask-for-approval never` — restrictive sandbox, no interactive escalation (a denied action is returned to the model immediately, not paused) | `--dangerously-bypass-approvals-and-sandbox` — the single dedicated bypass flag | Codex's own `--ask-for-approval` only exposes `on-request`/`never` in this version — there is no third "always ask" value to combine with a sandbox the way Claude's `--permission-prompts none` does; `never` + `workspace-write` is the closest verified restrictive-but-non-hanging equivalent |
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
