# Status

Dernière mise à jour : Slice 16 (2026-09-13).

- **Slice 15 — Configurable Worker Registry + Execution Profiles : DONE,
  publiée sur `origin/main`** (commit `9d4424546e714cb50b6d7056ca28e3de62c4a832`).
- **Slice 16 — Complexity pre-flight + persistent execution
  recommendations : DONE** (offline, tests verts). Voir
  `src/orchestrator/complexity_estimation.py` (nouveau :
  `ExecutionRecommendationService`, `ExecutionRecommendationStore`,
  `ExecutionRecommendation`, `ComplexityEstimationRequest`). Séquence
  Option C : un Worker estimator (capability `complexity_estimation`,
  sélectionné via `WorkerSelector` existant, jamais codé en dur) est
  exécuté via `RalphExecutionEngine` (comme un planner/synthesizer) et
  émet `execution.profile_recommended` (`minimum_quality_tier`,
  `recommended_reasoning` optionnel, `reasons` — jamais un modèle/
  provider/worker choisi). Fingerprint sha256 déterministe implémenté
  (role/objective/acceptance criteria/handoff/git_sha/review findings) :
  une recommandation identique n'est jamais recalculée
  (`force_refresh=False` par défaut). Fail-closed partout (tier inconnu,
  payload invalide, pas d'estimator disponible, `estimator_profile_id`
  non configuré). Aucune sélection finale de worker/profile de
  développement, aucune intégration dans `MVPManager` — service autonome,
  testable isolément.
- Prochaine étape attendue : **Slice 17 — Adaptive Worker/Profile
  Selection integration (development)** (voir `ROADMAP.md`, « Découpage
  incrémental »).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
