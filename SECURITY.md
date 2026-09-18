# Security Policy

## Reporting a vulnerability

Please do not open a public GitHub issue for a security vulnerability,
a leaked credential, or any other security-sensitive report — public
issues are visible to everyone before a fix exists.

Preferred: use **GitHub's private vulnerability reporting** for this
repository ("Security" tab → "Report a vulnerability"), if it has been
enabled by the repository owner. This project has not preconfigured
that feature from within the repository itself — if it is not yet
enabled when you look, please ask the repository owner to enable
Security Advisories / private vulnerability reporting, or check the
repository's "Security" tab for current instructions.

Please include, where possible:
- affected file(s)/component(s) and, if known, the commit/version;
- a minimal reproduction;
- the potential impact.

## Supported versions

Only the current public release line is supported. Older pre-release
history (before the first public release) is not maintained.

## Scope

This project orchestrates real developer-agent workers (Claude Code,
Codex CLI, Mistral Vibe) that execute against real Git repositories.
Security reports in scope include, non-exhaustively:

- exposure of provider credentials (Anthropic/OpenAI/Mistral
  authentication) through logs, reports, or persisted state;
- arbitrary or unintended command execution;
- prompt/tool injection that causes a worker to act outside a
  WorkItem's governed scope, or against a repository it should not
  touch;
- a Git governance bypass (an ungoverned commit/merge onto a protected
  branch, evidence attributed to the wrong SHA, etc.);
- arbitrary filesystem path access outside the intended governed
  workspace;
- secret leakage into generated reports, QA output, or Ralph event
  logs;
- unsafe merge/branch behavior (e.g. a non-fast-forward merge accepted
  as if it were fast-forward, a force-push, history rewriting).

## Out of scope

General bugs with no security impact should go through the normal
issue tracker instead.
