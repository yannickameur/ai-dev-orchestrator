# Status

Dernière mise à jour : Slice 20 (2026-09-13).

- **Slice 20 — Git/PR/merge governance : DONE.** Nouveau
  `src/orchestrator/git_governance.py` (`LocalGitWorkspace`,
  `GitGovernancePolicy`, `GitWorkItemRecord`/`GitWorkItemStore`,
  `GitGovernanceService`) — l'orchestrateur devient propriétaire de la
  branche/du SHA/de l'éligibilité au merge d'un WorkItem ; Ralph reste le
  moteur d'exécution, les workers ne décident jamais eux-mêmes de la
  branche, du merge, ou de la mergeabilité. Branche gouvernée
  déterministe et stable après restart (`work/<work-item-id>`), `main`
  protégée par défaut, `base_sha` immuable, preuve de merge liée au SHA
  exact (gate + review), fast-forward-only par défaut (jamais de rebase/
  force/reset automatique ; une divergence fait échouer proprement),
  `auto_merge=False` par défaut (testé réellement à `True` sur repos
  temporaires). Intégration opt-in dans `MVPManager` (fresh + rework +
  reprise WAITING + reprise RECOVERY, un seul point de câblage partagé),
  `ReleaseManager` (nouveau check `governed-work-items-merged`) et
  `RealizationReport` (branche/SHA/statut de merge/PR + événements
  d'audit dans la timeline). Abstraction PR optionnelle (`gh` CLI, argv
  explicite) — une PR n'implique jamais une autorisation de merge ;
  aucune vraie PR/push distant créé cette session. Smoke local réel PASS
  (dépôt temporaire, sans provider LLM additionnel). 876 tests offline
  PASS (790 + 86). Voir `ROADMAP.md`, Slice 20, et
  `docs/GIT_GOVERNANCE.md`.
- **Slice 19 — Adaptive review/planning integration : DONE.** Review
  (fresh + reprise `WaitPhase.REVIEW` + reprise `RECOVERY_REQUIRED`, un
  seul point de câblage via `MVPManager._run_review` déjà partagé par les
  trois chemins) et release planning/roadmap synthesis
  (`PlanningCoordinator`, planner **et** synthesizer — les deux confirmés
  être de vraies exécutions Ralph/LLM, jamais une synthèse déterministe
  dans ce dépôt) passent désormais par la même chaîne adaptative que
  development/rework (Slice 17) : pre-flight indépendant par rôle
  (fingerprint Slice 16), `minimum_quality_tier`, `resolve_profile()`
  réutilisée sans seconde implémentation, `AdaptiveExecutionDecision`
  persistée avant toute exécution. `AdaptiveExecutionSelector.select()`
  gagne un paramètre optionnel `author_worker_id` (rétrocompatible) qui
  active la politique cross-provider existante de `WorkerSelector` sans
  jamais la réimplémenter. Aucun profil `CRITICAL` fabriqué (testé
  explicitement) ; `ApprovalCoordinator`/`RoadmapApplicationService`/
  `RealizationReport`/`ActivityReport` non touchés (`RealizationReport`
  agrège déjà les rôles sans filtrage — prouvé par un nouveau test, pas
  par une modification). `config/workers.yaml` non modifié. Voir
  `ROADMAP.md`, Slice 19, et `docs/ADAPTIVE_EXECUTION.md`. 790 tests
  offline PASS (772 + 18). Smoke réel : non relancé (offline uniquement,
  comme demandé).
- **Slice 18.5 — Stabilisation pré-Slice 19 : DONE.** Deux corrections
  factuelles trouvées lors de l'audit OmniRoute : (1) mismatch
  `REVIEW_CAPABILITY` ("reviewer") vs la capability réelle
  `config/workers.yaml` ("code_review") — corrigé, `code_review` est
  désormais la seule convention canonique, testé contre le vrai fichier
  de config ; (2) `reasoning_effort` réellement vérifié comme supporté
  côté `claude_code` via le vrai flag `claude --effort <level>` — transmis
  désormais comme pour `codex`, aucun changement de comportement observable
  tant qu'aucun profil Claude ne définit `reasoning_effort`. Voir
  `ROADMAP.md`, Slice 18.5.
- **Slice 17 — Adaptive Worker/Profile Selection for development/rework :
  DONE.** `WorkerSelector`/`AdaptiveExecutionSelector`/`resolve_profile()`
  couvrent DEVELOPMENT et REWORK, y compris les chemins de reprise
  (`_try_resume_due_wait`, `_try_resume_recovery_required`) — jamais de
  downgrade silencieux, jamais `Worker.profile()` par défaut quand
  l'adaptive execution est activée. Review, release planning et roadmap
  synthesis rendus adaptatifs depuis par la Slice 19 (ci-dessus).
- **Slice 18 — Realization reports + real cross-worker cold-resume
  acceptance : DONE.** Voir `src/orchestrator/realization_report.py`
  (nouveau : `RealizationReport`, `RealizationReportStore`,
  `RealizationReportService`) — un snapshot déterministe et auditable
  d'un WorkItem (timeline, executions, recommendations/adaptive
  decisions, handoffs, quality gates, reviews, waits, incidents), rendu
  en HTML autonome (`render_html()`, aucun CDN/JS externe, déterministe,
  jamais LLM-généré) ; insert-only, un rapport intermédiaire
  (`HANDOFF_READY`) n'est jamais écrasé par le rapport final. Ne remplace
  pas `ActivityReport` (consolidation release/MVP) ; `HandoffRecord` reste
  l'unique artefact machine de passation.
- **Cross-worker cold-resume E2E (validation d'acceptation Slice 17/18,
  2026-09-13)** :
  - offline automatisé (`tests/integration/test_cross_worker_resume_e2e.py`) :
    **PASS**
  - **smoke réel (Claude/Codex) : PASS** (`scripts/smoke_cross_worker_real.py`,
    manuel, jamais lancé par `pytest`) — deux vrais workers de
    `config/workers.yaml`, deux process OS réellement distincts :
    `alice` (anthropic/claude_code, `haiku`, tier `SIMPLE`) puis `victor`
    (openai/codex, `gpt-5.6-terra`, `reasoning_effort=low`, tier
    `SIMPLE`) ; handoff réellement transmis ; quality gate réel PASSED ;
    aucun downgrade ; aucun bug Slice 17/18 découvert. Preuve versionnée
    (sanitizée) : `docs/reports/real-cross-worker-resume-2026-09-13.html`
  - `~/projects/ralph-spike` original : non modifié (vérifié avant/après)
- **Next : revue de roadmap avec l'utilisateur.** Toutes les Slices 7-20
  de la Phase 1 sont DONE — point de contrôle prévu par le projet avant
  toute nouvelle Slice. Aucune Slice 21 n'est décidée ici. OmniRoute reste
  une qualification future optionnelle, hors roadmap principale (voir
  `docs/OMNIROUTE_ARBITRATION.md`).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
