# Status

Snapshot factuel court — mis à jour le 2026-09-21. Pas un journal ;
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
- **Tests offline** : 1234 PASS (1186 avant + 6 renommage distribution
  PyPI + 22 attribution Git worker (P13.2) + 20 status/quota/registry
  standalone (P13.3), voir `ROADMAP.md` §13 ; ce nombre n'est pas un
  invariant permanent).
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
- **P13.1 (première exécution réelle du WorkItem Flow sur AIDO Code M1)** :
  `DONE` (2026-09-21). WI-01..WI-07 `completed` en 8 cycles, 32 tests
  finaux PASS, un recovery réel (WI-01) ; défaut moteur réel découvert
  (`FAIL`->`PASS` sur SHA identique via dérive de l'environnement de
  validation, hors Git) et corrigé (`ValidationEnvironmentEvidence`,
  `VALIDATION_ENVIRONMENT_CHANGED`, voir `docs/QA_STRATEGY.md` §8.3) ;
  attribution Git des commits workers par `display_name` ajoutée. AIDO
  Code promu deuxième projet de référence réel. Voir `ROADMAP.md` §13.
- **P13.2 (attribution Git worker renforcée après un défaut réel M1.1)** :
  `DONE` (2026-09-22). Un run réel gouverné d'AIDO Code (M1.1,
  WI-M1.1-01) a montré qu'un `git commit` imbriqué dans le backend
  `claude_code` peut ne pas hériter l'injection d'environnement seule
  (P13.1) ; `scoped_worker_git_identity` (config Git locale au workspace,
  jamais `--global`/`--system`) ajoutée en défense indépendante, plus un
  audit post-exécution fail-closed (`WorkerCommitIdentityMismatchError`)
  qui détecte toute mauvaise attribution avant QA/merge. `0e9eb96`
  (AIDO Code) non réécrit. Voir `ROADMAP.md` §13.
- **P13.3 (status opérationnel complet, quotas riches, registry
  standalone, GPT-6)** : `DONE` (2026-09-23). `aido status`/`--probe`
  affichent désormais tous les workers (prénoms/modèles) et le quota
  réel par provider (jamais dupliqué par worker, `unknown` jamais
  fabriqué) ; registry de workers par défaut packagé dans le wheel
  (`aido init` fonctionne sans checkout source, version moteur 0.1.3) ;
  Victor/Oscar passent aux modèles Codex GPT-6 réellement validés
  (`gpt-6-luna`/`gpt-6-sol`/`gpt-6-astra`) ; `MVP status=running` avec
  100% WorkItems `completed` confirmé non-bug (ReleaseManager séparé,
  P11 `À VOTER`). Voir `ROADMAP.md` §13.
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
- P13.1 : premier `aido run` réel sur AIDO Code (M1, WI-01..WI-07),
  révélant un défaut réel de preuve QA (même SHA, `FAIL` puis `PASS`,
  dérive de l'environnement de validation hors Git) — corrigé par
  `ValidationEnvironmentEvidence`/`environment_drift_reason`
  (`INCONCLUSIVE` fail-closed plutôt qu'un `PASS` silencieux) ; attribution
  Git des commits workers par `display_name` ajoutée à cette occasion
  (2026-09-21). Voir `ROADMAP.md` §13, `docs/QA_STRATEGY.md` §8.3.
- P13.2 : un run réel gouverné d'AIDO Code (M1.1, WI-M1.1-01) a montré
  que l'injection d'environnement seule (P13.1) ne suffit pas pour un
  `git commit` imbriqué dans le backend `claude_code` — `scoped_worker_
  git_identity` (config Git locale au workspace) ajoutée en défense
  indépendante, plus un audit post-exécution fail-closed
  (`WorkerCommitIdentityMismatchError`) (2026-09-22). Voir `ROADMAP.md`
  §13.
- P13.3 : le kata externe a révélé l'absence de registry de workers
  livré (`STANDALONE_RUNTIME_PASS = FAIL`) — corrigé par un registry
  packagé (`src/orchestrator/resources/default_workers.yaml`) et
  `aido init` standalone ; `aido status`/`--probe` enrichis (workers,
  quotas par provider) ; Victor/Oscar sur GPT-6 ; `MVP status=running`
  confirmé non-bug (2026-09-23). Voir `ROADMAP.md` §13.

Ce fichier reste court et factuel : pas de duplication de `ROADMAP.md`.
