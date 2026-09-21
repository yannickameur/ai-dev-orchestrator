# Status

Snapshot factuel court — mis à jour le 2026-09-19. Pas un journal ;
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
- **Providers/workers** (`config/workers.yaml`, source de vérité) : 8
  workers déclarés (6 activés), **5 providers de premier niveau** :
  `alice`/`bob` (anthropic/claude_code), `victor`/`oscar` (openai/codex),
  `milo`/`juno` (mistral/vibe, `development` uniquement), `dana`/`kai`
  (deepseek/kimi via `claude_code` redirigé, `development` uniquement,
  **`enabled: false`**, clé API requise, pas encore de preuve
  d'exécution réelle ; voir `ROADMAP.md` §7/§13).
- **Tests offline** : 1157 PASS (1135 avant + 22 pour la façade moteur
  `OrchestratorEngine`, snapshot
  courant, voir §11 de `ROADMAP.md` pour la méthode de comptage ; ce
  nombre n'est pas un invariant permanent).
- **Roman Numerals** (pilote externe) : `PASS`.
- **Mistral / Vibe** : ✅ `VALIDATED`.
- **Morpion Web 3D** (pilote externe) : `DONE`. SHA final :
  `593c615e66e6a2cb585fb465ded0185da46a3319`.
- **P12** (format de configuration de projet public `aido.yaml` + mode de
  permission d'exécution des workers project-controlled) : `DONE` — voir
  `docs/PROJECT_CONFIG.md`.
- **P1** (CLI publique `aido init/validate/run/status`) : `DONE` — plus
  besoin de harnais Python pour l'usage normal ; `aido run` est aussi la
  reprise. Cycle productisation/onboarding terminé.
- **Développement actif** : aucun.
- **5 providers implémentés, 3 validés, 2 en attente de validation réelle** :
  Anthropic/OpenAI/Mistral `VALIDATED` ; DeepSeek/Kimi `IMPLEMENTED — REAL
  VALIDATION PENDING`.
- **P3 (DeepSeek + Kimi comme providers de premier niveau)** :
  implémentation `DONE`, validation réelle `PENDING` (2026-09-19). Voir
  `ROADMAP.md` §7/§13. **P4 (étude build-vs-reuse Mammouth AI)** :
  `RETIRÉ`. Étude menée, agrégateur jugé d'intérêt économique/
  architectural insuffisant face à l'intégration directe de providers
  (décision utilisateur, 2026-09-19), aucune dépendance gateway/agrégateur
  multi-modèles.
- **P13 (découplage moteur / externalisation AIDO Code), priorité 1** :
  `DONE` (2026-09-19). Façade publique `orchestrator.engine.
  OrchestratorEngine` ; dépôt `aido-code` créé (`~/projects/aido-code`,
  roadmap/`MVP_SPEC.yaml`/WorkItems M1 préparés, aucun code fonctionnel
  écrit, aucun `aido run` lancé). Voir `ROADMAP.md` §13.
- **P14 (observabilité de consommation et efficacité économique)** :
  `APPROUVÉ — APRÈS P13` (2026-09-19). Aucun WorkItem d'implémentation
  créé à ce jour ; voir `ROADMAP.md` §13. Toutes les autres propositions
  restent `À VOTER`.

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
  `docs/PROJECT_CONFIG.md`.
- P1 implémenté : CLI publique `aido` (`init`/`validate`/`run`/`status`),
  nouvelle couche de composition `orchestrator.project_runtime.ProjectRuntime`
  (`ProjectConfig` -> stores/services réels -> `MVPManager` réel, jamais
  un second orchestrateur), point d'entrée `[project.scripts]`. Validé
  entièrement hors ligne, y compris un WorkItem Flow complet (DEV A →
  DEV B → QA déterministe → merge gouverné → `COMPLETED`) et une reprise
  multi-process après `WAITING`, via des adaptateurs providers et un
  subprocess Ralph faux — jamais de vrai Claude/Codex/Vibe/Ralph
  (2026-09-19). Cycle productisation/onboarding (P1 + P12) `DONE`.
- P4 (étude Mammouth) menée puis close `RETIRÉ` : agrégateur jugé d'intérêt
  économique/architectural insuffisant face à l'intégration directe de
  providers (décision utilisateur, 2026-09-19).
- P3 : DeepSeek et Kimi intégrés comme providers de premier niveau, sur
  cette base, en réutilisant `ClaudeCodeAdapter` (redirection
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` documentée officiellement par
  les deux providers) plutôt qu'un second client HTTP ; workers `dana`/`kai`
  ajoutés à `config/workers.yaml`, désactivés par défaut (clé API requise,
  pas encore de preuve d'exécution réelle). Voir `ROADMAP.md` §7/§13.
- P13 (priorité 1) : découplage moteur / externalisation AIDO Code.
  Façade publique `orchestrator.engine.OrchestratorEngine` exposée,
  masquant `ProjectRuntime`/`MVPManager`/`WorkerSelector`/`QuotaManager`/
  `ProviderAdapter`/`GitGovernanceService`/`InternalQAEngine`/toute Store
  derrière des snapshots typés ; projet `aido-code` créé
  (`~/projects/aido-code`, dépôt Git local séparé, roadmap/
  `MVP_SPEC.yaml`/WorkItems M1 préparés), aucun code fonctionnel écrit,
  aucun `aido run` lancé. `DONE` (2026-09-19).
- P14 (observabilité de consommation et efficacité économique) approuvé
  par l'utilisateur pour après P13 ; aucun WorkItem d'implémentation créé
  à ce jour (2026-09-19). Voir `ROADMAP.md` §13.

Ce fichier reste court et factuel : pas de duplication de `ROADMAP.md`.
