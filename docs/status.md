# Status

Snapshot factuel court — mis à jour le 2026-10-09 (gouvernance de reprise P21.1). Pas un journal ;
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
- **Providers/workers** (`config/workers.yaml`, source de vérité) : **8
  workers actifs, 4 providers de premier niveau**, tous `VALIDATED` :
  Alice/Lydie (anthropic/claude_code), Victor/Yannick (openai/codex),
  Nathaniel/Juno (mistral/vibe, `development` uniquement), Arthur/Nora
  (gravity/agy, `development` uniquement). L'état de quota Gravity provient
  du probe read-only `agy -p "/usage" --output-format json`. Voir
  `ROADMAP.md` §13, P19/P20.
- **Tests offline** : 1 426 PASS sur `main` et sur la branche WI-P21.1-04
  à code fonctionnel identique (`pytest -q`, 2026-10-09), 0 FAIL ; un
  avertissement de collecte préexistant
  (`TestChangeAuthorization`). Snapshot, pas un contrat ; le compte
  courant fait foi dans la sortie de `pytest -q`.
- **Roman Numerals** (pilote externe) : `PASS`.
- **Mistral / Vibe** : ✅ `VALIDATED`.
- **Gravity / agy** : ✅ `VALIDATED` — spike réel (Phase A) et run réel
  Ralph+Gravity (P19-03) ; voir `ROADMAP.md` §13, sous-section P19. Le
  seul point de réserve connu (nettoyage de groupe de processus après
  une interruption réelle) est une limite pré-existante déjà documentée
  depuis P18-03, partagée avec Vibe, pas une régression P19.
- **Morpion Web 3D** (pilote externe) : `DONE`. SHA final :
  `593c615e66e6a2cb585fb465ded0185da46a3319`.
