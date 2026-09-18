# Status

Snapshot factuel court — mis à jour le 2026-09-18. Pas un journal ;
l'historique détaillé daté (Slices, incidents, diagnostics) vit dans
l'historique Git (`git log`) et dans les rapports sous `docs/reports/`.
Voir `ROADMAP.md` pour la source de vérité fonctionnelle complète.

## État actuel

- **Release** : v0.1.1 — **PUBLIÉE** (première release publique open
  source).
- **MVP 0.1** : `DONE`. Contrat d'acceptation : `MVP_SPEC.yaml` v4.
- **Phase 1** : `DONE`.
- **Workflow** : WorkItem Flow — le seul workflow d'exécution de WorkItem
  implémenté (`GOVERNED_FULL` retiré avant la première release publique,
  2026-09-18 ; `WorkflowMode` lui-même supprimé, un seul mode restant).
- **Providers/workers** (`config/workers.yaml`, source de vérité) : 6
  workers, 3 providers — `alice`/`bob` (anthropic/claude_code),
  `victor`/`oscar` (openai/codex), `milo`/`juno` (mistral/vibe,
  `development` uniquement).
- **Tests offline** : 1048 PASS (983 avant + 65 pour P12 — snapshot
  courant, voir §11 de `ROADMAP.md` pour la méthode de comptage ; ce
  nombre n'est pas un invariant permanent).
- **Roman Numerals** (pilote externe) : `PASS`.
- **Mistral / Vibe** : ✅ `VALIDATED`.
- **Morpion Web 3D** (pilote externe) : `DONE`. SHA final :
  `593c615e66e6a2cb585fb465ded0185da46a3319`.
- **P12** (format de configuration de projet public `aido.yaml` + mode de
  permission d'exécution des workers project-controlled) : `DONE` — voir
  `docs/PROJECT_CONFIG.md`.
- **Développement actif** : aucun.
- **Prochain WorkItem approuvé** : P1 — CLI publique, consommant
  `ProjectConfig` (P12). P3 (providers/workers supplémentaires) et P4
  (étude build-vs-reuse Mammouth AI, en premier, sous REUSE FIRST) restent
  approuvés pour après P1. Aucun WorkItem d'implémentation de P1 créé à
  ce jour — voir `ROADMAP.md` §13, « Cycle produit approuvé » / « Ordre
  approuvé ». Toutes les autres propositions restent `À VOTER`.

## Historique synthétique

Chronologie détaillée entièrement récupérable via `git log` et
`docs/reports/`. Jalons majeurs :

- Spécification / étude d'écosystème, décision de réutiliser Ralph.
- Orchestration MVP durable (Project/MVP/WorkItem).
- Reprise durable (WAITING/RECOVERY_REQUIRED), gouvernance Git,
  gouvernance QA déterministe.
- Introduction de WorkItem Flow (alors nommé `LEAN_FEATURE_FLOW`),
  devenu le workflow par défaut puis unique.
- Clôture MVP 0.1 / Phase 1 (2026-09-17).
- Validation externe Roman Numerals — `PASS` (2026-09-17).
- Validation Mistral/Vibe — ✅ `VALIDATED` (2026-09-18), y compris usage
  réel comme DEV B avec gouvernance de commit validée.
- Pilote externe Morpion Web 3D — `DONE` (2026-09-18), avec une
  régression navigateur découverte et corrigée après un acceptance
  initial incomplet (gap de couverture spécifique au projet, pas une
  faille des primitives QA de l'orchestrateur) ; récit complet public :
  `examples/morpion-web-3d/README.md`.
- `GOVERNED_FULL` retiré avant la première release publique
  (2026-09-18) — pipeline superseded par WorkItem Flow, complexité
  inutile, aucun besoin produit actuel (KISS/YAGNI).
- Normalisation terminologique WorkItem Flow + réécriture de
  `ROADMAP.md` (2026-09-18).
- v0.1.1 publiée en open source, dépôt GitHub public, `main` protégée
  par ruleset CI (2026-09-18).
- Cycle produit « productisation/onboarding » (P1 CLI + P12 format de
  configuration de projet, avec exigence de mode de permission
  d'exécution des workers explicite et contrôlé par le projet) approuvé
  par l'utilisateur ; P3/P4 approuvés pour après ce cycle (2026-09-18) —
  voir `ROADMAP.md` §13.
- P12 implémenté : `orchestrator.project_config.ProjectConfig`
  (`aido.yaml` schema v1) et `orchestrator.execution_policy.ExecutionPermissionMode`
  (`standard`/`unrestricted`, traduit en flags CLI vérifiés à la
  frontière `RalphExecutionEngine` ; Vibe n'est plus jamais
  unconditionnellement `--auto-approve`) ; audit d'exécution persistant
  avec migration SQLite rétrocompatible (2026-09-19) — voir
  `docs/PROJECT_CONFIG.md`. P1 (CLI) reste le prochain WorkItem, pas
  démarré.

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
