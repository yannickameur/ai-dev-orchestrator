"""Packaged, non-Python resources shipped inside the ``ai-dev-orchestrator``
wheel. Currently just ``default_workers.yaml`` (P13.3, see ROADMAP.md) —
a starter worker registry template consumed only by the legacy
``orchestrator.cli`` module's own ``init``/scaffold path, so it works
standalone (pip install only, no sibling source checkout).

LEGACY, INTERNAL ONLY: this is never the modern product's worker
configuration source of truth. Since the product cutover (AIDO Code owns
the ``aido`` console script — see ``README.md``, "engine/library
boundary"), this distribution no longer installs any console script at
all; ``orchestrator.cli``/this resource are reachable only via a direct
Python import (existing internal tests), never a real end-user command.
``OrchestratorEngine``/``ProjectRuntime`` never read this file
themselves — a caller of the engine API constructs/owns its own
``WorkerRegistry`` and injects it directly (see ``engine.py``'s module
docstring, "engine/library boundary"). AIDO Code's own default worker
configuration is that application's responsibility, not this one's.
"""