- **P12** (format de configuration de projet public `aido.yaml` + mode de
  permission d'exécution des workers project-controlled) : `DONE` — voir
  `docs/PROJECT_CONFIG.md`.
- **P1** (CLI publique `aido init/validate/run/status`) : `DONE` — plus
  besoin de harnais Python pour l'usage normal ; `aido run` est aussi la
  reprise. Cycle productisation/onboarding terminé.
- **P1.1 (guided project bootstrap / onboarding)** : `DONE` (2026-09-24).
  `aido init <parent-path> <project-name>` crée les quatre fichiers projet,
  initialise Git sur `main` et commite ; Git absent/en échec conserve le
  scaffold avec instructions de reprise et exit non nul. Mode historique
  conservé, aucun provider/runtime lancé. Test manuel réel : bootstrap,
  commit, working tree propre et `aido validate` OK. Voir `ROADMAP.md` §13
  et `docs/PROJECT_CONFIG.md`.
- **Historique de la première reprise** : P21 transport moteur terminé. AIDO Code
  M3.1 a livré ses trois WorkItems, mais l'acceptance live reste
  `PARTIAL` : une ligne JSON Claude contenant un bloc `thinking` a été
  affichée. P21.1 est `PARTIAL / RECOVERY REQUIRED`. Sa première
  tentative gouvernée a produit le commit DEV A `88dd945`, puis
  WI-P21.1-01 a échoué terminalement selon les preuves documentaires
  pendant DEV B Gravity : Ralph
  `max_iterations` après cinq itérations, `exit_code=2`, aucun verdict
  métier, aucun DEV FIX, QA non atteinte. WI-P21.1-02 est `BLOCKED`.
  Aucun de ces deux WorkItems ne sera rouvert ; `88dd945` n'est pas
  livré. La reprise documentaire définit WI-P21.1-03/04 sous le nouvel
  MVP `p21-1-recovery`. L'audit en lecture seule du
  2026-10-09 n'a retrouvé ni l'état SQLite historique déclaré sous
  `/tmp/ai-dev-orchestrator-p21-1-state`, ni sauvegarde exploitable : les
  statuts et `ExecutionRecord` historiques ne peuvent pas être revérifiés
  dans une base. L'exception `HISTORICAL_RUNTIME_STATE_UNAVAILABLE` a été
  approuvée explicitement et intégrée par la PR #41 au commit
  `ab4b01871a0c8a59e8e9b3bc53db9d7fb373ede3` : elle autorise un
  nouveau runtime indépendant sans reconstituer l'ancien historique.
  Le worktree `~/projects/ai-dev-orchestrator-p21-1-recovery` était alors
  propre sur `main` ; la configuration externe
  `~/.local/state/ai-dev-orchestrator/recovery-configs/p21-1/aido.yaml`
  y pointe, avec une nouvelle racine persistante sous
  `~/.local/state/ai-dev-orchestrator/projects/ai-dev-orchestrator-p21-1-recovery/`.
  Après un GO humain distinct, le bootstrap a réussi le 2026-10-09 avec
  `umask 077` : sept bases SQLite privées (`0600`) ont été créées dans
  la nouvelle racine indépendante (`0700`), appartenant à `jarvis`.
  Le projet `ai-dev-orchestrator-p21-1` a pour `current_mvp_id`
  `p21-1-recovery` ; WI-P21.1-03 est `ready`, WI-P21.1-04 est `planned`
  et dépend de 03. Aucun WorkItem n'est `running` ; il y a zéro
  `ExecutionRecord`. La sauvegarde initiale cohérente se trouve sous
  `~/.local/state/ai-dev-orchestrator/backups/ai-dev-orchestrator-p21-1-recovery/20261009T163740088865Z-bootstrap/` :
  les sept copies SQLite ont passé `integrity_check`, avec permissions
  privées (`0700`/`0600`). Elle est sur le même disque que l'état actif et
  ne protège pas d'une panne physique. L'ancien état historique n'a pas
  été restauré ; aucun WorkItem de reprise ni provider n'a été lancé.
  À cette étape, un GO humain distinct était requis avant l'exécution
  gouvernée ; l'approbation automatique après 20 minutes ne s'appliquait
  pas. P21.1 et M3.1 étaient `PARTIAL` et aucun code fonctionnel P21.1
  n'avait encore atterri sur `main`. Voir `ROADMAP.md`.
- **État courant P21.1 et reprise WI-05 proposée (2026-10-09)** : après
  GO explicite, WI-P21.1-03 est `COMPLETED`, livré par PR #44 sur `main`
  (`dae6e6ab0fd7928f3bc20b566adad988ddc270b3`, CI Python
  3.10/3.12 `PASS`). WI-P21.1-04 est `BLOCKED` terminal,
  `HUMAN_REVIEW_REQUIRED` après trois tentatives QA ; sa branche
  `work/wi-p21.1-04` (`a7f24722`) n'a que des changements documentaires,
  aucun correctif fonctionnel ou test à fusionner. QA 1 et 2 ont échoué
  faute de `pytest` dans l'interpréteur configuré ; QA 3 faute de `pip`
  pour les tests de packaging. Après correction locale, les 16 tests
  ciblés et les 1 426 tests offline passent avec cet interpréteur exact.
  Le nouvel environnement QA dédié hors dépôt (Python 3.12, `pip`,
  `pytest`, package éditable) passe également les 1 426 tests. Ces
  vérifications n'altèrent ni le statut ni les six `ExecutionRecord` de
  la reprise 03/04. Leur statut `succeeded` indique le verdict métier
  des phases DEV, même avec un `exit_code` non nul ; QA et WI-04 restent
  en échec.
  Les parcours jetables réels Claude/Ralph de WI-04 ont conservé les
  sorties publiques sûres et les diagnostics, produit quatre heartbeats
  et n'ont laissé aucun processus résiduel observé ; le texte des blocs
  `thinking` était vide. Les tests synthétiques
  `PRIVATE_REASONING_SENTINEL` démontrent la suppression déterministe ;
  la suppression d'un texte privé non vide en condition réelle reste à
  prouver. Un WorkItem frontend jetable ne couvre pas les trois contrats
  d'acceptance AIDO Code M3.1. Le MVP **proposé, non initialisé**
  `p21-1-acceptance-recovery` porte un nouveau `wi-p21.1-05` sans
  dépendance runtime envers WI-04, dans une racine persistante
  indépendante sous
  `~/.local/state/ai-dev-orchestrator/projects/ai-dev-orchestrator-p21-1-acceptance-recovery/`.
  Sa configuration candidate est externe, sous
  `~/.local/state/ai-dev-orchestrator/recovery-configs/p21-1-acceptance/aido.yaml`,
  et vise le worktree propre
  `~/projects/ai-dev-orchestrator-p21-1-acceptance-recovery` sur `main`.
  Aucun nouveau SQLite, bootstrap, provider ou WorkItem n'a été lancé.
  Les GO antérieurs et l'exception historique ne couvrent pas WI-05 ;
  une approbation humaine du nouveau MVP et de sa racine, puis un GO
  distinct pour son bootstrap et un autre pour son exécution gouvernée
  sont requis. L'approbation automatique de 20
  minutes ne concerne que les propositions roadmap. P21.1 reste
  `PARTIAL / RECOVERY REQUIRED`, AIDO Code M3.1 reste `PARTIAL`, M4
  n'est pas commencé. Voir `ROADMAP.md` §P21.1 pour le contrat WI-05.
- **P21 (Live worker execution observability)** : `DONE` (2026-10-06),
  WorkItems P21-01/02/03/04 livrés via le WorkItem Flow moteur et PR #36.
  Le moteur émet `execution.output`, `execution.output_truncated` et
  `execution.heartbeat` via `EngineEvent` ; `RunResult.diagnostics`
  fournit les faits typés d'un FAILED sans verdict métier. Le flux live
  est borné ; les captures et le dernier extrait diagnostique subsistent.
  CI Python 3.10/3.12 PASS. Acceptance réelle jetable Arthur/Gravity :
  62 sorties stdout progressives, deux heartbeats puis reprise ; Ralph
  `max_iterations` après cinq itérations, aucun événement métier terminal,
  `exit_code=2`, aucun commit et nettoyage correct. Aucun outil ou
  commande du provider ne peut être affirmé depuis ce flux brut ; la
  cause de cet échec d'exécution reste non établie. AIDO Code M3-03 est livré ;
  M3-01 a échoué, M3-02/M3-04 sont bloqués. Aucun état terminal n'a été
  rouvert.
- **4 providers, tous `VALIDATED`** : Anthropic/OpenAI/Mistral/Gravity.
- **P3 (DeepSeek + Kimi)** : `RETIRÉ` (P20, 2026-09-28), faute de preuve
  d'exécution réelle. Voir `ROADMAP.md` §13. **P4 (étude build-vs-reuse Mammouth AI)** :
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
  créé à ce jour ; voir `ROADMAP.md` §13. P15 reste approuvé pour étude
  et P16 pour revue ; P17 est terminé.
- **P17 (quota-aware worker routing)** : `DONE` (2026-09-26). L'unique
  `WorkerSelector` classe maintenant par pression `max(utilization connue)`
  dans une bande de 10 points, après disponibilité et gouvernance de review.
  `None` reste inconnu ; le probe existant est réutilisé. P17 est
  indépendant de P14 (aucun suivi tokens/coûts).
- **P18 (live execution events and graceful interruption)** : `DONE`
  (GO humain 2026-09-26). **P18-01/P18-02/P18-03 `DONE`** :
  `OrchestratorEngine.run(on_event=...)` live ; `EngineEvent` dans le
  module neutre `orchestrator.engine_events` (ré-exporté par
  `orchestrator.engine`, un seul type) ; DEV A/DEV B/DEV FIX/QA/Git
  émettent leurs événements réels avec métadonnées
  (worker/provider/backend/profile_id/model/quality_tier/
  reasoning_effort/commit_sha) ; `RunResult` inchangé. Interruption
  gracieuse : `posix_subprocess.run_in_new_process_group` partagé,
  `RecoveryCoordinator` étendu (jamais dupliqué) pour reconcilier aussi
  une QA interrompue. Adaptive Execution reste non branché en
  production. Voir `ROADMAP.md` §13, sous-section P18.
- **P19 (Gravity worker/backend)** : `DONE` (GO humain 2026-09-28).
  `GravityAdapter` (`EXECUTION_PROBE_ONLY`, même schéma que
  `MistralVibeAdapter`) ; `gravity_ralph_bridge.py` sur le modèle exact
  de `vibe_ralph_bridge.py`, réutilisant le mécanisme custom-backend de
  Ralph déjà prouvé par Vibe — jamais un second moteur d'exécution ;
  worker `gravity` (`enabled: true`, `priority: 101`, exception
  délibérée à un seul worker). Spike réel (Phase A) et run réel
  Ralph+Gravity (P19-03) validés. Voir `ROADMAP.md` §13, sous-section
  P19.
- **P13.5 (frontière moteur/librairie : injection du `WorkerRegistry`,
  `workers:` optionnel)** : `DONE` (2026-09-24). `ai-dev-orchestrator` ne
  considère plus `aido.yaml` comme la source de configuration complète du
  produit : `workers:` devient optionnel dans `ProjectConfig` ;
  `OrchestratorEngine`/`ProjectRuntime` acceptent un `WorkerRegistry`
  injecté par l'appelant (`worker_registry=`), `WorkerSelector` restant
  seul propriétaire de la sélection ; chemin legacy fichier intégralement
  conservé et testé ; CLI `aido` documentée comme surface legacy/
  transitoire. Voir `ROADMAP.md` §13, sous-section P13.5, et
  `docs/PROJECT_CONFIG.md`, "Engine/library boundary".
- **P13.6 (retrait de la commande produit `aido`, cutover AIDO Code)** :
  `DONE` (2026-09-24). `ai-dev-orchestrator` n'installe plus aucune
  commande console (`[project.scripts]` retiré de `pyproject.toml`) ;
  AIDO Code en devient l'unique propriétaire. `orchestrator.cli`/
  `default_workers.yaml` restent dans le code source, legacy/internes
  (tests, développement dual-repo), jamais supprimés. Aucun binaire de
  compatibilité ajouté (YAGNI). Voir `ROADMAP.md` §13, sous-section
  P13.6.
- **P13.7 (pre-execution state safety)** : `DONE` (2026-09-25). Défaut
  réel révélé par le cutover M8 d'AIDO Code (`WI-M8-01` resté `RUNNING`
  durablement, sans `ExecutionRecord`) corrigé : `mark_mvp_running`/
  `mark_work_item_running` déplacés après tous les prérequis
  pré-exécution dans `_execute_work_item` et `_resume_dev_b_wait` (deux
  sites réels). Invariant de recovery existant inchangé ; `WI-M8-01` non
  modifié rétroactivement (YAGNI). Voir `ROADMAP.md` §13, sous-section
  P13.7.

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
- P3 : DeepSeek et Kimi intégrés (2026-09-19) puis retirés (P20,
  2026-09-28) faute de preuve d'exécution réelle.
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
- P13.5 : frontière moteur/librairie — `workers:` rendu optionnel dans
  `ProjectConfig` (`NoWorkerRegistryConfiguredError` si absent et non
  injecté) ; `OrchestratorEngine`/`ProjectRuntime` acceptent un
  `WorkerRegistry` construit et injecté par l'appelant, sans jamais lire
  `workers.yaml` dans ce cas ; `WorkerSelector` reste l'unique
  propriétaire de la sélection ; chemin legacy fichier inchangé et
  toujours testé (2026-09-24). Voir `ROADMAP.md` §13.

Ce fichier reste court et factuel : pas de duplication de `ROADMAP.md`.
