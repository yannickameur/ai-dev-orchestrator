# Status

Dernière mise à jour : Slice 15 (2026-09-13).

- **Slice 14 — Apply decided RoadmapProposal + create/start next MVP :
  DONE, publiée sur `origin/main`** (commit `a5430227c623138fc8e3498378247f489e658083`).
- **Étude adaptive execution : DONE, publiée sur `origin/main`** (commit
  `bc869dbc5ef01d44293eed9815face14299316fe`). Voir
  `docs/ADAPTIVE_EXECUTION.md`.
- **Slice 15 — Configurable Worker Registry + Execution Profiles : DONE**
  (offline, tests verts). Voir `src/orchestrator/worker_registry.py`
  (nouveau) et `src/orchestrator/worker_selector.py` (`Worker` = agent
  logique seul, nouveaux `ExecutionProfile`/`QualityTier`). `Worker`
  n'a plus de `model`/`reasoning_effort` fixes — ceux-ci vivent sur un ou
  plusieurs `ExecutionProfile` par worker, déclarés dans
  `config/workers.yaml` (exemple réel, sans secret). `PyYAML` ajoutée
  comme première dépendance runtime déclarée du projet.
  `RalphExecutionEngine`/`mvp_manager.py`/`planning.py` adaptés au
  minimum nécessaire (résolution du profil par défaut avant construction
  de l'`ExecutionRequest`) — aucune sélection adaptative de profil,
  aucun estimator (Slices 16/17).
- Prochaine étape attendue : **Slice 16 — Complexity pre-flight +
  recommendations** (voir `ROADMAP.md`, « Découpage incrémental »).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
