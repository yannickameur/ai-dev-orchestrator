# Status

Dernière mise à jour : Slice 18 (2026-09-13).

- **Slice 17 — Adaptive Worker/Profile Selection for development/rework :
  DONE.** `WorkerSelector`/`AdaptiveExecutionSelector`/`resolve_profile()`
  couvrent DEVELOPMENT et REWORK, y compris les chemins de reprise
  (`_try_resume_due_wait`, `_try_resume_recovery_required`) — jamais de
  downgrade silencieux, jamais `Worker.profile()` par défaut quand
  l'adaptive execution est activée. Seule la reprise **review** reste non
  adaptative (Slice 19).
- Review, release planning et roadmap synthesis restent sur une
  sélection **non adaptative** (profil par défaut) — Slice 19 les
  couvrira.
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
- Prochaine étape attendue : **Slice 19 — Adaptive review + release
  planning/synthesis profiles** (voir `ROADMAP.md`, « Découpage
  incrémental » ; Git/PR/merge governance reste décalée en Slice 20).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
