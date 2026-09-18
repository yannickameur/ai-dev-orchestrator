"""execution_policy — provider-neutral worker execution permission policy.

``ExecutionPermissionMode`` is the *only* generic concept this module
defines: how permissive a worker is allowed to be when Ralph actually
launches it. It knows nothing about Claude/Codex/Vibe CLI flags — that
translation lives exclusively at the execution/backend boundary
(``orchestrator.ralph_execution_engine``), never here, never in
``MVPManager``, never in ``WorkerSelector``. See ROADMAP.md §13 for the
product rationale (project-controlled permission mode, approved as a
cross-cutting requirement of the P1/P12 cycle).

``STANDARD``: AIDO does not request unrestricted/bypass execution. Where
the backend supports an explicit safe/default permission mechanism, AIDO
explicitly requests it — never silently omitted, never left to whatever
the host machine happens to already be configured as.

``UNRESTRICTED``: AIDO explicitly requests verified unattended/bypass-
permission ("YOLO") execution, where the backend honestly supports it.
This is always explicit opt-in — never a default, never inferred from
the host machine's own configuration.

Neither mode implies AIDO ever stores or manages a provider credential —
authentication stays entirely with the provider CLI/environment. This
module has no notion of credentials at all.
"""

from __future__ import annotations

from enum import Enum


class ExecutionPermissionMode(str, Enum):
    """Provider-neutral worker execution permission policy.

    String value is the same lowercase spelling used in project
    configuration (``execution.permission_mode: standard|unrestricted``)
    — see ``orchestrator.project_config``.
    """

    STANDARD = "standard"
    UNRESTRICTED = "unrestricted"

    @classmethod
    def parse(cls, value: object) -> "ExecutionPermissionMode":
        """Strict parse from a config-supplied string — never guesses."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        raise ValueError(
            f"invalid execution permission mode {value!r} — must be one of "
            f"{[m.value for m in cls]!r}"
        )
