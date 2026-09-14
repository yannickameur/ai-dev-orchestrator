"""InternalQAEngine — the first real ``QAEngine`` implementation (Slice 23).

Python/pytest only, and honestly scoped as such: this MVP never claims to
support JavaScript/Java/mobile/browser/Playwright/BrowserStack/TestSprite/
Momentic. A project with none of ``pyproject.toml``/``pytest.ini``/
``setup.cfg``/``tox.ini`` is an unsupported stack — the engine never
attempts a command, and the run resolves to ``INCONCLUSIVE`` (never
``PASS``, never silently skipped).

THIN BY DESIGN — never a second implementation of what already exists:

- Test execution goes exclusively through ``validation.QualityGateRunner``
  (Slice 8, hardened Slice 21.5) — never a second, parallel subprocess
  test runner. A "targeted pytest run" is just a ``ValidationCommand``
  whose ``argv`` embeds the selected pytest node ids, executed the exact
  same way as any other configured validation command.
- Deterministic test selection reuses
  ``qa_knowledge.analyze_test_impact_deterministic`` verbatim (Slice 22).
- Read-only Final Verification reuses ``qa.run_final_verification_gate``
  verbatim (Slice 21.5's ``verify_repository_unchanged``/
  ``require_nonempty_mandatory_manifest``, wrapped once in Slice 22).
- Protected-test enforcement reuses ``qa_protection`` verbatim.
- The governed PASS/FAIL/INCONCLUSIVE decision is exclusively
  ``qa.evaluate_qa_verdict`` — this module never computes a verdict
  itself, only the facts that feed it.
- QA Test Authoring (optional) composes the *existing* adaptive
  mechanism unchanged: ``ComplexityEstimationRequest`` ->
  ``ExecutionRecommendationService`` -> ``WorkerSelector``/
  ``AdaptiveExecutionSelector`` -> ``RalphExecutionEngine``. No second
  worker-selection or execution mechanism.

SCOPE BOUNDARY (Slice 24, not here): this module is never imported by
``mvp_manager.py``, never referenced by
``git_governance.compute_merge_eligibility``, never referenced by
``release_manager.py``. It never launches a coding agent automatically —
a QA ``FAIL`` only ever returns ``requires_coding_agent=True`` with
evidence for a future Slice 24 loop to act on.

ENGINE RESULT != GOVERNED VERDICT: like every other ``QAEngine``, nothing
this module produces is authoritative for PASS/FAIL/INCONCLUSIVE — see
``orchestrator.qa`` module docstring. This module's ``run()``/
``run_final_verification()`` only ever produce a ``QAResult`` (an
observation); ``run_qa_cycle`` below is the one place that calls
``evaluate_qa_verdict``, exactly once, from already-persisted facts.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from orchestrator.adaptive_execution import AdaptiveExecutionSelector
from orchestrator.complexity_estimation import ComplexityEstimationRequest
from orchestrator.qa import (
    FailureClassification,
    QAEvidenceManifest,
    QAPhase,
    QAPolicy,
    QARequest,
    QARun,
    QARunStatus,
    QARunStore,
    QAResult,
    QAVerdict,
    TestImpactRequest,
    TestImpactResult,
    evaluate_qa_verdict,
    new_qa_run,
    run_final_verification_gate,
)
from orchestrator.qa_knowledge import (
    KnownFlakyEntry,
    QAKnowledgeBase,
    analyze_test_impact_deterministic,
    load_qa_knowledge_base,
)
from orchestrator.qa_protection import (
    ProtectedTestBaseline,
    TestChangeAuthorization,
    capture_protected_test_baseline,
    compare_protected_test_baseline,
    has_unauthorized_change,
)
from orchestrator.ralph_execution_engine import (
    ExecutionRequest,
    RalphExecutionEngine,
    RalphExecutionEngineError,
)
from orchestrator.validation import (
    QualityGateRunner,
    ValidationCommand,
    ValidationKind,
    ValidationStatus,
    ValidationStore,
)
from orchestrator.worker_selector import (
    NoEligibleWorkerError,
    ReviewIndependenceError,
    Worker,
    WorkerSelector,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

ENGINE_ID = "internal"

#: This project's own single role/capability string for QA authoring —
#: deliberately the same string for both (unlike development/review's
#: role-vs-capability split), per the task brief.
QA_TESTING_ROLE = "qa_testing"
QA_TESTING_CAPABILITY = "qa_testing"

AUTHORING_INITIAL_EVENT_TOPIC = "qa.authoring.start"
AUTHORING_SUCCESS_TOPIC = "qa.authoring.completed"
AUTHORING_FAILURE_TOPIC = "qa.authoring.failed"

_PYTEST_STACK_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")

_DEFAULT_BOUNDED_RETRIES = 2
_MAX_BOUNDED_RETRIES = 5


def _require_non_empty_str(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


class InternalQAEngineError(Exception):
    """Base for this module's own domain errors."""


