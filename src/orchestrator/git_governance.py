"""Git/PR/merge governance — the orchestrator owns the code lifecycle (Slice 20).

This module answers exactly one question per WorkItem: "on what governed
branch is this work happening, what is its base/current SHA, and is it
currently safe to merge?" Ralph remains the execution engine (it still
runs the worker and may still auto-commit — this module never replaces
that mechanism); workers still work in Git — but they never decide when a
governed branch is created, which branch is protected, when to merge, or
whether their own work is mergeable. Those are exclusively this module's
decisions, and every one of them is deterministic and durable — never an
LLM call.

Design invariants:

- No Git reimplementation: every mutation goes through the real ``git``
  CLI via ``subprocess`` with an explicit argv list (never ``shell=True``,
  never a string-concatenated command). See ``LocalGitWorkspace``.
- FAIL CLOSED, always: a dirty tracked working tree, a missing expected
  branch, a HEAD that drifted from what the store expects with no known
  execution to explain it, or a base branch that has advanced
  incompatibly with the work branch — every one of these raises, never
  guesses "probably fine". See the module's exception hierarchy.
- NEVER DESTRUCTIVE BY DEFAULT: no automatic ``git stash``, ``reset
  --hard``, ``clean -fd``, rebase, or force-push anywhere in this module.
  The default merge strategy is fast-forward-only, which by construction
  can never rewrite or discard a commit — a merge that is not a clean
  fast-forward is refused, never resolved automatically.
- SHA-BOUND EVIDENCE: quality-gate and review evidence is only valid for
  the exact ``head_sha`` it was produced against. A new commit on the work
  branch after either silently invalidates the old evidence for merge
  purposes — ``compute_merge_eligibility`` re-checks the SHA every time,
  never trusting "it passed before".
- MERGE ELIGIBILITY IS NEVER AN LLM DECISION: ``compute_merge_eligibility``
  is a pure function of already-persisted facts (WorkItem status, the
  governed branch record, the latest quality-gate result, the latest
  review) — same inputs, same output, always.
- RESTART-SAFE: after a cold restart, ``GitGovernanceService.reconcile``
  reconstructs state from ``GitWorkItemStore`` plus the real repository —
  never from "whatever branch happens to be checked out right now".
- The product never hardcodes a contribution identity (author/committer)
  anywhere in this module — that is this project's *own* contribution
  rule for commits to ai-dev-orchestrator itself, not something the
  orchestrator imposes on a target repository it governs. Every git
  operation here relies on the target repository's own configuration (or
  a future configurable policy), never a literal name/email.
- No worktree manager: this slice operates on a single governed branch
  inside the workspace ``RalphExecutionEngine`` already uses (Ralph's own
  parallel-loop worktrees, if ever used, are a distinct, unrelated
  mechanism this module does not touch or duplicate).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

DEFAULT_GIT_BINARY = "git"
DEFAULT_BASE_BRANCH = "main"
_WORK_BRANCH_PREFIX = "work/"


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_aware(moment: datetime, *, field_name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {moment!r}")


def _default_id_factory() -> str:
    return uuid.uuid4().hex


# --- branch naming ----------------------------------------------------------

_UNSAFE_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_branch_component(raw: str) -> str:
    """Turns an arbitrary WorkItem id into a safe, deterministic branch component.

    Deterministic (same id -> same output, always — a resumed/retried
    WorkItem must reuse the exact same branch, never recreate a new one),
    collision-resistant enough for one project's WorkItem ids (they are
    already unique), and satisfies git's own ref-name rules (no
    whitespace/control chars, no "..", cannot start with "-" or end with
    "." or "/", cannot be empty).
    """
    candidate = _UNSAFE_BRANCH_CHARS.sub("-", raw.strip())
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-.")
    while ".." in candidate:
        candidate = candidate.replace("..", ".")
    if not candidate:
        candidate = "item"
    return candidate.lower()


def work_branch_name(work_item_id: str) -> str:
    return f"{_WORK_BRANCH_PREFIX}{sanitize_branch_component(work_item_id)}"


# --- LocalGitWorkspace: thin, explicit-argv wrapper around the git CLI ------


class GitCommandError(Exception):
    """Raised when a git subprocess call fails unexpectedly (non-zero exit,
    for a call that is not itself the "does this exist?" kind of probe)."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git command failed ({returncode}): {list(argv)!r}\n{stderr}")
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class NotAGitRepositoryError(Exception):
    """Raised when the configured repository path is not a git working tree."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"not a git repository: {path}")
        self.path = path


@dataclass(frozen=True, slots=True)
class WorkingTreeStatus:
    """The result of inspecting the working tree once, in one ``git status`` call.

    ``tracked_dirty`` (modified/added/deleted/renamed tracked files) is
    what governance actually fails closed on. ``untracked`` is reported
    for visibility only — untracked files (build artifacts, freshly
    created source not yet added) are normal before a worker has even
    started and are never, by themselves, a reason to refuse preparing a
    governed branch; they are never auto-stashed, auto-added, or
    auto-cleaned either way.
    """

    tracked_dirty: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()

    @property
    def is_clean_for_governance(self) -> bool:
        return not self.tracked_dirty


class LocalGitWorkspace:
    """Runs real ``git`` subprocess commands against one repository path.

    Every method is a small, explicit operation with an explicit argv list
    — never ``shell=True``, never a string-built command. This class knows
    nothing about WorkItems, policies, or merge eligibility; it only
    answers factual questions about the repository and performs the
    specific, narrow mutations governance needs (branch create/checkout,
    ff-only merge). It never stashes, resets, or cleans anything.
    """

    def __init__(self, repository_path: str | Path, *, git_binary: str = DEFAULT_GIT_BINARY) -> None:
        self._repository_path = Path(repository_path)
        self._git_binary = git_binary

    @property
    def repository_path(self) -> Path:
        return self._repository_path

    def _run(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [self._git_binary, *args],
            cwd=str(self._repository_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise GitCommandError(args, result.returncode, result.stderr)
        return result

    def is_git_repository(self) -> bool:
        result = self._run(["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _require_repository(self) -> None:
        if not self.is_git_repository():
            raise NotAGitRepositoryError(self._repository_path)

    def current_branch(self) -> str:
        self._require_repository()
        result = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        return result.stdout.strip()

    def head_sha(self, ref: str = "HEAD") -> str:
        self._require_repository()
        result = self._run(["rev-parse", ref])
        return result.stdout.strip()

    def try_rev_parse(self, ref: str) -> str | None:
        """Like ``head_sha`` but returns ``None`` instead of raising for an
        unknown ref — the primitive missing-branch/drift detection needs."""
        self._require_repository()
        result = self._run(["rev-parse", "--verify", "--quiet", ref], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def branch_exists(self, name: str) -> bool:
        self._require_repository()
        result = self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
        return result.returncode == 0

    def working_tree_status(self) -> WorkingTreeStatus:
        self._require_repository()
        result = self._run(["status", "--porcelain=v1"])
        tracked: list[str] = []
        untracked: list[str] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            code, _, path = line.partition(" ")
            # Normalize: porcelain lines are "XY <path>" with a 2-char code;
            # partition on the first space above is imprecise for codes
            # containing a space (e.g. " M"), so re-derive from fixed width.
            code = line[:2]
            path = line[3:]
            if code == "??":
                untracked.append(path)
            else:
                tracked.append(path)
        return WorkingTreeStatus(tracked_dirty=tuple(tracked), untracked=tuple(untracked))

    def create_branch(self, name: str, *, from_ref: str) -> None:
        self._require_repository()
        self._run(["branch", name, from_ref])

    def switch(self, name: str) -> None:
        self._require_repository()
        self._run(["switch", name])

    def is_ancestor(self, ancestor_ref: str, descendant_ref: str) -> bool:
        self._require_repository()
        result = self._run(["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], check=False)
        if result.returncode not in (0, 1):
            raise GitCommandError(
                ["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], result.returncode, result.stderr
            )
        return result.returncode == 0

    def merge_ff_only(self, source_branch: str) -> str:
        """Merges ``source_branch`` into the currently checked-out branch,
        fast-forward only. Never rebases, never forces, never resolves a
        conflict — a non-fast-forward situation raises ``MergeRefusedError``
        and leaves the repository exactly as it was (``git merge --ff-only``
        itself never partially applies)."""
        self._require_repository()
        result = self._run(["merge", "--ff-only", source_branch], check=False)
        if result.returncode != 0:
            raise MergeRefusedError(
                source_branch, reason=f"git merge --ff-only failed: {result.stderr.strip()}"
            )
        return self.head_sha()


# --- policy -------------------------------------------------------------


class MergeStrategy(str, Enum):
    FF_ONLY = "ff_only"


@dataclass(frozen=True, slots=True)
class GitGovernancePolicy:
    """Conservative-by-default governance rules — intentionally small.

    ``merge_strategy`` defaults to fast-forward-only: the current
    architecture is a single sequential worker per WorkItem producing a
    linear commit chain on its own branch, so a clean fast-forward is
    always achievable when nothing else touched ``base_branch`` meanwhile
    — no merge commit, no rewritten history, a divergence is simply
    refused rather than resolved automatically. ``auto_merge`` defaults to
    ``False`` so governance itself is validated (and observable via
    ``MERGE_READY``) before the orchestrator is ever allowed to advance a
    protected branch on its own.
    """

    base_branch: str = DEFAULT_BASE_BRANCH
    protected_branches: frozenset[str] = frozenset({DEFAULT_BASE_BRANCH})
    require_clean_worktree: bool = True
    require_review: bool = True
    require_required_gates: bool = True
    merge_strategy: MergeStrategy = MergeStrategy.FF_ONLY
    auto_merge: bool = False
    remote_pr_enabled: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.base_branch, field_name="GitGovernancePolicy.base_branch")
        object.__setattr__(self, "protected_branches", frozenset(self.protected_branches) | {self.base_branch})
        if not isinstance(self.merge_strategy, MergeStrategy):
            raise TypeError(
                f"GitGovernancePolicy.merge_strategy must be a MergeStrategy, got {type(self.merge_strategy)!r}"
            )


# --- durable state --------------------------------------------------------


class GitWorkItemStatus(str, Enum):
    """A governed WorkItem's git lifecycle — deliberately small.

    PREPARED: branch created/reused, base_sha captured, no development
    execution has landed a commit yet.
    IN_PROGRESS: at least one head has been captured on the work branch;
    also the state a WorkItem *returns to* if a new commit lands after it
    had reached MERGE_READY (e.g. a rework cycle) — new evidence always
    invalidates old merge-readiness.
    MERGE_READY: ``compute_merge_eligibility`` currently says PASS for the
    exact current head.
    MERGED: the fast-forward-only merge into ``base_branch`` has happened
    — terminal, permanent history.
    CONFLICT: a divergence (base branch advanced incompatibly with the
    work branch) was detected — never auto-resolved; recoverable only by
    an explicit future action (out of scope for this slice), so a fresh
    head capture is still allowed to move it back to IN_PROGRESS.
    FAILED: an unexpected git-level error occurred.
    ABANDONED: an explicit terminal give-up (e.g. the WorkItem itself is
    permanently BLOCKED without ever merging) — never reached implicitly.
    """

    PREPARED = "prepared"
    IN_PROGRESS = "in_progress"
    MERGE_READY = "merge_ready"
    MERGED = "merged"
    CONFLICT = "conflict"
    FAILED = "failed"
    ABANDONED = "abandoned"


_GIT_WORK_ITEM_TRANSITIONS: dict[GitWorkItemStatus, frozenset[GitWorkItemStatus]] = {
    GitWorkItemStatus.PREPARED: frozenset(
        {GitWorkItemStatus.IN_PROGRESS, GitWorkItemStatus.FAILED, GitWorkItemStatus.ABANDONED}
    ),
    GitWorkItemStatus.IN_PROGRESS: frozenset(
        {
            GitWorkItemStatus.IN_PROGRESS,
            GitWorkItemStatus.MERGE_READY,
            GitWorkItemStatus.CONFLICT,
            GitWorkItemStatus.FAILED,
            GitWorkItemStatus.ABANDONED,
        }
    ),
    GitWorkItemStatus.MERGE_READY: frozenset(
        {
            GitWorkItemStatus.IN_PROGRESS,  # a new commit landed after merge-readiness was computed
            GitWorkItemStatus.MERGED,
            GitWorkItemStatus.CONFLICT,
            GitWorkItemStatus.FAILED,
            GitWorkItemStatus.ABANDONED,
        }
    ),
    GitWorkItemStatus.CONFLICT: frozenset(
        {GitWorkItemStatus.IN_PROGRESS, GitWorkItemStatus.FAILED, GitWorkItemStatus.ABANDONED}
    ),
    GitWorkItemStatus.MERGED: frozenset(),
    GitWorkItemStatus.FAILED: frozenset({GitWorkItemStatus.ABANDONED}),
    GitWorkItemStatus.ABANDONED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class GitWorkItemRecord:
    """The durable, current git state of one governed WorkItem."""

    git_work_id: str
    project_id: str
    mvp_id: str
    work_item_id: str
    repository_path: str
    base_branch: str
    work_branch: str
    base_sha: str
    status: GitWorkItemStatus
    created_at: datetime
    updated_at: datetime
    current_head_sha: str | None = None
    merged_at: datetime | None = None
    merged_sha: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    conflict_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "git_work_id", "project_id", "mvp_id", "work_item_id", "repository_path",
            "base_branch", "work_branch", "base_sha",
        ):
            _require_non_empty_str(getattr(self, name), field_name=f"GitWorkItemRecord.{name}")
        if not isinstance(self.status, GitWorkItemStatus):
            raise TypeError(f"GitWorkItemRecord.status must be a GitWorkItemStatus, got {type(self.status)!r}")
        _require_aware(self.created_at, field_name="GitWorkItemRecord.created_at")
        _require_aware(self.updated_at, field_name="GitWorkItemRecord.updated_at")
        if self.merged_at is not None:
            _require_aware(self.merged_at, field_name="GitWorkItemRecord.merged_at")
        for name in ("current_head_sha", "merged_sha", "pull_request_url", "conflict_reason"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, field_name=f"GitWorkItemRecord.{name}")
        if self.pull_request_number is not None and (
            not isinstance(self.pull_request_number, int) or isinstance(self.pull_request_number, bool)
        ):
            raise TypeError("GitWorkItemRecord.pull_request_number must be an int or None")


class GitGovernanceError(Exception):
    """Base for git-governance domain errors."""


class UnknownGitWorkItemError(GitGovernanceError):
    def __init__(self, work_item_id: str) -> None:
        super().__init__(f"no governed git record for work item: {work_item_id!r}")
        self.work_item_id = work_item_id


class DuplicateGitWorkItemError(GitGovernanceError):
    def __init__(self, work_item_id: str) -> None:
        super().__init__(f"governed git record already exists for work item: {work_item_id!r}")
        self.work_item_id = work_item_id


class InvalidGitWorkItemTransitionError(GitGovernanceError):
    def __init__(self, work_item_id: str, current: GitWorkItemStatus, attempted: GitWorkItemStatus) -> None:
        super().__init__(
            f"governed work item {work_item_id!r} cannot move from {current.value!r} to {attempted.value!r}"
        )
        self.work_item_id = work_item_id
        self.current = current
        self.attempted = attempted


class DirtyWorkingTreeError(GitGovernanceError):
    """Raised when a governed branch cannot be prepared because tracked
    files are modified — never auto-stashed, reset, or cleaned."""

    def __init__(self, dirty_files: Sequence[str]) -> None:
        super().__init__(f"working tree has modified tracked files: {list(dirty_files)!r}")
        self.dirty_files = tuple(dirty_files)


class ProtectedBranchError(GitGovernanceError):
    """Raised if a governed WorkItem's worker would otherwise run directly
    on a protected branch — this must never happen."""

    def __init__(self, branch: str) -> None:
        super().__init__(f"refusing to run a governed WorkItem directly on protected branch: {branch!r}")
        self.branch = branch


class GitBranchMissingError(GitGovernanceError):
    """Raised when the store expects a work branch that no longer exists.

    Never silently recreated (that could happen at the wrong SHA) — a
    human/business decision is required.
    """

    def __init__(self, work_item_id: str, work_branch: str) -> None:
        super().__init__(f"work branch {work_branch!r} for {work_item_id!r} is missing")
        self.work_item_id = work_item_id
        self.work_branch = work_branch


class GitHeadDriftError(GitGovernanceError):
    """Raised when the branch's real HEAD does not match what the store
    expects, and no known persisted execution explains the difference."""

    def __init__(self, work_item_id: str, expected_sha: str | None, actual_sha: str) -> None:
        super().__init__(
            f"head drift for {work_item_id!r}: store expects {expected_sha!r}, branch is at {actual_sha!r}"
        )
        self.work_item_id = work_item_id
        self.expected_sha = expected_sha
        self.actual_sha = actual_sha


class MergeRefusedError(GitGovernanceError):
    """Raised when a fast-forward-only merge cannot be performed cleanly —
    the base branch has diverged from the work branch. Never auto-resolved
    (no rebase, no force, no automatic conflict resolution)."""

    def __init__(self, work_branch: str, *, reason: str) -> None:
        super().__init__(f"merge refused for {work_branch!r}: {reason}")
        self.work_branch = work_branch
        self.reason = reason


class NotMergeableError(GitGovernanceError):
    """Raised by ``merge()`` when called while eligibility is not currently PASS."""

    def __init__(self, work_item_id: str, reason: str) -> None:
        super().__init__(f"work item {work_item_id!r} is not mergeable: {reason}")
        self.work_item_id = work_item_id
        self.reason = reason


# --- GitWorkItemStore: sqlite3, mutable current-state row + insert-only audit log --


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS git_work_items (
    work_item_id TEXT PRIMARY KEY,
    git_work_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    mvp_id TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    work_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    current_head_sha TEXT,
    merged_at TEXT,
    merged_sha TEXT,
    pull_request_number INTEGER,
    pull_request_url TEXT,
    conflict_reason TEXT
);
CREATE TABLE IF NOT EXISTS git_governance_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_COLUMNS = (
    "work_item_id", "git_work_id", "project_id", "mvp_id", "repository_path",
    "base_branch", "work_branch", "base_sha", "status", "created_at", "updated_at",
    "current_head_sha", "merged_at", "merged_sha", "pull_request_number",
    "pull_request_url", "conflict_reason",
)


def _encode(record: GitWorkItemRecord) -> tuple:
    return (
        record.work_item_id, record.git_work_id, record.project_id, record.mvp_id,
        record.repository_path, record.base_branch, record.work_branch, record.base_sha,
        record.status.value, record.created_at.isoformat(), record.updated_at.isoformat(),
        record.current_head_sha,
        None if record.merged_at is None else record.merged_at.isoformat(),
        record.merged_sha, record.pull_request_number, record.pull_request_url,
        record.conflict_reason,
    )


def _decode_row(row: sqlite3.Row) -> GitWorkItemRecord:
    return GitWorkItemRecord(
        work_item_id=row["work_item_id"], git_work_id=row["git_work_id"],
        project_id=row["project_id"], mvp_id=row["mvp_id"],
        repository_path=row["repository_path"], base_branch=row["base_branch"],
        work_branch=row["work_branch"], base_sha=row["base_sha"],
        status=GitWorkItemStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        current_head_sha=row["current_head_sha"],
        merged_at=None if row["merged_at"] is None else datetime.fromisoformat(row["merged_at"]),
        merged_sha=row["merged_sha"], pull_request_number=row["pull_request_number"],
        pull_request_url=row["pull_request_url"], conflict_reason=row["conflict_reason"],
    )


class GitWorkItemStore:
    """Synchronous, sqlite3-backed store: one current-state row per WorkItem,
    plus an insert-only audit event log (``list_events``) — durable enough
    to answer, after a cold restart, exactly who/what prepared the branch,
    which head was reviewed/tested, why a merge was authorized, and what
    incident (if any) occurred."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_CREATE_TABLES_SQL)

    def close(self) -> None:
        self._conn.close()

    def create(self, record: GitWorkItemRecord) -> GitWorkItemRecord:
        try:
            with self._conn:
                self._conn.execute(
                    f"INSERT INTO git_work_items ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    _encode(record),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateGitWorkItemError(record.work_item_id) from exc
        self._log_event(record.work_item_id, "branch_prepared", f"work_branch={record.work_branch!r}")
        return record

    def get(self, work_item_id: str) -> GitWorkItemRecord:
        row = self._conn.execute(
            "SELECT * FROM git_work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise UnknownGitWorkItemError(work_item_id)
        return _decode_row(row)

    def try_get(self, work_item_id: str) -> GitWorkItemRecord | None:
        row = self._conn.execute(
            "SELECT * FROM git_work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        return None if row is None else _decode_row(row)

    def list_for_mvp(self, mvp_id: str) -> list[GitWorkItemRecord]:
        rows = self._conn.execute(
            "SELECT * FROM git_work_items WHERE mvp_id = ? ORDER BY work_item_id ASC", (mvp_id,)
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def update_head(self, work_item_id: str, head_sha: str) -> GitWorkItemRecord:
        current = self.get(work_item_id)
        new_status = (
            GitWorkItemStatus.IN_PROGRESS
            if current.status in (GitWorkItemStatus.PREPARED, GitWorkItemStatus.IN_PROGRESS,
                                   GitWorkItemStatus.MERGE_READY, GitWorkItemStatus.CONFLICT)
            else current.status
        )
        if new_status != current.status:
            self._transition(work_item_id, new_status)
        return self._update(
            work_item_id, current_head_sha=head_sha,
            event=("head_captured", f"current_head_sha={head_sha!r}"),
        )

    def mark_merge_ready(self, work_item_id: str) -> GitWorkItemRecord:
        self._transition(work_item_id, GitWorkItemStatus.MERGE_READY)
        return self._update(work_item_id, event=("merge_ready", "eligibility PASS"))

    def mark_merged(self, work_item_id: str, *, merged_sha: str, merged_at: datetime | None = None) -> GitWorkItemRecord:
        self._transition(work_item_id, GitWorkItemStatus.MERGED)
        return self._update(
            work_item_id, merged_sha=merged_sha, merged_at=merged_at or self._now(),
            event=("merged", f"merged_sha={merged_sha!r}"),
        )

    def mark_conflict(self, work_item_id: str, *, reason: str) -> GitWorkItemRecord:
        _require_non_empty_str(reason, field_name="reason")
        self._transition(work_item_id, GitWorkItemStatus.CONFLICT)
        return self._update(
            work_item_id, conflict_reason=reason, event=("merge_conflict", reason),
        )

    def mark_failed(self, work_item_id: str, *, reason: str) -> GitWorkItemRecord:
        _require_non_empty_str(reason, field_name="reason")
        self._transition(work_item_id, GitWorkItemStatus.FAILED)
        return self._update(work_item_id, conflict_reason=reason, event=("failed", reason))

    def mark_abandoned(self, work_item_id: str, *, reason: str) -> GitWorkItemRecord:
        _require_non_empty_str(reason, field_name="reason")
        self._transition(work_item_id, GitWorkItemStatus.ABANDONED)
        return self._update(work_item_id, event=("abandoned", reason))

    def record_pull_request(self, work_item_id: str, *, number: int | None, url: str) -> GitWorkItemRecord:
        self.get(work_item_id)  # raises UnknownGitWorkItemError if absent
        return self._update(
            work_item_id, pull_request_number=number, pull_request_url=url,
            event=("pull_request_created", f"number={number!r} url={url!r}"),
        )

    def list_events(self, work_item_id: str) -> list[tuple[str, str, datetime]]:
        """Insert-only audit trail: ``(event_type, detail, created_at)``, oldest first."""
        rows = self._conn.execute(
            "SELECT event_type, detail, created_at FROM git_governance_events "
            "WHERE work_item_id = ? ORDER BY row_id ASC",
            (work_item_id,),
        ).fetchall()
        return [(r["event_type"], r["detail"], datetime.fromisoformat(r["created_at"])) for r in rows]

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, field_name="GitWorkItemStore clock()")
        return value

    def _transition(self, work_item_id: str, new_status: GitWorkItemStatus) -> None:
        current = self.get(work_item_id)
        if new_status == current.status:
            return  # idempotent self-transition (e.g. repeated head capture)
        allowed = _GIT_WORK_ITEM_TRANSITIONS.get(current.status, frozenset())
        if new_status not in allowed:
            raise InvalidGitWorkItemTransitionError(work_item_id, current.status, new_status)
        with self._conn:
            self._conn.execute(
                "UPDATE git_work_items SET status = ?, updated_at = ? WHERE work_item_id = ?",
                (new_status.value, self._now().isoformat(), work_item_id),
            )

    def _update(
        self, work_item_id: str, *, event: tuple[str, str], **fields,
    ) -> GitWorkItemRecord:
        current = self.get(work_item_id)
        updated = replace(current, updated_at=self._now(), **fields)
        with self._conn:
            self._conn.execute(
                "UPDATE git_work_items SET updated_at = ?, current_head_sha = ?, merged_at = ?, "
                "merged_sha = ?, pull_request_number = ?, pull_request_url = ?, conflict_reason = ? "
                "WHERE work_item_id = ?",
                (
                    updated.updated_at.isoformat(), updated.current_head_sha,
                    None if updated.merged_at is None else updated.merged_at.isoformat(),
                    updated.merged_sha, updated.pull_request_number, updated.pull_request_url,
                    updated.conflict_reason, work_item_id,
                ),
            )
        self._log_event(work_item_id, event[0], event[1])
        return updated

    def _log_event(self, work_item_id: str, event_type: str, detail: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO git_governance_events (work_item_id, event_type, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (work_item_id, event_type, detail, self._now().isoformat()),
            )


# --- merge eligibility (deterministic, never LLM) --------------------------


@dataclass(frozen=True, slots=True)
class MergeEligibilityResult:
    """A deterministic, explainable merge/no-merge verdict for one exact head SHA."""

    work_item_id: str
    head_sha: str | None
    mergeable: bool
    reason: str | None
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty_str(self.work_item_id, field_name="MergeEligibilityResult.work_item_id")
        _require_aware(self.evaluated_at, field_name="MergeEligibilityResult.evaluated_at")
        if not self.mergeable and not self.reason:
            raise ValueError("MergeEligibilityResult.reason is required when mergeable is False")


# --- optional pull-request abstraction --------------------------------------


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    """A structured result from creating a remote pull/merge request —
    never itself a merge authorization (that stays exclusively
    ``compute_merge_eligibility``'s decision)."""

    provider: str
    number: int | None
    url: str
    head_branch: str
    base_branch: str
    head_sha: str
    state: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("provider", "url", "head_branch", "base_branch", "head_sha", "state"):
            _require_non_empty_str(getattr(self, name), field_name=f"PullRequestRecord.{name}")
        _require_aware(self.created_at, field_name="PullRequestRecord.created_at")


GhSubprocessRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess"]


def _default_gh_subprocess_runner(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([*argv], cwd=str(cwd), capture_output=True, text=True, timeout=30)


class PullRequestPublisher:
    """Abstract: creates a remote pull request for a governed work branch.

    A PR existing is purely informational for humans — it must never be
    read anywhere as merge authorization; only
    ``GitGovernanceService.compute_merge_eligibility``/``merge`` decide
    that, and neither ever consults a PR's existence or state.
    """

    def create_pull_request(
        self, *, repository_path: Path, base_branch: str, head_branch: str, head_sha: str,
        title: str, body: str,
    ) -> PullRequestRecord:
        raise NotImplementedError


class GitHubCliPullRequestPublisher(PullRequestPublisher):
    """Wraps the ``gh`` CLI (no GitHub SDK, no token handling here — ``gh``
    manages its own auth outside this module). Every call is a fixed,
    explicit argv list; the subprocess runner is injectable so tests never
    make a real network call."""

    def __init__(
        self, *, gh_binary: str = "gh", clock: Clock | None = None,
        subprocess_runner: GhSubprocessRunner | None = None,
    ) -> None:
        self._gh_binary = gh_binary
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run_subprocess = subprocess_runner or _default_gh_subprocess_runner

    def create_pull_request(
        self, *, repository_path: Path, base_branch: str, head_branch: str, head_sha: str,
        title: str, body: str,
    ) -> PullRequestRecord:
        argv = [
            self._gh_binary, "pr", "create",
            "--base", base_branch,
            "--head", head_branch,
            "--title", title,
            "--body", body,
        ]
        result = self._run_subprocess(argv, repository_path)
        if result.returncode != 0:
            raise GitGovernanceError(f"gh pr create failed: {result.stderr.strip()}")
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        number = _pr_number_from_url(url)
        return PullRequestRecord(
            provider="github", number=number, url=url, head_branch=head_branch,
            base_branch=base_branch, head_sha=head_sha, state="open", created_at=self._clock(),
        )


def _pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", url)
    return int(match.group(1)) if match else None


# --- GitGovernanceService: the orchestration-facing composition root -------


class GitGovernanceService:
    """Composes ``LocalGitWorkspace`` + ``GitWorkItemStore`` + ``GitGovernancePolicy``
    into the operations ``MVPManager`` needs, in the same opt-in style as
    the rest of this codebase: supplying this service to ``MVPManager``
    enables git governance; omitting it preserves prior behavior exactly
    (no branch, no SHA-binding, workers run directly in ``project.workspace``
    on whatever branch it already has checked out — unchanged).
    """

    def __init__(
        self,
        store: GitWorkItemStore,
        *,
        policy: GitGovernancePolicy | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        git_binary: str = DEFAULT_GIT_BINARY,
        pull_request_publisher: PullRequestPublisher | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or GitGovernancePolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _default_id_factory
        self._git_binary = git_binary
        self._pull_request_publisher = pull_request_publisher

    @property
    def policy(self) -> GitGovernancePolicy:
        return self._policy

    def _workspace(self, repository_path: str | Path) -> LocalGitWorkspace:
        return LocalGitWorkspace(repository_path, git_binary=self._git_binary)

    # --- preparation -----------------------------------------------------

    def prepare_work_item(
        self, *, project_id: str, mvp_id: str, work_item_id: str, repository_path: str | Path,
    ) -> GitWorkItemRecord:
        """Ensures a governed branch exists for this WorkItem and checks it out.

        Idempotent and restart-safe: if a record already exists, this
        reconciles it (never recreates the branch, never re-captures
        ``base_sha``) and simply re-checks-out the existing work branch.
        Only a genuinely new WorkItem goes through the full
        verify-repo/verify-base/verify-clean/capture-base-SHA/create-branch
        sequence.
        """
        existing = self._store.try_get(work_item_id)
        if existing is not None:
            return self.reconcile(work_item_id, repository_path=repository_path, checkout=True)

        ws = self._workspace(repository_path)
        if not ws.is_git_repository():
            raise NotAGitRepositoryError(ws.repository_path)

        current_branch = ws.current_branch()
        if current_branch != self._policy.base_branch:
            # This module never assumes which branch happens to be checked
            # out; a governed WorkItem always starts from the policy's own
            # base branch, explicitly.
            ws.switch(self._policy.base_branch)

        status = ws.working_tree_status()
        if self._policy.require_clean_worktree and status.tracked_dirty:
            raise DirtyWorkingTreeError(status.tracked_dirty)

        base_sha = ws.head_sha(self._policy.base_branch)
        branch = work_branch_name(work_item_id)
        if not ws.branch_exists(branch):
            ws.create_branch(branch, from_ref=base_sha)
        ws.switch(branch)

        record = GitWorkItemRecord(
            git_work_id=self._id_factory(), project_id=project_id, mvp_id=mvp_id,
            work_item_id=work_item_id, repository_path=str(ws.repository_path),
            base_branch=self._policy.base_branch, work_branch=branch, base_sha=base_sha,
            status=GitWorkItemStatus.PREPARED, created_at=self._clock(), updated_at=self._clock(),
        )
        return self._store.create(record)

    def assert_not_protected_branch_target(self, branch: str) -> None:
        if branch in self._policy.protected_branches:
            raise ProtectedBranchError(branch)

    # --- post-execution facts ---------------------------------------------

    def capture_head(self, work_item_id: str, *, repository_path: str | Path) -> GitWorkItemRecord:
        record = self._store.get(work_item_id)
        ws = self._workspace(repository_path)
        actual_branch = ws.current_branch()
        if actual_branch != record.work_branch:
            # The worker/engine is expected to still be on the governed
            # branch right after its own execution — anything else is a
            # governance invariant violation, never silently accepted.
            raise GitHeadDriftError(work_item_id, record.current_head_sha, ws.head_sha())
        head = ws.head_sha()
        return self._store.update_head(work_item_id, head)

    def assert_review_target(self, work_item_id: str, *, repository_path: str | Path) -> GitWorkItemRecord:
        """Verifies the governed branch/head a reviewer is about to see is
        exactly what the store expects — never an ambiguous ``base_branch``
        state."""
        record = self._store.get(work_item_id)
        ws = self._workspace(repository_path)
        if not ws.branch_exists(record.work_branch):
            raise GitBranchMissingError(work_item_id, record.work_branch)
        actual_head = ws.try_rev_parse(record.work_branch)
        if record.current_head_sha is not None and actual_head != record.current_head_sha:
            raise GitHeadDriftError(work_item_id, record.current_head_sha, actual_head or "")
        return record

    # --- merge eligibility & merge ------------------------------------------

    def compute_merge_eligibility(
        self,
        work_item_id: str,
        *,
        repository_path: str | Path,
        work_item_status: str,
        gate_passed: bool | None,
        gate_git_sha: str | None,
        review_approved: bool | None,
        review_git_sha: str | None,
    ) -> MergeEligibilityResult:
        """Pure, deterministic: never an LLM call, never "probably fine".

        Every input beyond ``work_item_id``/``repository_path`` is an
        already-persisted fact the caller supplies explicitly (the exact
        WorkItem status string, the latest quality-gate verdict and the
        exact SHA it was produced against, the latest review verdict and
        the exact SHA it reviewed) — this method never queries a store
        itself, so it works identically whether those facts came from live
        objects in the current process or were reloaded after a cold
        restart.
        """
        now = self._clock()
        record = self._store.try_get(work_item_id)
        if record is None:
            return MergeEligibilityResult(
                work_item_id=work_item_id, head_sha=None, mergeable=False,
                reason="no governed git record for this work item", evaluated_at=now,
            )

        def _not_mergeable(reason: str) -> MergeEligibilityResult:
            return MergeEligibilityResult(
                work_item_id=work_item_id, head_sha=record.current_head_sha, mergeable=False,
                reason=reason, evaluated_at=now,
            )

        if record.status in (GitWorkItemStatus.CONFLICT, GitWorkItemStatus.FAILED, GitWorkItemStatus.ABANDONED):
            return _not_mergeable(f"blocking git incident: {record.status.value}")
        if record.status is GitWorkItemStatus.MERGED:
            return _not_mergeable("already merged")
        if work_item_status != "completed":
            return _not_mergeable(f"work item is not completed (status={work_item_status!r})")
        if record.current_head_sha is None:
            return _not_mergeable("no head captured for the work branch yet")

        if self._policy.require_required_gates:
            if gate_passed is not True:
                return _not_mergeable("required quality gate has not passed")
            if gate_git_sha != record.current_head_sha:
                return _not_mergeable(
                    f"quality gate evidence is for an old SHA ({gate_git_sha!r} != {record.current_head_sha!r})"
                )

        if self._policy.require_review:
            if review_approved is not True:
                return _not_mergeable("required review is not approved")
            if review_git_sha != record.current_head_sha:
                return _not_mergeable(
                    f"review evidence is for an old SHA ({review_git_sha!r} != {record.current_head_sha!r})"
                )

        ws = self._workspace(repository_path)
        if not ws.branch_exists(record.work_branch):
            return _not_mergeable("work branch is missing")
        base_tip = ws.try_rev_parse(record.base_branch)
        if base_tip is None:
            return _not_mergeable("base branch is missing")
        if not ws.is_ancestor(base_tip, record.current_head_sha):
            self._store.mark_conflict(
                work_item_id,
                reason=f"base branch advanced incompatibly (base tip {base_tip!r} not an ancestor of work head)",
            )
            return _not_mergeable("base branch has diverged from the work branch (not fast-forwardable)")

        self._store.mark_merge_ready(work_item_id)
        return MergeEligibilityResult(
            work_item_id=work_item_id, head_sha=record.current_head_sha, mergeable=True,
            reason=None, evaluated_at=now,
        )

    def merge(self, work_item_id: str, *, repository_path: str | Path, eligibility: MergeEligibilityResult) -> GitWorkItemRecord:
        """Performs the actual fast-forward-only merge.

        Requires a fresh, already-computed ``eligibility.mergeable is
        True`` (never re-derives it implicitly) — the caller is expected
        to have just called ``compute_merge_eligibility``. Idempotent: if
        the work item is already ``MERGED``, returns the existing record
        unchanged rather than attempting a second merge.

        TOCTOU/head-drift hardening (Slice 21.5): ``merge_ff_only`` merges
        by *branch name* — it always picks up the work branch's live tip,
        not a pinned SHA. Between ``compute_merge_eligibility`` proving a
        specific ``eligibility.head_sha`` (H2, with gate+review evidence
        bound to it) and this call, the work branch could have advanced
        further (H3 — e.g. a stray rework execution), and H3 would still
        be fast-forwardable from base. Without this check, ``merge()``
        would silently fold in H3 on the strength of evidence that only
        ever covered H2. So the work branch's *actual* current tip is
        re-read here and required to equal ``eligibility.head_sha`` exactly
        before any mutation of ``base_branch`` is attempted — any mismatch
        fails closed as ``GitHeadDriftError``, never a silent re-merge, no
        rebase/reset/force. A base branch that has meanwhile diverged
        incompatibly is still caught by ``merge_ff_only`` itself (git's own
        ff-only check re-validates ancestry at merge time) via the
        existing ``MergeRefusedError``/``mark_conflict`` path below.
        """
        record = self._store.get(work_item_id)
        if record.status is GitWorkItemStatus.MERGED:
            return record
        if not eligibility.mergeable or eligibility.work_item_id != work_item_id:
            raise NotMergeableError(work_item_id, eligibility.reason or "eligibility check did not pass")

        ws = self._workspace(repository_path)
        actual_tip = ws.try_rev_parse(record.work_branch)
        if actual_tip != eligibility.head_sha:
            raise GitHeadDriftError(work_item_id, eligibility.head_sha, actual_tip or "")

        ws.switch(record.base_branch)
        try:
            merged_sha = ws.merge_ff_only(record.work_branch)
        except MergeRefusedError as exc:
            self._store.mark_conflict(work_item_id, reason=str(exc))
            raise
        return self._store.mark_merged(work_item_id, merged_sha=merged_sha)

    # --- restart recovery --------------------------------------------------

    def reconcile(
        self, work_item_id: str, *, repository_path: str | Path, checkout: bool = False,
        known_execution_shas: frozenset[str] = frozenset(),
    ) -> GitWorkItemRecord:
        """Reconstructs/validates state from the store + the real repository —
        never from "whatever is currently checked out".

        A missing work branch while the store still expects one (any
        non-terminal status) fails closed (``GitBranchMissingError``) —
        never silently recreated. A HEAD that no longer matches what the
        store expects is only reconciled (the stored ``current_head_sha``
        is updated) when it corresponds exactly to a SHA in
        ``known_execution_shas`` (a caller-supplied set of persisted,
        legitimate ``ExecutionRecord.git_sha_after`` values for this
        WorkItem) — otherwise it fails closed (``GitHeadDriftError``), no
        broad heuristic.
        """
        record = self._store.get(work_item_id)
        ws = self._workspace(repository_path)

        if record.status in (GitWorkItemStatus.MERGED, GitWorkItemStatus.ABANDONED):
            return record  # terminal: nothing left to reconcile

        if not ws.branch_exists(record.work_branch):
            raise GitBranchMissingError(work_item_id, record.work_branch)

        if checkout:
            ws.switch(record.work_branch)

        actual_head = ws.try_rev_parse(record.work_branch)
        if record.current_head_sha is not None and actual_head != record.current_head_sha:
            if actual_head in known_execution_shas:
                return self._store.update_head(work_item_id, actual_head)
            raise GitHeadDriftError(work_item_id, record.current_head_sha, actual_head or "")
        return record

    # --- optional pull request ---------------------------------------------

    def publish_pull_request(
        self, work_item_id: str, *, title: str, body: str,
    ) -> PullRequestRecord:
        """Creates a remote pull request for the governed branch.

        Never implies merge authorization — that remains exclusively
        ``compute_merge_eligibility``/``merge``'s decision, and this
        method never consults either. Requires ``policy.remote_pr_enabled``
        and a configured ``pull_request_publisher`` — this slice never
        calls it automatically from ``MVPManager``.
        """
        if not self._policy.remote_pr_enabled:
            raise GitGovernanceError("remote PR publishing is disabled by policy (remote_pr_enabled=False)")
        if self._pull_request_publisher is None:
            raise GitGovernanceError("no PullRequestPublisher configured")
        record = self._store.get(work_item_id)
        pr = self._pull_request_publisher.create_pull_request(
            repository_path=Path(record.repository_path), base_branch=record.base_branch,
            head_branch=record.work_branch, head_sha=record.current_head_sha or record.base_sha,
            title=title, body=body,
        )
        self._store.record_pull_request(work_item_id, number=pr.number, url=pr.url)
        return pr
