"""Packaged, non-Python resources shipped inside the ``ai-dev-orchestrator``
wheel. Currently just ``default_workers.yaml`` (P13.3, see ROADMAP.md) —
a starter worker registry template consumed only by the legacy ``aido
init``/``aido`` CLI scaffold path, so it works standalone (pip install
only, no sibling source checkout).

LEGACY: this is never the modern product's worker configuration source of
truth. ``OrchestratorEngine``/``ProjectRuntime`` never read this file
themselves — a caller of the engine API constructs/owns its own
``WorkerRegistry`` and injects it directly (see ``engine.py``'s module
docstring, "engine/library boundary"). AIDO Code's own default worker
configuration is that application's responsibility, not this one's.
"""