class UnsupportedStackError(InternalQAEngineError):
    """No Python/pytest marker file found in the workspace — this MVP is
    Python/pytest only, honestly. Never attempted as a command; the
    caller (``run_qa_cycle``) turns this into a ``FAILED`` run, which
    ``evaluate_qa_verdict`` already maps to ``INCONCLUSIVE`` — never a
    silent ``PASS``."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(f"no Python/pytest stack marker found under {workspace}")
        self.workspace = workspace


class NoEvidenceAvailableError(InternalQAEngineError):
    """Raised when the deterministic plan selects zero targeted tests AND
    the project has no configured regression command to fall back to —
    truly nothing executable exists. Distinct from "manifest is empty"
    (a Slice 21.5/22 invariant on an *attempted, completed* run): this is
    an infrastructure-level inability to even gather evidence, so the
    caller marks the run ``FAILED`` -> ``INCONCLUSIVE``, never ``FAIL``."""


class InvalidQAAuthoringEventError(InternalQAEngineError):
    """A ``qa.authoring.completed``/``qa.authoring.failed`` payload was
    absent or malformed — fails closed, no lenient fallback (unlike
    ``review.parse_findings``): a QA authoring report is either a
    trustworthy structured fact or nothing is assumed about it at all."""


class AuthoringViolationError(InternalQAEngineError):
    """A QA Test Authoring execution touched a production file it was
    never authorized to touch, or modified a protected test without a
    valid ``TestChangeAuthorization`` — fail closed, no auto-repair."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"QA authoring violation: {detail}")


# --- deterministic plan -------------------------------------------------


@dataclass(frozen=True, slots=True)
class InternalQAPlan:
    """The engine's own auditable plan — included in ``QAResult``/
    ``QARun`` evidence, never a hidden intermediate. Deliberately mirrors
    ``TestImpactResult`` plus what actually gets executed."""

    impacted_areas: tuple[str, ...] = ()
    selected_tests: tuple[str, ...] = ()
    required_invariants: tuple[str, ...] = ()
    targeted_commands: tuple[ValidationCommand, ...] = ()
    regression_commands: tuple[ValidationCommand, ...] = ()
    static_checks: tuple[ValidationCommand, ...] = ()
    phase: QAPhase | None = None
    rationale: tuple[str, ...] = ()
    #: validation_id -> the known-flaky entry it corresponds to, for the
    #: (small) subset of selected tests that are individually listed in
    #: ``.qa/known-flaky.yaml`` with a retry policy — see
    #: ``InternalQAEngine._run_with_flaky_retry``. Never used to skip a
    #: test; only to bound how many times it may be re-run.
    flaky_by_validation_id: Mapping[str, KnownFlakyEntry] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QAAuthoringReport:
    """Strictly parsed ``qa.authoring.completed`` payload — a *claim* from
    the worker, never trusted alone (see ``verify_authoring_git_facts``)."""

    tests_added: tuple[str, ...] = ()
    tests_modified: tuple[str, ...] = ()
    fixtures_added: tuple[str, ...] = ()
    fixtures_modified: tuple[str, ...] = ()
    production_files_modified: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()


def parse_qa_authoring_event(payload: str | None) -> QAAuthoringReport:
    """Strict, fail-closed parse of a ``qa.authoring.completed`` payload —
    unlike ``review.parse_findings``, an absent or malformed payload is
    never lenient-fallback-wrapped into a best-effort report: it raises.
    """
    if not payload:
        raise InvalidQAAuthoringEventError("qa.authoring.completed payload is empty/absent")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidQAAuthoringEventError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidQAAuthoringEventError(f"payload must be a JSON object, got {type(data)!r}")

    def _strs(key: str) -> tuple[str, ...]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidQAAuthoringEventError(f"payload field {key!r} must be a list of strings")
        return tuple(value)

    return QAAuthoringReport(
        tests_added=_strs("tests_added"),
        tests_modified=_strs("tests_modified"),
        fixtures_added=_strs("fixtures_added"),
        fixtures_modified=_strs("fixtures_modified"),
        production_files_modified=_strs("production_files_modified"),
        findings=_strs("findings"),
        risks=_strs("risks"),
        recommended_actions=_strs("recommended_actions"),
    )


# --- git facts (read-only; independent of the worker's own claim) -------


def _run_git(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True, timeout=30)


