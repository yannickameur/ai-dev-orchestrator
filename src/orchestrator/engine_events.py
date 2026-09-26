"""``EngineEvent`` — the public, live-progress event DTO (P18).

This module exists purely to hold this one dataclass, with zero
dependency on either ``orchestrator.engine`` (the public façade) or
``orchestrator.mvp_manager`` (which produces the DEV A/DEV B/DEV FIX/
QA/Git facts an event describes). ``orchestrator.engine`` already
depends on ``orchestrator.project_runtime``/``orchestrator.mvp_manager``
(never the reverse) — if ``EngineEvent`` stayed defined in
``orchestrator.engine``, ``mvp_manager.py`` constructing one to hand to
an ``on_event`` callback would need to import the façade, closing an
import cycle. A neutral, dependency-free module avoids that without an
emitter/indirection layer (REUSE FIRST/KISS): both sides import this
one module directly.

``orchestrator.engine`` re-exports ``EngineEvent`` unchanged, so
``from orchestrator.engine import EngineEvent`` (the historical/public
import path) keeps working exactly as before — this module's own path
is an implementation detail, never a second public contract. There is
only ever one ``EngineEvent`` type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """The smallest structured event contract a live consumer needs
    (P18, ROADMAP.md §13). See ``ROADMAP.md`` for the full, approved
    event catalog and DTO field provenance rules.

    ``kind`` is one of ``"work_item.<status>"`` (the coarse event this
    project has always produced, still the only thing
    ``RunResult.events`` ever contains) or a finer
    ``"dev_a.<status>"``/``"dev_b.<status>"``/``"dev_fix.<status>"``/
    ``"qa.<status>"``/``"git.<status>"``/``"run.<status>"`` value
    (P18-02/P18-03) — delivered live via
    ``OrchestratorEngine.run(on_event=...)``, never accumulated into
    ``RunResult``.

    Every field below ``payload`` is optional, ``None`` when genuinely
    unknown at the time this event is built — never fabricated, never
    deduced by a caller from ``worker_id`` alone. ``model``/
    ``reasoning_effort`` come from the real ``ExecutionRequest``/
    ``ExecutionResult`` this execution actually ran with;
    ``profile_id``/``quality_tier`` come from the ``Worker``'s own
    resolved ``ExecutionProfile`` at the exact moment of selection —
    today, since Adaptive Execution is not wired into production
    (``ROADMAP.md``, P18), this is always the worker's
    ``default_profile_id`` profile, never a value Adaptive Execution
    would have chosen.
    """

    kind: str
    timestamp: str
    project_id: str
    mvp_id: str
    work_item_id: str
    payload: dict[str, Any]

    execution_id: str | None = None
    phase: str | None = None
    status: str | None = None
    worker_id: str | None = None
    worker_display_name: str | None = None
    provider: str | None = None
    backend: str | None = None
    profile_id: str | None = None
    model: str | None = None
    quality_tier: str | None = None
    reasoning_effort: str | None = None
    commit_sha: str | None = None
