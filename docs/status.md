# Status

Dernière mise à jour : Slice 17 (2026-09-13).

- **Slice 16 — Complexity pre-flight + persistent execution
  recommendations : DONE, publiée sur `origin/main`** (commit
  `229a09ec8ad431ecf17a005c63ab66433c85f7ee`).
- **Slice 17 — Adaptive Worker/Profile Selection for development/rework :
  DONE** (offline, tests verts). Voir `src/orchestrator/adaptive_execution.py`
  (nouveau : `AdaptiveExecutionSelector`, `AdaptiveExecutionDecisionStore`,
  `AdaptiveExecutionDecision`, `resolve_profile`). Le développement et le
  rework passent désormais réellement par un pre-flight
  (`ExecutionRecommendationService`, Slice 16) puis une sélection
  adaptative : `WorkerSelector` (étendu avec `minimum_quality_tier`)
  choisit un worker capable d'atteindre le tier requis, puis
  `resolve_profile()` choisit le profil le moins coûteux qui satisfait ce
  tier (préférence `reasoning_effort` exacte quand possible, jamais de
  dégradation de tier). Toute décision est persistée
  (`AdaptiveExecutionDecision`, insert-only, auditable). Distinction
  WAITING (quota temporaire) vs incapacité structurelle (aucun profil
  suffisant configuré, jamais un `WAITING` fabriqué) obtenue sans code
  supplémentaire grâce au filtrage par tier appliqué avant toute diagnose
  de quota dans `WorkerSelector`. Intégré dans `MVPManager` en opt-in
  (paramètre `adaptive_execution_selector`), sur DEVELOPMENT et REWORK —
  y compris les chemins de reprise (`_try_resume_due_wait`,
  `_try_resume_recovery_required`), qui repassent par le même pre-flight/
  la même sélection adaptative à chaque tentative plutôt que par
  `Worker.profile()` par défaut (jamais de downgrade silencieux après une
  attente/récupération). Seule la reprise **review** reste non adaptative
  (Slice 18).
- Review, release planning et roadmap synthesis restent sur une
  sélection **non adaptative** (profil par défaut) — Slice 18 les
  couvrira.
- **Cross-worker cold-resume E2E (validation d'acceptation Slice 17,
  2026-09-13)** — `tests/integration/test_cross_worker_resume_e2e.py` :
  - offline automatisé : **PASS** (copie jetable de `~/projects/ralph-spike`,
    Worker A interrompu -> fermeture/réouverture réelle de tous les stores
    sqlite -> `RecoveryCoordinator` -> Worker B différent, sélection
    adaptative sans downgrade -> code réellement corrigé sur disque ->
    tests du mini-projet réellement verts)
  - smoke réel (Claude/Codex) : **NOT RUN** — aucun reset credit/quota
    réel consommé sans confirmation explicite ; la preuve automatique
    offline suffit pour cette validation
  - `~/projects/ralph-spike` original : non modifié (vérifié)
- Prochaine étape attendue : **Slice 18 — Adaptive review + release
  planning/synthesis profiles** (voir `ROADMAP.md`, « Découpage
  incrémental »).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