def changed_files_since(cwd: Path, base_sha: str, head_sha: str | None = None) -> tuple[str, ...]:
    """Read-only ``git diff --name-only`` between two refs (``head_sha``
    defaults to the working tree's current state, including uncommitted
    changes, so an in-progress authoring execution can be inspected
    before it commits anything)."""
    target = head_sha or "HEAD"
    result = _run_git(["diff", "--name-only", base_sha, target], cwd)
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def working_tree_changed_files(cwd: Path, base_sha: str) -> tuple[str, ...]:
    """Committed changes since ``base_sha`` plus any currently uncommitted
    tracked modifications — the union an authoring diff-check needs,
    since a worker may or may not have committed."""
    committed = set(changed_files_since(cwd, base_sha, "HEAD"))
    status = _run_git(["status", "--porcelain=v1"], cwd)
    for line in status.stdout.splitlines():
        if line:
            committed.add(line[3:])
    return tuple(sorted(committed))


#: Runtime/scratch noise legitimately left behind by a real execution —
#: Ralph's own event/loop state (``.ralph/`` — see
#: ``ralph_execution_engine``'s real subprocess behavior, mirrored by
#: ``tests/test_ralph_execution_engine.py``'s fake runner) and pytest's
#: own bytecode/cache directories (Part C, explicitly never a product
#: mutation) — never authored content. Matched as a path *segment*
#: (``_is_runtime_noise``), not just a root prefix: a real run genuinely
#: produced ``__pycache__/review_candidate.cpython-314.pyc`` nested next
#: to the source file it compiled, not only at the workspace root.
_RUNTIME_NOISE_SEGMENTS = (".ralph", "__pycache__", ".pytest_cache")


def _is_runtime_noise(path: str) -> bool:
    return any(segment in path.split("/") for segment in _RUNTIME_NOISE_SEGMENTS)


#: pytest's own default test-discovery convention (a ``test_*.py``/
#: ``*_test.py`` file anywhere, not only under a ``tests/`` directory —
#: many real, small, or spike-style projects, including
#: ``~/projects/ralph-spike``, keep a flat layout with no ``tests/``
#: directory at all). Checked in addition to ``allowed_prefixes``, never
#: instead of it — a directory-based convention (``tests/``, ``fixtures/``)
#: is still honored for projects that use one.
def _looks_like_a_test_filename(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")


def verify_authoring_git_facts(
    *, cwd: Path, base_sha: str, allowed_prefixes: Sequence[str] = ("tests/", "fixtures/", ".qa/")
) -> tuple[str, ...]:
    """Independently computes which files actually changed — never trusts
    ``QAAuthoringReport.production_files_modified`` alone. Returns the
    subset of changed files that fall outside ``allowed_prefixes``
    (production files) — a non-empty result is an ``AuthoringViolationError``
    condition for the caller to raise.

    ``base_sha`` must reference a commit where the working tree was
    already clean (no pre-existing untracked files) — exactly what
    ``GitGovernanceService.prepare_work_item``'s ``require_clean_worktree``
    already enforces for a governed WorkItem (Slice 20). This function has
    no way to distinguish "untracked before base_sha" from "created by
    this authoring execution" on its own; a caller outside that governed
    path (e.g. a manual smoke script) must establish a clean baseline
    itself before calling this.
    """
    changed = working_tree_changed_files(cwd, base_sha)
    return tuple(
        f for f in changed
        if not any(f.startswith(p) for p in allowed_prefixes)
        and not _looks_like_a_test_filename(f)
        and not _is_runtime_noise(f)
    )


# --- failure classification (deterministic-first) ------------------------


def classify_validation_status(status: ValidationStatus) -> FailureClassification:
    """Deterministic-first classification for one command's outcome.
    Never claims REGRESSION/TEST_DEFECT/EXPECTED_CHANGE without more
    context than a bare pytest exit code provides — those stay UNKNOWN,
    a conservative and acceptable outcome per the task brief."""
    if status is ValidationStatus.TIMEOUT:
        return FailureClassification.ENVIRONMENT_FAILURE
    if status is ValidationStatus.ERROR:
        return FailureClassification.ENVIRONMENT_FAILURE
    return FailureClassification.UNKNOWN


def _parse_retry_budget(retry_policy: str | None) -> int:
    if not retry_policy:
        return 0
    match = re.search(r"\d+", retry_policy)
    if not match:
        return _DEFAULT_BOUNDED_RETRIES
    return min(int(match.group()), _MAX_BOUNDED_RETRIES)


# --- InternalQAEngine -----------------------------------------------------


class InternalQAEngine:
    """Satisfies ``orchestrator.qa.QAEngine`` (``run(request) -> QAResult``,
    synchronous per the Protocol) — internally wraps the async
    ``QualityGateRunner``. ``run_final_verification`` is a separate,
    non-Protocol method for the read-only phase, since ``QARequest`` has
    no phase field (phase lives on ``QARun``, the caller's concern)."""

    engine_id = ENGINE_ID

    def __init__(
        self,
        *,
        validation_store: ValidationStore,
        gate_runner: QualityGateRunner,
        clock: Clock | None = None,
    ) -> None:
        self._validation_store = validation_store
        self._gate_runner = gate_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- stack detection ---------------------------------------------------

    def stack_supported(self, workspace: str | Path) -> bool:
        workspace = Path(workspace)
        return any((workspace / name).is_file() for name in _PYTEST_STACK_MARKERS)

    # -- deterministic plan --------------------------------------------------

    def build_plan(self, request: QARequest, *, phase: QAPhase | None = None) -> InternalQAPlan:
        workspace = Path(request.workspace)
        knowledge_base = load_qa_knowledge_base(workspace)
        impact = analyze_test_impact_deterministic(
            TestImpactRequest(
                base_sha=request.base_sha, head_sha=request.head_sha,
                changed_files=request.changed_files, technology_stack=request.technology_stack,
            ),
            knowledge_base,
        )
        selected_tests = tuple(dict.fromkeys((*impact.existing_tests, *request.required_test_ids)))
        required_invariants = tuple(dict.fromkeys((*impact.required_invariants, *request.required_invariants)))

        # Known-flaky selected tests get their OWN command (Part G): a
        # test bundled into one big pytest invocation can't be
        # individually retried — a failure can't even be attributed to
        # it without parsing pytest's own output, which this MVP
        # deliberately does not do. A flaky-listed test runs alone so a
        # bounded retry (see `_run_with_flaky_retry`) can target exactly
        # that command. Never a skip: still a required command either way.
        flaky_ids = {e.test_id for e in knowledge_base.known_flaky if e.retry_policy}
        flaky_selected = tuple(t for t in selected_tests if t in flaky_ids)
        normal_selected = tuple(t for t in selected_tests if t not in flaky_ids)

        targeted_commands: list[ValidationCommand] = []
        flaky_by_validation_id: dict[str, KnownFlakyEntry] = {}
        if normal_selected:
            targeted_commands.append(
                ValidationCommand(
                    validation_id="qa-targeted-pytest", kind=ValidationKind.UNIT_TEST,
                    argv=(sys.executable, "-m", "pytest", "-q", *normal_selected), required=True,
                )
            )
        for test_id in flaky_selected:
            entry = next(e for e in knowledge_base.known_flaky if e.test_id == test_id)
            vid = f"qa-flaky-{len(targeted_commands)}"
            targeted_commands.append(
                ValidationCommand(
                    validation_id=vid, kind=ValidationKind.UNIT_TEST,
                    argv=(sys.executable, "-m", "pytest", "-q", test_id), required=True,
                )
            )
            flaky_by_validation_id[vid] = entry

        regression_commands: tuple[ValidationCommand, ...] = ()
        if phase is QAPhase.FINAL_VERIFICATION:
            regression_commands = self._validation_store.get_project_commands(request.project_id)
        elif not selected_tests:
            # Part Q fallback: nothing selected but QA is presumably
            # required — fall back to the project's configured
            # regression suite if one exists.
            regression_commands = self._validation_store.get_project_commands(request.project_id)

        return InternalQAPlan(
            impacted_areas=impact.impacted_components,
            selected_tests=selected_tests,
            required_invariants=required_invariants,
            targeted_commands=tuple(targeted_commands),
            regression_commands=regression_commands,
            static_checks=(),
            phase=phase,
            rationale=impact.rationale,
            flaky_by_validation_id=flaky_by_validation_id,
        )

    def build_manifest(self, plan: InternalQAPlan) -> QAEvidenceManifest:
        commands = (*plan.targeted_commands, *plan.regression_commands, *plan.static_checks)
        return QAEvidenceManifest(
            required_test_ids=plan.selected_tests,
            required_invariant_ids=plan.required_invariants,
            required_engines=(ENGINE_ID,),
            deterministic_commands=tuple(" ".join(c.argv) for c in commands),
        )

    # -- QAEngine protocol (synchronous) -------------------------------------

    def run(self, request: QARequest) -> QAResult:
        """Satisfies the ``QAEngine`` Protocol — synchronous entry point,
        wraps the async pipeline internally."""
        return asyncio.run(self.run_async(request))

    async def run_async(self, request: QARequest, *, plan: InternalQAPlan | None = None) -> QAResult:
        workspace = Path(request.workspace)
        if not self.stack_supported(workspace):
            raise UnsupportedStackError(workspace)
        plan = plan or self.build_plan(request)
        return await self._execute_plan(request, plan, project_suffix="targeted")

    async def run_final_verification(
        self, request: QARequest, *, plan: InternalQAPlan | None = None
    ) -> tuple[QAResult, bool, bool]:
        """Read-only Final Verification — reuses
        ``qa.run_final_verification_gate`` verbatim (Slice 21.5's
        ``verify_repository_unchanged``). Returns
        ``(result, read_only_violation, read_only_unprovable)``."""
        workspace = Path(request.workspace)
        if not self.stack_supported(workspace):
            raise UnsupportedStackError(workspace)
        plan = plan or self.build_plan(request, phase=QAPhase.FINAL_VERIFICATION)
        commands = (*plan.targeted_commands, *plan.regression_commands, *plan.static_checks)
        if not commands:
            raise NoEvidenceAvailableError(
                f"no targeted tests selected and no regression commands configured for {request.project_id!r}"
            )

        synthetic_project_id = f"{request.project_id}:qa:final:{request.work_item_id}"
        self._validation_store.set_project_commands(synthetic_project_id, commands)

        started_at = self._clock()
        read_only_violation, read_only_unprovable = await run_final_verification_gate(
            gate_runner=self._gate_runner, project_id=synthetic_project_id, cwd=workspace,
            mvp_id=request.mvp_id, work_item_id=request.work_item_id,
        )
        gate = self._validation_store.latest_gate_result_for_work_item(request.work_item_id)
        adjusted_results, flaky_risks, flaky_ids = ([], [], set())
        if gate is not None and not read_only_violation and not read_only_unprovable:
            adjusted_results, flaky_risks, flaky_ids = await self._run_with_flaky_retry(
                request, plan, gate.results, project_prefix=synthetic_project_id,
            )
        result = self._normalize_result(
            request, plan, gate, started_at=started_at,
            adjusted_results=adjusted_results or None, flaky_risks=flaky_risks, flaky_ids=flaky_ids,
        )
        return result, read_only_violation, read_only_unprovable

    async def _execute_plan(self, request: QARequest, plan: InternalQAPlan, *, project_suffix: str) -> QAResult:
        commands = (*plan.targeted_commands, *plan.regression_commands, *plan.static_checks)
        if not commands:
            raise NoEvidenceAvailableError(
                f"no targeted tests selected and no regression commands configured for {request.project_id!r}"
            )
        synthetic_project_id = f"{request.project_id}:qa:{project_suffix}:{request.work_item_id}"
        self._validation_store.set_project_commands(synthetic_project_id, commands)

        started_at = self._clock()
        gate = await self._gate_runner.run_gate(
            project_id=synthetic_project_id, cwd=Path(request.workspace),
            mvp_id=request.mvp_id, work_item_id=request.work_item_id,
        )
        adjusted_results, flaky_risks, flaky_ids = await self._run_with_flaky_retry(
            request, plan, gate.results, project_prefix=synthetic_project_id,
        )
        return self._normalize_result(
            request, plan, gate, started_at=started_at,
            adjusted_results=adjusted_results, flaky_risks=flaky_risks, flaky_ids=flaky_ids,
        )

    async def _run_with_flaky_retry(
        self, request: QARequest, plan: InternalQAPlan, results: Sequence, *, project_prefix: str,
    ) -> tuple[list, list[str], set[str]]:
        """Part G: a known-flaky test that failed on its first attempt may
        be re-run a bounded number of times (never infinite). Every
        attempt is a real, separately persisted ``QualityGateRunner`` run
        (own ``validation_run_id``, own audit trail) — nothing here
        silently overwrites the first attempt's evidence. If any retry
        passes, the test's *effective* result becomes that passing
        attempt (excluded from ``regressions``) and the retry history is
        recorded in ``QAResult.risks`` — never dropped, never treated as
        equivalent to "ignored"."""
        adjusted = list(results)
        risks: list[str] = []
        flaky_ids: set[str] = set()
        commands_by_id = {c.validation_id: c for c in (*plan.targeted_commands, *plan.regression_commands)}
        for i, result in enumerate(results):
            entry = plan.flaky_by_validation_id.get(result.validation_id)
            if entry is None or result.status is ValidationStatus.PASSED:
                continue
            budget = _parse_retry_budget(entry.retry_policy)
            if budget <= 0:
                continue
            command = commands_by_id[result.validation_id]
            attempts = [result.status.value]
            for attempt_n in range(1, budget + 1):
                retry_project_id = f"{project_prefix}:flaky-retry:{result.validation_id}:{attempt_n}"
                self._validation_store.set_project_commands(retry_project_id, [command])
                retry_gate = await self._gate_runner.run_gate(
                    project_id=retry_project_id, cwd=Path(request.workspace),
                    mvp_id=request.mvp_id, work_item_id=request.work_item_id,
                )
                retry_result = retry_gate.results[0]
                attempts.append(retry_result.status.value)
                if retry_result.status is ValidationStatus.PASSED:
                    adjusted[i] = retry_result
                    flaky_ids.add(result.validation_id)
                    break
            risks.append(f"known-flaky {entry.test_id!r}: attempts={attempts!r}")
        return adjusted, risks, flaky_ids

    def _normalize_result(
        self, request: QARequest, plan: InternalQAPlan, gate, *, started_at: datetime,
        adjusted_results: Sequence | None = None, flaky_risks: Sequence[str] = (),
        flaky_ids: set[str] = frozenset(),
    ) -> QAResult:
        finished_at = self._clock()
        if gate is None:
            return QAResult(
                engine_id=ENGINE_ID, observed_head_sha=request.head_sha,
                started_at=started_at, finished_at=finished_at,
                change_scope="; ".join(plan.rationale) or "no changes analyzed",
                tests_selected=plan.selected_tests, coverage_gaps=plan.required_invariants,
                engine_reported_status=None,
            )

        results = list(adjusted_results) if adjusted_results is not None else list(gate.results)
        passed_count = sum(1 for r in results if r.status is ValidationStatus.PASSED)
        skipped_results = [r for r in results if r.status is ValidationStatus.SKIPPED]
        failed_results = [
            r for r in results if r.status not in (ValidationStatus.PASSED, ValidationStatus.SKIPPED)
        ]

        classifications = list(dict.fromkeys(classify_validation_status(r.status) for r in failed_results))
        if flaky_ids:
            classifications.append(FailureClassification.FLAKY_TEST)
        classifications = tuple(dict.fromkeys(classifications))
        regressions = tuple(
            f"{r.validation_id} ({r.status.value}, exit={r.exit_code})" for r in failed_results
        )
        # A required invariant only counts as automatically verified when
        # its related tests actually ran as part of this run's commands —
        # an invariant this run's plan required but for which no command
        # ran at all is a coverage gap, never silently assumed satisfied
        # just because its text sounds true. This engine has no mechanical
        # way to attribute one failing pytest command to a specific
        # invariant id, so it never populates `failed_invariant_ids`
        # (an honest limitation, not a false negative) — `regressions`
        # already carries the raw failed-command evidence that feeds
        # `evaluate_qa_verdict`'s "unresolved regression" check.
        ran_commands = bool(results)
        coverage_gaps = plan.required_invariants if (plan.required_invariants and not ran_commands) else ()

        real_head = self._observed_head(Path(request.workspace)) or request.head_sha
        return QAResult(
            engine_id=ENGINE_ID, engine_run_id=gate.validation_run_id, observed_head_sha=real_head,
            started_at=started_at, finished_at=finished_at,
            change_scope="; ".join(plan.rationale) or "no impact-mapped changes",
            tests_selected=plan.selected_tests, tests_executed=tuple(r.validation_id for r in results),
            passed_count=passed_count, failed_count=len(failed_results), skipped_count=len(skipped_results),
            failure_classifications=classifications, regressions=regressions,
            coverage_gaps=coverage_gaps, risks=tuple(flaky_risks),
            requires_coding_agent=bool(regressions),
            failed_invariant_ids=(),
            engine_reported_status="PASS" if not failed_results else "FAIL",
        )

    @staticmethod
    def _observed_head(workspace: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None


# --- top-level orchestration: one QA run, start to finish -----------------


async def run_qa_cycle(
    *,
    engine: InternalQAEngine,
    run_store: QARunStore,
    request: QARequest,
    phase: QAPhase,
    policy: QAPolicy,
    protected_baseline: ProtectedTestBaseline | None = None,
    authorizations: Mapping[str, TestChangeAuthorization] | None = None,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
) -> QARun:
    """Sequences exactly Part I: create -> RUNNING -> engine actions ->
    QAResult insert -> deterministic QAVerdict -> terminal run status.
    Evidence is always persisted before this function returns."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    plan = engine.build_plan(request, phase=phase)
    manifest = engine.build_manifest(plan)

    run = new_qa_run(
        project_id=request.project_id, mvp_id=request.mvp_id, work_item_id=request.work_item_id,
        engine_id=engine.engine_id, phase=phase, expected_base_sha=request.base_sha,
        expected_head_sha=request.head_sha, policy=policy, manifest=manifest,
        clock=clock, id_factory=id_factory,
    )
    run_store.create(run)
    run_store.update_status(run.run_id, QARunStatus.RUNNING)

    read_only_violation = False
    read_only_unprovable = False
    result: QAResult | None = None
    try:
        if phase is QAPhase.FINAL_VERIFICATION and policy.final_verification_read_only:
            result, read_only_violation, read_only_unprovable = await engine.run_final_verification(request, plan=plan)
        else:
            result = await engine.run_async(request, plan=plan)
    except (UnsupportedStackError, NoEvidenceAvailableError, RalphExecutionEngineError):
        run_store.update_status(run.run_id, QARunStatus.FAILED)
        run = run_store.get(run.run_id)
        verdict = evaluate_qa_verdict(run=run, result=None, now=clock())
        run_store.record_verdict(run.run_id, verdict)
        return run_store.get(run.run_id)

    run_store.record_result(run.run_id, result)
    run_store.update_status(run.run_id, QARunStatus.COMPLETED)
    run = run_store.get(run.run_id)

    unauthorized = False
    if protected_baseline is not None:
        changes = compare_protected_test_baseline(
            protected_baseline, request.workspace, authorizations=authorizations,
        )
        unauthorized = has_unauthorized_change(changes)

    verdict = evaluate_qa_verdict(
        run=run, result=result, now=clock(),
        unauthorized_protected_change=unauthorized,
        read_only_violation=read_only_violation, read_only_unprovable=read_only_unprovable,
    )
    run_store.record_verdict(run.run_id, verdict)
    return run_store.get(run.run_id)


# --- QA Test Authoring: adaptive worker, real Ralph execution -------------


def _build_qa_authoring_instructions(
    *, objective: str, acceptance_criteria: Sequence[str], base_sha: str, head_sha: str,
    changed_files: Sequence[str], existing_coverage: Sequence[str],
) -> str:
    criteria = "\n".join(f"- {c}" for c in acceptance_criteria) or "- (none specified)"
    changed = "\n".join(f"- {f}" for f in changed_files) or "- (none reported)"
    coverage = "\n".join(f"- {t}" for t in existing_coverage) or "- (no related existing tests found)"
    return (
        "You are the QA Test Authoring worker. You are not the coding agent.\n\n"
        f"Objective under test: {objective}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Git range to analyze: {base_sha} -> {head_sha}\n"
        f"Changed files:\n{changed}\n\n"
        f"Existing related test coverage already known:\n{coverage}\n\n"
        "Your job:\n"
        "- Analyze the diff and the .qa/ knowledge base if present.\n"
        "- Inspect existing test coverage for the change.\n"
        "- If — and only if — coverage is genuinely missing, add a new "
        "regression test (and/or an explicitly authorized fixture). Prefer "
        "a test that reproduces the bug/gap and fails on current behavior "
        "when technically possible (red-first).\n"
        "- If coverage is already sufficient, add nothing — do not "
        "fabricate a duplicate test just to have something to report.\n\n"
        "You may modify ONLY: new or explicitly authorized test files, "
        "explicitly authorized fixtures, and .qa/ files if policy allows.\n\n"
        "You must NEVER:\n"
        "- modify production/source code;\n"
        "- modify or weaken an existing protected test's assertions;\n"
        "- make a failing test pass by rewriting its expectations instead "
        "of fixing the underlying behavior (you cannot fix behavior — "
        "that is the coding agent's job, not yours);\n"
        "- fabricate a green result.\n\n"
        "When done, emit exactly one structured event as a single-line "
        "JSON object with fields: tests_added, tests_modified, "
        "fixtures_added, fixtures_modified, production_files_modified "
        "(must be empty), findings, risks, recommended_actions (all "
        "string lists):\n\n"
        f'ralph emit "{AUTHORING_SUCCESS_TOPIC}" \'{{"tests_added": [...], "tests_modified": [...], '
        '"fixtures_added": [...], "fixtures_modified": [...], "production_files_modified": [], '
        '"findings": [...], "risks": [...], "recommended_actions": [...]}\'\n\n'
        "If you cannot complete this analysis, emit exactly:\n\n"
        f'ralph emit "{AUTHORING_FAILURE_TOPIC}" "<short reason>"\n\n'
        "Then output:\n\nLOOP_COMPLETE\n"
    )


@dataclass(frozen=True, slots=True)
class QAAuthoringOutcome:
    """What happened during one QA Test Authoring execution — the
    worker's own structured claim (``report``, ``None`` if the event was
    absent/invalid) plus this module's own independently-verified Git
    facts (``unauthorized_files``, always computed, never trusted from
    the worker alone)."""

    worker_id: str
    provider: str
    model: str
    reasoning_effort: str | None
    execution_id: str
    succeeded: bool
    report: QAAuthoringReport | None
    unauthorized_files: tuple[str, ...]
    git_sha_after: str | None


class InternalQATestAuthor:
    """Composes the existing adaptive mechanism for the optional QA Test
    Authoring phase — never a parallel worker-selection/execution path.
    """

    def __init__(
        self,
        *,
        adaptive_execution_selector: AdaptiveExecutionSelector,
        worker_selector: WorkerSelector,
        execution_engine: RalphExecutionEngine,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout_seconds: float = 900.0,
    ) -> None:
        self._adaptive_execution_selector = adaptive_execution_selector
        self._worker_selector = worker_selector
        self._execution_engine = execution_engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._timeout_seconds = timeout_seconds

    async def select_worker(
        self, *, estimation_request: ComplexityEstimationRequest, developer_worker_id: str | None = None,
        reviewer_worker_id: str | None = None,
    ) -> Worker:
        """``qa_testing`` worker selection. ``developer_worker_id`` exclusion
        is MANDATORY whenever a real development author exists (forwarded
        as ``author_worker_id`` — the exact same governance
        ``WorkerSelector`` already enforces for review independence,
        reused verbatim, never re-implemented) — ``None`` only for a
        standalone QA analysis with no development author at all (e.g.
        the Part R smoke, which never runs a dev-fix phase). A distinct
        ``reviewer_worker_id`` is only PREFERRED: tried first via
        ``excluded_worker_ids``, and — only if that leaves zero eligible
        candidates — retried without excluding the reviewer, so a
        two-worker configuration is never blocked over a soft preference.
        """
        excluded = frozenset({reviewer_worker_id}) if reviewer_worker_id else frozenset()
        try:
            if excluded:
                selection = await self._adaptive_execution_selector.select(
                    estimation_request=estimation_request,
                    required_capabilities=frozenset({QA_TESTING_CAPABILITY}),
                    author_worker_id=developer_worker_id, excluded_worker_ids=excluded,
                )
                return selection.worker
        except (NoEligibleWorkerError, ReviewIndependenceError):
            pass
        selection = await self._adaptive_execution_selector.select(
            estimation_request=estimation_request,
            required_capabilities=frozenset({QA_TESTING_CAPABILITY}),
            author_worker_id=developer_worker_id,
        )
        return selection.worker

    async def run_authoring(
        self,
        *,
        worker: Worker,
        model: str,
        reasoning_effort: str | None,
        task_id: str,
        workspace: str | Path,
        objective: str,
        acceptance_criteria: Sequence[str],
        base_sha: str,
        head_sha: str,
        changed_files: Sequence[str],
        existing_coverage: Sequence[str],
    ) -> QAAuthoringOutcome:
        workspace = Path(workspace)
        execution_id = self._id_factory()
        request = ExecutionRequest(
            execution_id=execution_id, task_id=task_id, worker=worker, role=QA_TESTING_ROLE,
            workspace=workspace,
            instructions=_build_qa_authoring_instructions(
                objective=objective, acceptance_criteria=acceptance_criteria,
                base_sha=base_sha, head_sha=head_sha, changed_files=changed_files,
                existing_coverage=existing_coverage,
            ),
            initial_event_topic=AUTHORING_INITIAL_EVENT_TOPIC,
            success_topics=frozenset({AUTHORING_SUCCESS_TOPIC}),
            failure_topics=frozenset({AUTHORING_FAILURE_TOPIC}),
            timeout_seconds=self._timeout_seconds, model=model, reasoning_effort=reasoning_effort,
        )
        result = await self._execution_engine.execute(request)

        report: QAAuthoringReport | None = None
        success_events = [e for e in result.events if e.topic == AUTHORING_SUCCESS_TOPIC]
        if success_events:
            try:
                report = parse_qa_authoring_event(success_events[-1].payload)
            except InvalidQAAuthoringEventError:
                report = None

        unauthorized_files = verify_authoring_git_facts(cwd=workspace, base_sha=base_sha)

        return QAAuthoringOutcome(
            worker_id=worker.worker_id, provider=worker.provider, model=model,
            reasoning_effort=reasoning_effort, execution_id=result.record.execution_id,
            succeeded=bool(success_events) and report is not None and not unauthorized_files,
            report=report, unauthorized_files=unauthorized_files,
            git_sha_after=result.record.git_sha_after,
        )
