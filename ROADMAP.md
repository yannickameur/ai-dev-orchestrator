# ROADMAP — AI Dev Orchestrator

Ce fichier est la **source de vérité fonctionnelle courante** du projet :
ce que le produit est, ce qui est implémenté aujourd'hui, comment ça
marche, ce qui est approuvé pour la suite (voir §13, « Cycle produit
approuvé » et « Ordre approuvé ») et ce qui reste proposé — pas encore
approuvé — en attente d'un vote explicite (§13, table des propositions).

L'historique d'implémentation détaillé (Slices, décisions produit datées,
diagnostics forensiques) vit dans l'historique Git (`git log`) et dans
`docs/status.md`/les rapports sous `docs/reports/`, pas ici. Ce fichier a
été réécrit substantiellement le 2026-09-18 (préparation de la première
release publique) pour rester lisible en une seule lecture ; la version
précédente, beaucoup plus longue, reste entièrement récupérable via
`git log -p -- ROADMAP.md`.

## 1. Vision

```
UN PROJET
  + UNE ROADMAP VERSIONNÉE
  + UN ÉTAT D'EXÉCUTION PERSISTANT
  + DES WORKERS IA INTERCHANGEABLES
  + UNE GOUVERNANCE GIT
  + UNE QA DÉTERMINISTE
```

AI Dev Orchestrator est une couche mince de gouvernance/quota/sélection
au-dessus de Ralph Orchestrator (le CLI `ralph`, qui fait le travail
d'exécution fin — itérations, hats, TDD). Cette couche décide *qui* fait
*quoi*, *quand c'est vraiment terminé*, et *comment c'est intégré à Git*
— elle ne réimplémente jamais l'exécution elle-même.

Principe central : un développeur IA seul n'est jamais l'autorité finale
(`LLM IS NOT ORACLE`). Un second développeur indépendant corrige le
premier, et une QA déterministe et non-LLM décide seule du verdict
PASS/FAIL — jamais l'auto-déclaration d'un worker.

## 2. Version actuelle

- **v0.1.1** — **PUBLIÉE**, première release publique open source.
- **MVP 0.1** : `DONE` (contrat d'acceptation : `MVP_SPEC.yaml` v4).
- **Phase 1** : `DONE`.
- Suite de tests offline : snapshot daté dans `docs/status.md` (ce
  compte évolue à chaque changement ; voir §13 pour l'historique par
  incrément, `pytest -q` pour le nombre exact courant).
- **P12 (format de configuration de projet public + mode de permission
  d'exécution des workers) : `DONE`**, voir §10 et `docs/PROJECT_CONFIG.md`.
  **P1 (CLI publique `aido`) : `DONE`** (`aido init/validate/run/status`,
  voir §10). Cycle productisation/onboarding terminé.
  **P1.1 (guided project bootstrap / onboarding) : `DONE`** (2026-09-24),
  extension locale de `aido init`, voir §13.
- **P3 (DeepSeek + Kimi) : `RETIRÉ`** (décision produit 2026-09-28, P20), voir
  §13. **P4 (étude Mammouth) : `RETIRÉ`** : étude menée, agrégateur jugé
  d'intérêt économique/architectural insuffisant, intégration directe
  préférée (décision utilisateur, 2026-09-19).
- **P13 (découplage moteur / externalisation AIDO Code), priorité 1 :
  `DONE`** (2026-09-19) — voir §10 et §13. L'orchestrateur expose
  désormais une façade publique (`orchestrator.engine.OrchestratorEngine`)
  qu'un frontend externe (AIDO Code) peut consommer sans jamais construire
  `MVPManager`/`WorkerSelector`/`QuotaManager` lui-même. Aucun
  développement fonctionnel d'AIDO Code n'a été lancé par cette tâche.
- **P14 (observabilité de consommation et efficacité économique) :
  `APPROUVÉ — APRÈS P13`** (2026-09-19), voir §13. Aucun WorkItem
  d'implémentation créé à ce jour ; capacité moteur, jamais recalculée
  côté AIDO Code.
- **P13.5 (frontière moteur/librairie : injection du `WorkerRegistry`,
  `workers:` optionnel) : `DONE`** (2026-09-24), voir §13. `aido.yaml` ne
  possède plus obligatoirement le pool de workers ; `OrchestratorEngine`/
  `ProjectRuntime` acceptent un `WorkerRegistry` injecté par l'appelant,
  chemin legacy fichier intégralement conservé.
- **P13.6 (retrait de la commande produit `aido`, cutover AIDO Code) :
  `DONE`** (2026-09-24), voir §13. `ai-dev-orchestrator` n'installe plus
  aucune commande console ; AIDO Code devient l'unique propriétaire de
  `aido` ; `orchestrator.cli`/`default_workers.yaml` restent dans le code
  source, legacy/internes, jamais supprimés.
- **P13.7 (pre-execution state safety) : `DONE`** (2026-09-25), voir §13.
  Défaut réel révélé par le cutover M8 d'AIDO Code (`WI-M8-01`) corrigé :
  un WorkItem/son MVP ne sont plus marqués `RUNNING` avant que tous les
  prérequis pré-exécution (préparation Git notamment) aient réellement
  réussi — deux sites corrigés (`_execute_work_item`,
  `_resume_dev_b_wait`). Invariant de recovery existant inchangé.
- **P13.4 (remédiation post-audit externe) : `DONE`** (2026-09-23), voir
  §13. 8/11 findings corrigés (dont le seul HIGH : protection de tests
  réellement câblée dans `aido run`), 2 documentés/différés (YAGNI), 1
  classé P16 (candidat DELETE). Rapport complet :
  `docs/reports/mistral-engine-audit-2026-09-23.md`.
- **P15 (étude prompt optimization externe, Opik Optimizer) :
  `APPROUVÉ POUR ÉTUDE`** (2026-09-23), voir §13. Pas d'intégration.
- **P16 (revue YAGNI/REUSE FIRST structurée) : `APPROUVÉ POUR REVUE`**
  (2026-09-23), voir §13. Pas de refactor global autorisé par ce seul
  vote.
- **P17 (quota-aware worker routing) : `DONE`** — `WorkerSelector` utilise
  les fenêtres observées pour départager les providers disponibles après
  les filtres qualité et gouvernance de review. Indépendant de P14.
- **P18 (live execution events and graceful interruption) :
  `DONE`** (GO humain 2026-09-26), voir §13. Transport `on_event`,
  métadonnées DEV A/B/FIX/QA/Git et interruption/recovery P18-03 livrés.
- **P21 (Live worker execution observability) : `DONE`** (2026-10-06),
  voir §13. Sorties worker progressives et bornées, événements publics,
  diagnostic d'échec typé et heartbeat livrés dans le moteur. Le rendu
  terminal a été livré dans AIDO Code M3.1. Son acceptance réelle a révélé
  un défaut de sûreté du contenu public ; voir P21.1 proposé ci-dessous.
- **P21.1 (Safe public execution output) : `APPROUVÉ`, implémentation
  autorisée** (GO humain 2026-10-06), voir §13. Séparer l'observation
  publiquement affichable des captures brutes à la frontière d'exécution
  moteur.

## 3. Ce qui existe aujourd'hui

- Orchestration durable Project / MVP / WorkItem (`ProjectStateStore`,
  SQLite, survit à un redémarrage).
- `WorkerRegistry` — pool de workers déclaratif (`config/workers.yaml`),
  aucun worker codé en dur.
- `WorkerSelector` — capacités/exclusions/qualité > disponibilité provider
  > indépendance de review > pression quota observée (bande 10 points) >
  priorité > `worker_id` ; le coût reste hors du choix du worker.
- Gestion de quota consciente du provider (`QuotaManager` +
  `ProviderAdapter` par provider, re-probe réel, jamais un reset simulé).
- Adaptateur Claude Code (Anthropic).
- Adaptateur Codex CLI (OpenAI).
- Adaptateur Mistral/Vibe — honnêtement limité à `EXECUTION_PROBE_ONLY`
  (aucune fenêtre de quota observable pour ce provider ; jamais fabriquée).
- Adaptateur Gravity (`agy`) — quota réel via le probe read-only
  `agy -p "/usage" --output-format json` (P20, voir §13).
- DeepSeek et Kimi : retirés (P20, §13), jamais validés par une exécution
  réelle.
- `RalphExecutionEngine` — chaque exécution passe par le vrai CLI `ralph`,
  jamais un appel direct à un provider.
- État durable Execution/Handoff/Wait/Recovery — reprise après
  interruption/redémarrage, sans retry aveugle.
- `GitGovernanceService` — branche/merge/tag gouvernés, jamais par un
  worker directement.
- QA déterministe / `QAVerdict` (`InternalQAEngine`, non-LLM, lecture
  seule).
- Protection de tests / preuve à SHA exacte (`qa_protection.py`).
- WorkItem Flow — le workflow d'exécution de WorkItem (voir §5).
- Reporting d'activité/réalisation (`ActivityReport`/`RealizationReport`).
- Capacités optionnelles de planning/approbation/application de roadmap
  (voir §10 — construites, jamais enchaînées automatiquement).
- `ProjectConfig` (P12, `workers:` optionnel depuis P13.5) — format de
  configuration public `aido.yaml` schema v1 : identité de projet,
  référence *optionnelle et legacy* (jamais copie) au pool de workers,
  mode de permission d'exécution project-controlled, politique Git,
  MVP/WorkItems, commandes QA déterministes. `ExecutionPermissionMode`
  (`standard`/`unrestricted`), traduit en flags CLI réels et vérifiés
  exclusivement à la frontière `RalphExecutionEngine` (`docs/PROJECT_CONFIG.md`)
  — Vibe n'est plus jamais unconditionnellement `--auto-approve`. Un
  `aido.yaml` sans section `workers:` est un `ProjectConfig` moderne
  valide dont le `WorkerRegistry` est fourni à l'exécution par
  l'application appelante (§10, P13.5) — jamais requis pour l'API moteur.
- CLI historique `orchestrator.cli` (P1, `init`/`validate`/`run`/`status`)
  — **`aido init/validate/run/status`, plus besoin de harnais Python pour
  l'usage normal**, restent la description exacte de ce que P1 a
  construit et prouvé ; `run` est aussi la reprise (pas de commande
  `resume` séparée). **Depuis P13.6, cette commande n'est plus installée
  par `ai-dev-orchestrator`** — AIDO Code (`aido`) en est l'unique
  propriétaire produit ; ce module reste importable, legacy/interne
  uniquement (tests, développement dual-repo). Voir §10/§13 (P13.6) pour
  le détail complet.
- Bootstrap projet guidé (P1.1) — `aido init <parent-path> <project-name>` :
  README/ROADMAP/configuration/Git local, onboarding et reprise manuelle si
  Git manque ou échoue. Mode historique conservé ; aucune exécution IA.
- Façade moteur publique `orchestrator.engine.OrchestratorEngine` (P13) —
  `.open()`/`.validate()`/`.status()`/`.workers()`/`.probe_workers()`/
  `.run()`/`.close()`, masquant `ProjectRuntime`/`MVPManager`/
  `WorkerSelector`/`QuotaManager`/`ProviderAdapter`/`GitGovernanceService`/
  `InternalQAEngine`/toute Store derrière des snapshots typés,
  sérialisables, jamais un objet interne. Le CLI `aido` existant reste
  intact et n'est pas migré vers cette façade par P13. Depuis P13.5,
  `.open()`/`__init__` acceptent un `worker_registry:
  WorkerRegistry | None` injecté par l'appelant — `WorkerSelector` reste
  l'unique propriétaire de la sélection, jamais réimplémentée dans la
  façade. Voir §10.

Seules les capacités qui existent réellement dans le dépôt au commit
`47ab4da` (et après) sont listées ici.

## 4. Architecture actuelle

```
Project / MVP / WorkItem
        ↓
MVPManager
        ↓
WorkerSelector
        ↓
QuotaManager / ProviderAdapters
        ↓
RalphExecutionEngine
        ↓
DEV A
        ↓
DEV B
        ↓
Deterministic QA
        ↓
GitGovernanceService
        ↓
merge / tag / DONE
```

Propriété des composants — chacun a une responsabilité unique, jamais
dupliquée ailleurs :

- **`MVPManager`** orchestre les WorkItems (haut niveau) ; ne sélectionne
  jamais un worker, ne mute jamais Git, ne juge jamais la qualité du
  code lui-même.
- **`WorkerSelector`** possède la sélection de worker (capability >
  gouvernance > disponibilité > priorité) ; seule source de vérité pour
  "qui peut faire ce travail".
- **`QuotaManager`/`ProviderAdapter`** possèdent la vérité provider — un
  probe réel, jamais un calcul local du reset.
- **`RalphExecutionEngine`** possède l'exécution fine — lance le vrai
  `ralph`, jamais réimplémenté.
- **`GitGovernanceService`** possède branche/merge/tag — fast-forward
  uniquement, jamais de réécriture d'historique.
- **QA déterministe** (`InternalQAEngine`/`evaluate_qa_verdict`) possède
  le verdict PASS/FAIL/INCONCLUSIVE — jamais un LLM, jamais un
  auto-rapport de worker fait autorité.

## 5. WorkItem Flow

C'est le **seul** cycle de vie de WorkItem supporté. Pas de "défaut". Pas
de "Lean". Pas de workflow alternatif — `WorkflowMode` a été supprimé du
code (un seul mode restant n'a pas besoin d'un sélecteur).

Chemin nominal :

```
WorkItem
  ↓
WorkerSelector
  ↓
DEV A
  ↓
DEV B — corrective review, distinct worker, write-capable
  ↓
deterministic QA
  ↓
PASS
  ↓
governed fast-forward merge
  ↓
tag
  ↓
DONE
```

QA FAIL :

```
QA FAIL
  ↓
DEV FIX
  ↓
QA
  ↓
bounded attempts (max 3 au total)
  ↓
HUMAN_REVIEW_REQUIRED / BLOCKED
```

Détails :

- **Nominal** : exactement 2 exécutions LLM (DEV A, DEV B) ; la QA ne
  consomme et ne sélectionne aucun worker IA.
- **QA FAIL / DEV FIX** : un `DEV FIX` corrige, puis QA re-tourne. Jamais
  un retry aveugle d'une exécution crashée — toujours motivé par les
  constats QA réels.
- **Tentatives QA bornées** : 3 au total. Le 3ème échec passe le WorkItem
  en `BLOCKED` avec motif `HUMAN_REVIEW_REQUIRED` explicite (TODO ajouté
  à la roadmap du projet cible) — les autres WorkItems indépendants
  continuent.
- **WAITING** : quand `WorkerSelector` ne trouve aucun worker éligible
  mais qu'un provider candidat est diagnosticable `quota_exhausted` avec
  un `reset_at` connu. Reprise pull-based : re-probe réel au moment dû,
  jamais une reprise supposée.
- **RECOVERY_REQUIRED** : quand une exécution ne s'est jamais terminée de
  façon fiable (process orphelin après redémarrage). Immédiatement
  ré-orchestrable, toujours via une **nouvelle** exécution — jamais un
  replay de l'ancienne.
- **WorkItems dépendants** : un WorkItem `BLOCKED`/`WAITING`/`FAILED` ne
  bloque jamais ses WorkItems indépendants ; ses dépendants directs
  résolvent en `BLOCKED` via `refresh_readiness` (jamais silencieusement
  ignorés).

## 6. Invariants produit

- `DEV_B.worker_id != DEV_A.worker_id` — **REQUIS**, jamais désactivable.
- Provider distinct entre DEV A et DEV B — **PRÉFÉRÉ**, jamais requis.
- Un quota provider épuisé ne doit jamais produire `WAITING` tant qu'un
  autre worker éligible sur un provider disponible existe.
- `LLM IS NOT ORACLE` — un développeur IA seul n'est jamais l'autorité
  finale ; DEV B corrige, QA déterministe décide.
- QA exécutable et déterministe requise pour `COMPLETED` — jamais
  l'auto-rapport d'un worker.
- Preuve à SHA exacte — un verdict QA/gate n'est jamais réattribué à un
  SHA qu'il n'a pas réellement évalué.
- Tests protégés — une modification non autorisée d'un test protégé fait
  échouer la QA.
- Pas de retry aveugle — jamais un relancement automatique d'une
  exécution interrompue/crashée.
- État persistant pour la reprise — voir §8.
- `WorkerSelector` possède la sélection de worker.
- Ralph possède l'exécution fine.
- `GitGovernanceService` possède branche/merge/tag.
- **REUSE FIRST** — avant d'implémenter une nouvelle capacité, chercher
  ce qui existe déjà et peut être réutilisé/étendu.
- **KISS/YAGNI** — le plus petit changement qui satisfait un besoin
  prouvé ; jamais de construction pour un besoin hypothétique.

## 7. Providers et workers

Source de vérité : `config/workers.yaml` (lu directement pour ce
tableau, jamais supposé).

| Worker | Provider | Backend | Priorité | Capacités | Statut |
|---|---|---|---|---|---|
| `alice` | anthropic | claude_code | 100 | development, release_planning, roadmap_synthesis, complexity_estimation, qa_testing | ✅ VALIDATED |
| `bob` (Lydie) | anthropic | claude_code | 90 | (identiques à alice) | ✅ VALIDATED |
| `victor` | openai | codex | 100 | development, release_planning, roadmap_synthesis, complexity_estimation, qa_testing | ✅ VALIDATED |
| `oscar` (Yannick) | openai | codex | 90 | (identiques à victor) | ✅ VALIDATED |
| `milo` (Nathaniel) | mistral | vibe | 60 | development uniquement | ✅ VALIDATED |
| `juno` | mistral | vibe | 60 | development uniquement | ✅ VALIDATED |
| `gravity_primary` (Arthur) | gravity | gravity | 101 | development uniquement | ✅ VALIDATED |
| `gravity_secondary` (Nora) | gravity | gravity | 91 | development uniquement | ✅ VALIDATED |

8 workers déclarés et activés, **4 providers de premier niveau**
(anthropic, openai, mistral, gravity), tous résolus par la même table
explicite, `orchestrator.project_runtime._PROVIDER_ADAPTER_FACTORIES`,
jamais une hiérarchie métier codée en dur entre eux. Chaque provider a 2
workers indépendants (`DEV_B.worker_id != DEV_A.worker_id` reste toujours
satisfiable sans dépendre d'un autre provider).

Mistral/Vibe : capacité volontairement limitée à `development` (le spike
n'a produit de preuve d'exécution réelle que pour ce type de travail —
voir `docs/VIBE_SPIKE.md`) ; son signal de disponibilité est
`EXECUTION_PROBE_ONLY` (pas de fenêtre de quota observable), toujours
rapporté honnêtement comme `unknown`, jamais fabriqué en pourcentage.

**DeepSeek et Kimi — `RETIRÉ` (2026-09-28, P20)** : voir §13, P3.

Ollama n'est **pas** un provider actuel — voir §13, proposition P2.

## 8. État persistant et reprise

Deux couches distinctes, jamais confondues :

- **Déclaratif** (versionné, relu par un humain) : `config/workers.yaml`,
  la roadmap/le MVP d'un projet cible (YAML/Markdown + Git).
- **Runtime** (état d'exécution réel) : SQLite dédiées
  (`ProjectStateStore`, `WaitStore`, `ExecutionStore`,
  `GitWorkItemStore`, `QARunStore`, ...) — jamais reconstruit à la main.

Un WorkItem peut légitimement rester `WAITING` (quota) ou
`RECOVERY_REQUIRED` (exécution orpheline) et doit pouvoir reprendre plus
tard, par un tout autre process. Quand ce délai peut dépasser la durée de
vie d'un process (y compris un redémarrage machine), l'état d'exécution
doit vivre **hors `/tmp`** — `/tmp` est généralement vidé au redémarrage,
ce qui n'est *pas* la même garantie qu'une simple survie inter-process
(leçon tirée d'un incident réel sur le projet Morpion Web 3D, voir §11).
Chemin persistant typique :
`~/.local/state/ai-dev-orchestrator/projects/<project-id>/`, passé
explicitement aux constructeurs de store existants — aucun nouveau
framework de persistance. Une copie de travail purement jetable (checkout
temporaire pour reproduire un bug) peut rester sous `/tmp` sans problème.

## 9. Gouvernance Git et QA

- Branche de travail déterministe par WorkItem (`work/<work-item-id>`),
  jamais recréée si elle existe déjà (reprise idempotente).
- Branche de base protégée ; merge **fast-forward uniquement** — jamais
  de merge commit, jamais de rebase/reset/force automatisé.
- Preuve à SHA exacte — éligibilité au merge liée au SHA précis évalué,
  jamais approximée.
- QA en lecture seule pour la vérification finale — une violation de
  cette garantie fait échouer la QA, jamais un `PASS` silencieux.
- Preuve exécutable obligatoire — au moins une commande réelle
  configurée doit passer ; l'absence de preuve n'est jamais interprétée
  comme un succès.
- Protection de tests — un test protégé modifié sans autorisation versée
  fait échouer la QA.
- Aucun auto-rapport LLM n'est jamais traité comme un `PASS`.

## 10. Capacités optionnelles déjà construites

**CAPACITÉS OPTIONNELLES — DISPONIBLES, PAS PARTIE DU WORKITEM FLOW
AUTOMATIQUE.**

Le code suivant existe réellement dans le dépôt mais n'est jamais
enchaîné automatiquement par WorkItem Flow — chacune doit être invoquée
explicitement par un appelant :

- `PlanningCoordinator` (`src/orchestrator/planning.py`) — synthèse de
  proposition de roadmap à partir d'un `ActivityReport`.
- `ApprovalCoordinator` (`src/orchestrator/approval.py`) — fenêtre
  d'approbation optimiste sur une proposition de roadmap.
- `RoadmapApplicationService` (`src/orchestrator/roadmap_application.py`)
  — transformation déterministe (sans LLM) d'une proposition approuvée en
  `ROADMAP.md`/WorkItems réels ; utilise deux marqueurs dédiés
  (`<!-- orchestrator:roadmap-applications:begin -->`/`...:end -->`,
  absents de ce fichier tant qu'aucune application n'a encore eu lieu)
  pour préserver le reste du fichier verbatim.
- `ReleaseManager` (`src/orchestrator/release_manager.py`) — évalue le
  gate de release d'un MVP et construit son `ActivityReport` ; n'exige
  plus de `ReviewRecord` (retiré, voir §11 pour le contexte).
- Sélection adaptative / recommandations de complexité
  (`src/orchestrator/adaptive_execution.py`,
  `src/orchestrator/complexity_estimation.py`) — pré-vol de complexité et
  résolution de profil d'exécution ; aucun appelant actuel de WorkItem
  Flow ne les câble (WorkItem Flow utilise `WorkerSelector.select()`
  directement), mais le mécanisme reste disponible.
- `ProjectConfig.load(...)` (`src/orchestrator/project_config.py`, P12)
  — charge/valide un `aido.yaml` public en un objet typé complet (projet,
  référence registre de workers, `ExecutionConfig`, `GitConfig`, MVP,
  WorkItems, commandes QA). Consommé réellement par la CLI `aido` (P1,
  ci-dessous) ; `scripts/run_external_project_pilot.py` reste un exemple
  antérieur à P1, jamais migré vers `ProjectConfig`.
- `ExecutionPermissionMode` (`src/orchestrator/execution_policy.py`, P12)
  — `standard`/`unrestricted`, consommé par `RalphExecutionEngine` en
  paramètre de construction optionnel (`permission_mode=...`) ; omis, il
  n'ajoute aucun flag (comportement identique à avant P12) — un appelant
  doit le passer explicitement pour bénéficier de la politique
  project-controlled. `aido run` le passe toujours explicitement. Traduction vérifiée par backend dans
  `docs/PROJECT_CONFIG.md`.
- `ProjectRuntime` (`src/orchestrator/project_runtime.py`, P1) — couche
  de composition (jamais un second orchestrateur) : `ProjectConfig` ->
  stores réels (`ProjectStateStore`/`HandoffStore`/`ExecutionStore`/
  `WaitStore`/`ValidationStore`/`QARunStore`/`GitWorkItemStore`) ->
  `WorkerSelector`/`QuotaManager`/`RalphExecutionEngine`/
  `GitGovernanceService`/`InternalQAEngine` réels -> `MVPManager` réel.
  Centralise le câblage auparavant manuel de
  `scripts/run_external_project_pilot.py`. `ProjectRuntime.bootstrap()`
  est l'initialisation `aido.yaml` -> `ProjectStateStore` idempotente ;
  un conflit matériel entre l'état persisté et la config actuelle échoue
  fermé (`ConfigRuntimeConflictError`), jamais une mutation silencieuse
  d'un WorkItem/MVP/Project historique.
- **CLI publique `aido`** (`src/orchestrator/cli.py`, P1, point d'entrée
  `[project.scripts]`) — `aido init/validate/run/status`. C'est
  maintenant le point d'entrée produit normal (plus besoin de harnais
  Python). P1.1 étend `init` avec le bootstrap projet complet et guidé
  (`aido init <parent-path> <project-name>`), en conservant le mode
  config seul historique (voir §13 et `docs/PROJECT_CONFIG.md`) ; seul `aido run` provoque un appel provider réel ou une
  exécution Ralph — `init`/`validate`/`status` en sont exclus par
  construction. `aido run` est aussi l'opération de reprise : aucune
  commande `aido resume` séparée n'existe — relancer `aido run` contre le
  même `aido.yaml` rouvre le même `state_dir` persistant et reprend via
  les mécanismes `WAITING`/`RECOVERY_REQUIRED` existants.

## 11. Validations réelles

- **Roman Numerals** (kata externe) — `PASS`. Premier pilote réel
  post-clôture MVP 0.1 ; repli same-provider observé pour de vrai
  (openai en quota épuisé au moment du run). Détail :
  `docs/reports/roman-numerals-lean-pilot-2026-09-17.md`.
- **Mistral/Vibe** — ✅ `VALIDATED`. Utilisation réelle comme DEV B ;
  gouvernance de commit validée (consigne générique ajoutée aux
  instructions de tout worker, non spécifique à un backend). Détail :
  `docs/VIBE_SPIKE.md`.
- **Morpion Web 3D** (projet externe réel, multi-provider) — `DONE`.
  Régression navigateur découverte après un acceptance initial incomplet
  (couverture manquante sur le câblage DOM/timer, hors de portée des
  tests unitaires existants) ; correction finale gouvernée mergée. SHA
  cible final : `593c615e66e6a2cb585fb465ded0185da46a3319`. Récit complet
  public : `examples/morpion-web-3d/README.md`.
- **DeepSeek / Kimi** — `RETIRÉ` (P20, 2026-09-28) : aucune exécution réelle
  n'avait jamais eu lieu ; voir §13, P3.
- **AIDO Code** (deuxième projet de référence prévu, après Morpion Web
  3D) — `PREPARED`, `DEVELOPMENT NOT STARTED`. Dépôt Git local créé
  (`~/projects/aido-code`), roadmap/`MVP_SPEC.yaml`/WorkItems M1
  préparés par P13 ; aucun `aido run` lancé, aucun code fonctionnel du
  futur CLI écrit, DEV A/DEV B count = 0. Ne devient `DONE`/`VALIDATED`
  que lorsque AI Dev Orchestrator aura réellement exécuté ses propres
  WorkItems dessus (DEV A, DEV B, QA déterministe, merge gouverné),
  jamais avant.

La chronologie forensique détaillée (exécutions individuelles, durées,
diagnostics pas-à-pas) est récupérable via l'historique Git et les
rapports techniques sous `docs/reports/`, pas ici.

## 12. Historique synthétique

Jalons majeurs seulement — pas de journal Slice par Slice :

- Spécification / étude d'écosystème (gate build-vs-reuse).
- Décision de réutiliser Ralph plutôt que de réimplémenter l'exécution.
- Orchestration MVP (Project/MVP/WorkItem durables).
- Reprise durable (WAITING/RECOVERY_REQUIRED).
- Gouvernance Git (`GitGovernanceService`).
- Gouvernance QA (QA déterministe, non-LLM).
- Introduction de WorkItem Flow (alors nommé `LEAN_FEATURE_FLOW`).
- Validation multi-provider réelle (Mistral/Vibe).
- `GOVERNED_FULL` retiré avant la première release publique (2026-09-18).
- v0.1.1 publiée en open source (2026-09-18).
- Cycle productisation/onboarding approuvé (P1 + P12) ; P12 — format de
  configuration public `aido.yaml` + mode de permission d'exécution des
  workers project-controlled — implémenté (2026-09-19).
- P1 — CLI publique `aido` (`init`/`validate`/`run`/`status`) —
  implémenté, cycle productisation/onboarding `DONE` (2026-09-19).
- P1.1 — bootstrap projet guidé dans le `aido init` existant, Git local et
  reprise manuelle en cas d’échec, compatibilité préservée (2026-09-24).
- P4 (étude Mammouth) menée puis close `RETIRÉ` : agrégateur jugé d'intérêt
  économique/architectural insuffisant face à l'intégration directe de
  providers (décision utilisateur, 2026-09-19).
- P3 — DeepSeek + Kimi intégrés (2026-09-19) puis `RETIRÉ` (P20,
  2026-09-28), faute de preuve d'exécution réelle.
- P13 — Découplage moteur / externalisation AIDO Code (priorité 1) :
  façade publique `orchestrator.engine.OrchestratorEngine` exposée,
  projet `aido-code` créé (`~/projects/aido-code`, roadmap/`MVP_SPEC.yaml`/
  WorkItems M1 préparés, aucun code fonctionnel écrit), `DONE`
  (2026-09-19).

Chronologie détaillée : historique Git (`git log`) et docs techniques
(`docs/status.md`, `docs/QA_GOVERNANCE.md`, `docs/GIT_GOVERNANCE.md`,
`docs/ADAPTIVE_EXECUTION.md`, `docs/VIBE_SPIKE.md`,
`docs/PROJECT_CONFIG.md`).

## 13. Propositions à voter

La plupart des lignes ci-dessous restent `À VOTER` — aucun ordre n'implique
une priorité pour elles, et aucun WorkItem n'est créé pour une proposition
`À VOTER` tant qu'elle n'a pas été explicitement votée par l'utilisateur.
P1, P12 et P3 font exception : ce sont des décisions utilisateur déjà
explicitement approuvées et, pour P3, implémentées (2026-09-18/19),
documentées ci-dessous. P4 (étude Mammouth) a été menée puis close
`RETIRÉ` par l'utilisateur le 2026-09-19 : l'approche agrégateur a été
évaluée et jugée d'un intérêt économique/architectural insuffisant face à
l'intégration directe de providers supplémentaires ; aucun code Mammouth,
aucune dépendance gateway/agrégateur multi-modèles.

### Cycle produit approuvé — `DONE`

**Productisation / onboarding — P1 + P12 : `DONE`.**

Résultat visé, atteint : un nouvel utilisateur peut configurer et lancer
un projet gouverné sans écrire de harnais Python sur mesure
(`aido init` → `aido validate` → `aido run` → `aido status`).

**Exigence transverse d'acceptation de ce cycle** : le mode de permission
d'exécution des workers devait être explicite et contrôlé par le projet,
jamais hérité silencieusement de la configuration de la machine du
mainteneur — **implémenté**, voir ci-dessous.

- **P12 — Format de configuration de projet public : `DONE`.**
  `orchestrator.project_config.ProjectConfig` (`aido.yaml` schema v1)
  représente : identité du projet ; dépôt/workspace ; référence (jamais
  copie) au pool de workers existant (`config/workers.yaml` reste seul
  source de vérité) ; mode de permission d'exécution des workers ; base
  branch Git ; MVP/WorkItems ; commandes QA déterministes
  (`ValidationCommand` réutilisé, jamais dupliqué) ; aucun secret/
  identifiant. Voir §3, §10 et `docs/PROJECT_CONFIG.md` pour le détail
  complet. Exemple public tracké : `examples/aido.yaml`.
- **P1 — CLI publique `aido` : `DONE`.** KISS/YAGNI : exactement 4
  commandes (`init`/`validate`/`run`/`status`), aucune commande `resume`
  séparée — `aido run` reprend l'état persisté existant. Consomme
  `ProjectConfig`/`ExecutionPermissionMode` (P12) via une nouvelle couche
  de composition, `orchestrator.project_runtime.ProjectRuntime` (§10),
  jamais un second orchestrateur. Point d'entrée réel
  (`[project.scripts]`, `aido = "orchestrator.cli:main"`). Validé de bout
  en bout, entièrement hors ligne (adaptateurs providers et subprocess
  Ralph faux, jamais de vrai Claude/Codex/Vibe/Ralph), y compris un
  scénario complet DEV A → DEV B → QA déterministe → merge gouverné →
  `COMPLETED`, une reprise multi-process après `WAITING`, et la preuve
  qu'aucun appel provider ne se produit pour `init`/`validate`/`status`.

### P1.1 — Guided project bootstrap / onboarding — `DONE` (2026-09-24)

Extension explicitement approuvée de P1, dans la CLI `aido` existante
(ai-dev-orchestrator, aucun changement dans AIDO Code).
`aido init <parent-path> <project-name>` crée README, ROADMAP, `aido.yaml`
et `.gitignore`, puis un dépôt Git sur `main` avec le commit
`Initialize AIDO project`. Répertoire absent ou vide seulement ; noms
contenant un chemin et cibles symlink refusés. Zéro ou un argument conserve
la génération historique du seul fichier de configuration.

La génération de configuration et le registry utilisateur existants sont
réutilisés. README porte le contexte humain, ROADMAP la trajectoire produit,
`aido.yaml` le MVP exécutable ; aucune synchronisation automatique.
Aucun provider, probe, worker, Ralph, état métier, remote ou push pendant
init. Git absent/en échec : scaffold conservé, code non nul, étape en échec
et commandes de reprise affichées, puis onboarding édition/validation/
commit de définition/run explicite. Aucun lancement de M2.

**Validation** : suite offline complète verte ; tests CLI avec vrais dépôts
Git temporaires, erreurs Git simulées, sécurité des chemins, compatibilité
et garde contre la construction du runtime/résolution provider. Bootstrap
manuel via le binaire `aido` : branche `main`, commit initial exact, working
tree propre, `aido validate` OK. Compte courant dans `docs/status.md`.

### P3 — DeepSeek + Kimi — `RETIRÉ`

Une première intégration avait été implémentée mais n'a jamais obtenu de
preuve d'exécution réelle. Décision produit du 2026-09-28 : les providers
non validés ne font pas partie du produit actif. Les adapters et workers
DeepSeek/Kimi ont donc été retirés. Toute réintégration future nécessitera
un nouveau spike réel et un GO humain explicite.

**Historique (2026-09-19, conservé tel quel ci-dessous)** : implémentation `DONE`, validation réelle `PENDING`.

**Résultat** : DeepSeek et Kimi sont désormais des providers de premier
niveau au même sens architectural que Claude/Codex/Mistral, avec le même
contrat `ProviderAdapter`/`ProviderState` et la même table de résolution
(`orchestrator.project_runtime._PROVIDER_ADAPTER_FACTORIES`), sans
hiérarchie métier codée en dur entre les cinq. C'est une implémentation
logicielle complète, pas encore une validation : voir §7 pour le détail
(workers `dana`/`kai`, `enabled: false` par défaut, raisons).

**Décision d'architecture (REUSE FIRST)** : ni DeepSeek ni Kimi n'ont reçu
un second adaptateur HTTP indépendant. Les deux exposent officiellement un
point de terminaison compatible Anthropic
(`https://api.deepseek.com/anthropic`, `https://api.kimi.ai/coding/`)
documenté comme étant le binaire `claude` existant, simplement redirigé via
`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`. `ClaudeCodeAdapter` a donc été
étendu avec deux paramètres optionnels rétrocompatibles
(`provider_name`, `extra_env`, défaut = comportement Anthropic réel
inchangé) au lieu d'être dupliqué. `orchestrator.providers.deepseek_adapter`/
`kimi_adapter` sont de fines fabriques qui lisent
`DEEPSEEK_API_KEY`/`KIMI_API_KEY` (+ `..._BASE_URL`/`..._MODEL` optionnels)
depuis l'environnement process et construisent un `ClaudeCodeAdapter`
configuré, sans jamais qu'une clé soit committée ni qu'une valeur par
défaut lui soit donnée.

**Écart assumé par rapport au prompt d'origine** : le prompt demandait des
variables `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL` (et
l'équivalent Kimi) comme si ces providers étaient atteints par un client
HTTP direct. L'inspection de l'architecture existante (tous les adaptateurs
actuels, Claude Code, Codex, Vibe, pilotent un CLI déjà authentifié par
subprocess, jamais un client HTTP à clé) et la documentation officielle de
DeepSeek/Kimi elles-mêmes (leur propre intégration recommandée est
précisément « rediriger Claude Code ») ont montré que ces mêmes noms de
variables s'appliquent naturellement au mécanisme de redirection du CLI
existant, sans écart de nommage ni architecture parallèle.

**Erreur contrôlée** : une clé manquante ne lève jamais une exception brute.
`DeepSeekConfigError`/`KimiConfigError` (sous-classes de la nouvelle
`orchestrator.providers.adapter.ProviderConfigError`) sont interceptées par
`ProjectRuntime._resolve_provider_adapters` et re-levées comme
`ProviderConfigurationError` (`ProjectRuntimeError`), avec le même
traitement propre côté CLI que `UnsupportedProviderError`. Ceci ne se
produit que si un worker **activé** requiert effectivement ce provider,
jamais pour un provider simplement configuré mais inutilisé.

**Tests** : `tests/providers/test_deepseek_adapter.py`,
`tests/providers/test_kimi_adapter.py`, extensions de
`tests/providers/test_claude_code_adapter.py` (réutilisation
`provider_name`/`extra_env`) et de `tests/test_project_runtime.py`
(composition réelle, clé absente/présente, non-régression des autres
providers). Aucun appel réseau/CLI réel ; `env` est toujours une mapping
explicite dans les tests, jamais l'environnement process réel de la
machine de test. Suite complète : voir §2/`docs/status.md` pour le compte
à jour.

**Non fait, explicitement** : `dana`/`kai` ne sont pas activés par défaut
(pas de clé, pas de preuve d'exécution réelle) ; aucun rôle métier rigide
(planner/developer/qa/reviewer/...) n'a été codé, car ce dépôt exprime déjà
les rôles comme des capacités (`development`, `qa_testing`,
`release_planning`, `roadmap_synthesis`, `complexity_estimation`) sur
`Worker`, pas comme une énumération de rôles séparée ; DeepSeek/Kimi n'ont
reçu que `development`, à l'identique de l'onboarding Mistral/Vibe, faute
de preuve pour les autres capacités.

**Passage à `VALIDATED`, critères** : ni DeepSeek ni Kimi ne peut passer
`VALIDATED` sans un vrai pilote démontrant, dans cet ordre : (1) accès
provider réel réussi ; (2) `claude` effectivement redirigé vers le bon
endpoint ; (3) `ProviderState.provider` correctement normalisé ; (4) un
vrai worker exécuté ; (5) une modification réelle d'un dépôt cible ; (6)
un commit réel ; (7) un DEV A ou DEV B effectif ; (8) une QA déterministe
réussie ; (9) le `permission_mode` propagé correctement ; (10) aucun
secret dans logs/rapports/SQLite ; (11) un repli provider correct ; (12)
un état persistant correct : le même standard que Mistral/Vibe
(`docs/VIBE_SPIKE.md`). Aucun de ces points n'est couvert aujourd'hui ;
aucune preuve n'a été fabriquée pour en simuler la couverture.

### P13 — Découplage moteur / externalisation AIDO Code — `DONE`, priorité 1 (2026-09-19)

**Décision produit** : `ai-dev-orchestrator` devient un moteur headless
réutilisable. L'interface terminal interactive devient un projet
indépendant, `aido-code` (`~/projects/aido-code`, dépôt Git local séparé,
aucun remote créé par cette tâche). Le découplage lui-même est réalisé
directement par cette session ; le développement fonctionnel d'AIDO Code
(à partir de M1 de sa propre roadmap) sera ensuite confié à
AI Dev Orchestrator lui-même, une fois amorcé par un futur pilote réel,
exactement comme Morpion Web 3D (§11), le premier exemple externe réel.

**Propriété inchangée** : `ai-dev-orchestrator` reste seul propriétaire de
`ProjectConfig`, Project/MVP/WorkItem, l'état persistant, `WorkerRegistry`,
`WorkerSelector`, `QuotaManager`, les `ProviderAdapter`s,
`RalphExecutionEngine`, `WAITING`/`RECOVERY_REQUIRED`, DEV A/DEV B, la QA
déterministe, `GitGovernanceService`, et l'audit d'exécution. AIDO Code
n'implémente et ne duplique jamais aucun de ces mécanismes ; il consomme
la façade moteur ci-dessous.

**Façade moteur publique (REUSE FIRST)** : `orchestrator.engine.
OrchestratorEngine` (`src/orchestrator/engine.py`, nouveau). Ne construit
rien de neuf : chaque méthode délègue à une primitive déjà réelle et déjà
testée.

| Méthode | Délègue à | Effet de bord |
|---|---|---|
| `.open(config_path)` | `ProjectConfig.load()` | aucun (lecture seule) |
| `.validate()` | `ProjectConfig` + `WorkerRegistry` | aucun |
| `.workers()` | `WorkerRegistry.all_workers()` | aucun, jamais de probe provider |
| `.probe_workers()` | `project_runtime.resolve_provider_adapters` + `QuotaManager` | un probe réel, explicite, éphémère (aucun SQLite) |
| `.status()` | `ProjectStatusReader` | aucun (identique à `aido status`, P1) |
| `.run(max_cycles=...)` | `ProjectRuntime.open()`/`.bootstrap()` + `MVPManager.run_next_work_item()` | le seul appel écrivant réellement (SQLite, Git, provider) |
| `.close()` | rien à fermer | no-op documenté (aucune connexion persistante entre appels) |

Chaque réponse est un type `frozen`/`slots` sérialisable
(`ProjectSnapshot`, `WorkerSnapshot`, `ProviderSnapshot`,
`ExecutionSnapshot`, `WaitSnapshot`, `WorkItemSnapshot`,
`MVPStatusSnapshot`, `ProjectStatusSnapshot`, `RunResult`, `EngineEvent`),
jamais un objet Store, une connexion SQLite, ou une dataclass interne
d'un autre module. La fonction privée `project_runtime._resolve_provider_adapters`
a été rendue publique (`resolve_provider_adapters`) pour que
`.probe_workers()` la réutilise sans dupliquer la table de résolution de
providers (§7).

**Invariants vérifiés, inchangés** (relecture explicite de
`mvp_manager.py`/`worker_selector.py` avant toute modification) : aucun
client HTTP direct introduit par cette tâche ; aucun second moteur
d'exécution ; `ClaudeCodeAdapter` inchangé pour Anthropic ; aucun
branchement spécifique à un provider dans `MVPManager` ou
`WorkerSelector` (vérifié par recherche exhaustive, zéro occurrence) ;
`.probe_workers()` ne mute jamais `os.environ` globalement ; aucun credential provider n'est lu par le runtime (DeepSeek/Kimi, seuls providers à clé API, retirés en P20) ; aucun secret n'entre dans `aido.yaml`/
`config/workers.yaml`/`ExecutionRecord`/SQLite/logs/rapports ; la
résolution des factories providers reste centralisée dans
`project_runtime.py` ; `WorkerSelector` reste seul propriétaire du choix
du worker ; `QuotaManager` reste provider-level, jamais worker-level ;
Ralph reste propriétaire de l'exécution fine.

**Événements structurés (§10 du prompt de découplage)** : contrat formalisé
(`EngineEvent` : `kind`/`timestamp`/`project_id`/`mvp_id`/`work_item_id`/
`payload`), mais seulement au grain que `MVPManager` expose réellement
aujourd'hui : un événement `work_item.<status>` par appel à
`run_next_work_item()`. Les sous-étapes fines (DEV A running/completed,
DEV B running/completed, QA running, merge completed) ne sont émises nulle
part dans ce dépôt : `MVPManager._execute_work_item` les exécute de façon
synchrone, sans bus d'événements. Exposer cette granularité est un
incrément moteur réel et distinct, documenté ici comme travail futur,
jamais simulé.

**Tests** : `tests/test_engine.py` (22 tests, entièrement hors ligne,
mêmes fixtures que `tests/test_cli.py` : faux adaptateurs providers, faux
subprocess Ralph scripté) ; suite complète 1157/1157 PASS (1135 avant).

**Non fait, explicitement, par cette tâche** : aucune migration du CLI
`aido` existant vers cette façade (P1 reste `DONE`, intact, non modifié
fonctionnellement) ; aucun cutover de la commande `aido` ; aucun code
fonctionnel du futur CLI `aido-code` écrit par cette session ; aucun
`aido run` lancé dans `~/projects/aido-code` ; DEV A count = 0, DEV B
count = 0 pour AIDO Code. Voir la sous-section suivante pour la
préparation (roadmap/MVP_SPEC/WorkItems) du projet `aido-code` lui-même.

### P13.1 — Première exécution réelle du WorkItem Flow sur AIDO Code (M1) et défaut moteur QA découvert/corrigé (2026-09-21)

**Fait** : le GO humain a été donné pour laisser AI Dev Orchestrator
construire réellement AIDO Code M1 (WI-01 à WI-07, `~/projects/aido-code`)
via son propre `aido run` — DEV A (`alice`/anthropic), DEV B corrective
(`victor`/openai), QA déterministe interne, merge/tag gouvernés. Les 7
WorkItems ont atteint `completed` en 8 cycles ; 32 tests finaux PASS ; un
recovery réel (`RECOVERY_REQUIRED`) a eu lieu sur WI-01 après une
interruption externe du process ; aucune correction manuelle du code
produit par un opérateur humain/assistant.

**Défaut moteur découvert par ce run réel** (analyse forensique read-only,
preuve exacte) : WI-02 a produit un `pytest -q` `FAIL`
(`ModuleNotFoundError: No module named 'orchestrator'`) puis, sur le
**même** head SHA (`06c79d9…`) et la **même** commande, un `PASS`,
9 tests passés. Cause établie : la commande QA configurée (`argv:
["pytest", "-q"]`, résolue via le `PATH` ambiant hérité par
`asyncio.create_subprocess_exec`, jamais pinnée à un interpréteur
déclaré) a résolu un `pytest` différemment selon que le paquet
`orchestrator` était, ou non, installé dans l'environnement Python
utilisateur ambiant au moment précis de chaque tentative — un processus
externe (le worker DEV FIX lui-même, suivant `CONTRIBUTING.md`
d'AIDO Code) a installé cette dépendance manquante *entre* les deux
tentatives QA, sans aucun changement Git (SHA avant = SHA après). Ce
n'était donc ni un défaut produit d'AIDO Code, ni un « blind retry », mais
une lacune réelle de la preuve QA moteur : `ValidationResult` liait un
verdict au SHA et à la commande, jamais à l'environnement d'exécution
réellement observé — le run historique original **n'était donc pas
parfaitement déterministe/hermétique**, contrairement à ce qu'un simple
« QA PASS » suggérait avant cette correction.

**Correction moteur appliquée** (branche `fix/qa-environment-determinism`,
PR dédiée) : voir `docs/QA_STRATEGY.md`, §8.3 (« DETERMINISTIC QA
EVIDENCE ») pour le détail complet. En résumé :
`ValidationEnvironmentEvidence` (`src/orchestrator/validation.py`) capture
désormais l'exécutable réellement résolu, l'interpréteur Python
sous-jacent (générique, jamais une règle spécifique à `pytest`), sa
version, son `sys.prefix`, et une empreinte du jeu de paquets installés,
pour chaque commande de validation exécutée ; `qa.environment_drift_reason`
+ `evaluate_qa_verdict(..., environment_drift_detail=...)` refusent de
traiter comme un `PASS` déterministe une tentative qui suivrait un
`FAIL`/`INCONCLUSIVE` antérieur sur le même SHA sous un environnement de
validation différent — verdict `INCONCLUSIVE`
(`VALIDATION_ENVIRONMENT_CHANGED`) à la place, jamais un `PASS` silencieux.
Migration SQLite additive/idempotente (`environment_json`, anciens
enregistrements décodés `environment=None`, jamais un fingerprint
fabriqué après coup). Aucune promesse d'hermiticité totale — voir la
définition exacte de « DETERMINISTIC QA EVIDENCE » en §8.3 de
`docs/QA_STRATEGY.md`.

**Attribution Git des workers** (même correction) : les commits produits
par un worker sont désormais attribués par `worker.display_name` (jamais
un nom de provider/vendor — Claude/Anthropic/Codex/OpenAI/Mistral exclus
par construction), via `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/
`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` injectés dans l'environnement
du seul subprocess du worker concerné (`RalphExecutionEngine`,
`_worker_git_identity_env`) — jamais `git config --global`/`--system`,
aucune logique Git dans `MVPManager`/`WorkerSelector`. Email technique
stable et non trompeuse : `<worker_id>@workers.ai-dev-orchestrator.local`.
Les commits WI-01 à WI-07 déjà produits par le run réel ci-dessus
**n'ont pas été réécrits** (preuve d'exécution immuable) ; cette
attribution s'applique aux commits produits à partir de cette correction.

**Tests** : régression exacte du scénario WI-02 (même SHA, même commande,
environnement différent -> `INCONCLUSIVE`, jamais `PASS`) dans
`tests/test_mvp_manager_workitem_flow.py`
(`TestEnvironmentDriftNeverMasksAsPass`), plus tests unitaires
`tests/test_qa.py`/`tests/test_validation.py`/
`tests/test_ralph_execution_engine.py` (empreinte d'environnement,
migration idempotente, attribution Git par worker, aucune fuite de
secret, aucune mutation de config Git globale).

**AIDO Code, statut après cette correction** : `M1 DONE` fonctionnellement
(WI-01..WI-07 `completed`, 32 tests PASS, smoke test read-only PASS) ;
promu deuxième projet de référence réel avec la réserve honnête
ci-dessus — le run initial a révélé un défaut moteur réel, corrigé avant
toute promotion sans réserve. M2 (sessions/resume) reste `PLANNED`, non
démarré ; aucun WorkItem M2 créé dans l'état runtime.

### P13.2 — Attribution Git worker renforcée après un défaut réel découvert sur AIDO Code (M1.1) — `DONE` (2026-09-22)

**Défaut découvert** (run réel gouverné M1.1 d'AIDO Code, WI-M1.1-01,
`docs/M1_1_REFERENCE_RUN.md` de ce projet) : le commit fonctionnel réel
de `alice` (`0e9eb9676ba806a66e79689baaf16168699cc471`, correction
`pyproject.toml`) est resté attribué à l'identité ambiante
`yannickameur <yannick.ameur@gmail.com>` au lieu d'Alice, alors que le
commit de sécurité Ralph du même cycle (`7ae6037`, "auto-commit before
merge") a bien reçu l'identité injectée par P13.1
(`_worker_git_identity_env`, variables `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
dans l'environnement du seul subprocess `ralph`).

**Cause établie** (audit forensique read-only, `src/orchestrator/
ralph_execution_engine.py`, `tests/test_ralph_execution_engine.py`) :
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` ne sont injectées que dans l'environnement
du subprocess `ralph` lui-même — un héritage d'environnement à travers un
processus enfant arbitrairement imbriqué (le backend `claude_code`, puis
son propre outil shell interne) n'est PAS une garantie : un `git commit`
lancé par ce shell imbriqué sans hériter (ou avec un environnement
explicitement appauvri par ce niveau) retombe sur `user.name`/
`user.email` résolus par `git` (config locale/globale/système), jamais
sur les variables `GIT_AUTHOR_*` du processus grand-parent si elles ne
sont concrètement pas présentes dans l'environnement du processus qui
exécute réellement `git commit`. Reproduit et prouvé offline, sans aucun
provider réel : `tests/test_ralph_execution_engine.py::
TestWorkerCommitIdentityEndToEnd::test_nested_commit_with_stripped_env_still_gets_worker_identity`
exécute un vrai `ralph` factice qui lance un `git commit` imbriqué dans
un environnement volontairement réduit au seul `PATH` — la même forme de
défaut que celle réellement observée.

**Correction appliquée** (`src/orchestrator/ralph_execution_engine.py`) :
une seconde défense indépendante, `scoped_worker_git_identity` — un
contexte qui fixe temporairement `user.name`/`user.email` en config Git
**locale au workspace** (`git config --local`, jamais `--global`/
`--system`) à l'identité du worker pendant la durée d'une exécution
réelle, puis restaure exactement l'état précédent (valeur locale
antérieure, ou son absence). `git` lit cette config quel que soit le
niveau d'imbrication du processus qui lance `git commit`, y compris sans
aucun héritage d'environnement — l'injection `GIT_AUTHOR_*`/
`GIT_COMMITTER_*` (P13.1) est conservée telle quelle comme première
défense. Sûr uniquement parce qu'une seule exécution worker tourne
jamais contre un workspace gouverné donné à la fois (`MVPManager`,
« No parallelism », DEV A et DEV B jamais concurrents) — vérifié avant
d'introduire cette mutation locale, pour ne jamais l'exposer à une
future concurrence sans une isolation par worktree/processus.

**Audit post-exécution, fail-closed** : `_audit_worker_commit_identity`
énumère, après chaque exécution réelle, tous les commits introduits dans
`git_sha_before..git_sha_after` et compare author/committer à l'identité
attendue du worker — sur les deux champs. Le premier commit non conforme
lève `WorkerCommitIdentityMismatchError` (execution_id, worker_id, SHA,
identité attendue/observée pour author et committer), l'exécution est
finalisée `FAILED`, et l'exception remonte non interceptée jusqu'à
`aido run`/`OrchestratorEngine.run()` — aucun chemin ne l'avale en échec
métier ordinaire, donc aucun merge ne peut jamais voir un commit mal
attribué. Aucune réécriture automatique du commit fautif. Les deux
mécanismes (config locale + audit) ne s'appliquent qu'à une exécution
`ralph` réelle (`subprocess_runner` par défaut) — jamais aux tests avec
un faux `subprocess_runner` (toute la suite existante), qui ne
représentent jamais un vrai commit produit par un worker ; cette
frontière est documentée dans la docstring d'`execute()`.

**Historique M1.1 non réécrit** : `0e9eb96` reste tel quel dans
`~/projects/aido-code` — preuve d'exécution immuable du défaut, jamais
corrigée après coup.

**Tests** : 22 nouveaux tests offline, aucun provider réel
(`tests/test_ralph_execution_engine.py`) —
`TestScopedWorkerGitIdentity` (mise en place/restauration de la config
locale, y compris après exception, absence de fuite entre deux workers
successifs, jamais de mutation globale, no-op sur un workspace non-Git),
`TestWorkerCommitIdentityAudit` (les 11 cas requis : aucun commit,
commit(s) correct(s), identité incorrecte sur le 2e commit, author
correct/committer incorrect et inversement, commit de sécurité Ralph
audité comme les autres, Alice jamais dans la plage de Victor, preuve
complète dans l'exception, absence de commit antérieur), et
`TestWorkerCommitIdentityEndToEnd` (le vrai chemin `_default_
subprocess_runner`/`RalphExecutionEngine.execute()` avec un faux binaire
`ralph` réel : commit imbriqué à environnement appauvri correctement
attribué, identité imbriquée adverse détectée et fail-closed, config
locale restaurée après un run réel, chemin faux-runner inchangé).

### P13.3 — Status opérationnel complet, quotas riches, registry standalone, GPT-6 — `DONE` (2026-09-23)

**Contexte** : le clean-install kata externe (String Calculator, voir
AIDO Code) a prouvé `PACKAGE_INSTALL_PASS` mais révélé
`STANDALONE_RUNTIME_PASS = FAIL` (aucun registry de workers livré). En
parallèle, `aido status` restait sur le rendu P1 (sans vue workers,
sans quota), alors qu'AIDO Code (M1.1) l'avait déjà dépassé côté
frontend. Cette tâche corrige les deux, plus les modèles Codex GPT-6.

**`aido status` enrichi** : affiche désormais TOUJOURS une section
`Workers:` (tous les workers configurés, prénom/`display_name`,
provider, backend, modèle du profil par défaut), en plus du rendu
projet/MVP/WorkItems existant — inchangé, zéro appel provider. Le
`worker=` de `last execution` résout maintenant le prénom depuis le
registry courant (`Victor [victor]`), honnêtement `unknown display
name` si ce `worker_id` n'existe plus. `aido status --probe` (nouveau
flag, opt-in) ajoute un vrai probe explicite et une section `Provider
quotas:` — un bloc par PROVIDER (jamais dupliqué par worker : Alice/Bob
partagent `anthropic`, Victor/Oscar partagent `openai`), avec
`utilization`/`remaining`/`reset_at` par fenêtre de quota et les reset
credits observés, jamais consommés. `remaining` n'est calculé que si
`utilization` est connu (`1.0 - utilization`) ; une valeur inconnue
reste `unknown`, jamais fabriquée à 0%/100%. Un worker désactivé affiche
`probe=disabled`, jamais un `unknown` trompeur laissant croire à un
probe tenté.

**`OrchestratorEngine.ProviderSnapshot` enrichi** (`src/orchestrator/
engine.py`) : `quota_windows: tuple[QuotaWindowSnapshot, ...]` et
`reset_credits: tuple[ResetCreditSnapshot, ...]`, nouveaux, avec
défauts vides pour la compatibilité — l'ancien champ `reset_at` reste
inchangé. Ces DTO projettent fidèlement `providers.contracts.
QuotaWindow`/`ResetCredit` (déjà remontés par `CodexAdapter`/
`ClaudeCodeAdapter` mais jusqu'ici perdus par `probe_workers()`) — aucune
seconde infrastructure quota, `REUSE FIRST`. `ExecutionSnapshot` gagne
`worker_display_name: str | None`, résolu depuis le `WorkerRegistry`
courant par `.status()`, jamais fabriqué si le worker historique a
disparu du registry.

**Modèles Codex GPT-6** : la CLI Codex réelle (`codex-cli 0.155.1`,
`codex doctor` confirme `gpt-6-sol` déjà configuré comme défaut ambiant
de la machine) expose désormais `gpt-6-luna`/`gpt-6-sol`/`gpt-6-astra`.
Les trois ont été validés par un vrai appel minimal
(`codex exec -m <model> --sandbox read-only "Reply with exactly: OK"`,
~2000 tokens chacun, aucun reset credit consommé) avant toute
modification du registry. `victor`/`oscar` (`config/workers.yaml`) :
`economy=gpt-6-luna`, `standard=gpt-6-sol`, `deep=gpt-6-astra` — Astra
n'est jamais le profil par défaut du travail standard.

**Registry de workers standalone** (le vrai gap comblé) :
`src/orchestrator/resources/default_workers.yaml` (nouveau
sous-package, livré dans le wheel via `[tool.setuptools.package-data]`)
devient la source canonique — copie exacte de `config/workers.yaml`,
garantie identique par `tests/test_default_worker_registry.py`
(`config/workers.yaml` reste un chemin de compatibilité développement
uniquement, jamais une seconde source qui pourrait diverger
silencieusement). `aido init` sans `--workers-registry` : préfère
toujours un `./config/workers.yaml` du répertoire courant s'il existe
(convention dev inchangée), sinon matérialise
`$XDG_CONFIG_HOME/ai-dev-orchestrator/workers.yaml` (ou
`~/.config/ai-dev-orchestrator/workers.yaml`) depuis le template
packagé — créé si absent, **jamais écrasé** s'il existe déjà. Aucun
checkout source requis. Version moteur : **0.1.3**.

**`MVP status=running` avec 100% des WorkItems `completed` — PAS un bug,
confirmé par lecture directe du contrat existant** : `MVPStatus` n'a
pas de valeur `COMPLETED` ; la seule progression au-delà de `RUNNING`
est `RUNNING → VALIDATING → RELEASED`, exclusivement pilotée par
`ReleaseManager` (Slice 10, `src/orchestrator/release_manager.py`) —
"Kept deliberately separate from MVPManager... Merging the two would
turn MVPManager into exactly the kind of god object this codebase
avoids" (docstring du module lui-même). `MVPManager`/`aido run`
n'appellent jamais `mark_validating`/`mark_released` : ce cycle
release/planning reste **`À VOTER`** (P11, non productisé). `running`
avec tous les WorkItems `completed` décrit donc honnêtement l'état réel
: le travail de développement est fini, mais aucune release gate n'a
encore été exécutée. Aucun test de régression ajouté (rien à corriger),
aucun historique SQLite modifié.

**Tests** : 20 nouveaux tests offline, aucun provider réel —
`tests/test_cli.py` (`TestStatusWorkers`, `TestStatusProbe` : rendu
workers/quota, provider partagé affiché une seule fois, `unknown` ne
devient jamais 100%, `probe=disabled` pour un worker désactivé,
`TestStandaloneWorkerRegistry` : création/non-écrasement du registry
utilisateur, priorité conservée au `config/workers.yaml` du cwd) et
`tests/test_default_worker_registry.py` (identité byte-à-byte avec
`config/workers.yaml`, parsing réel, mapping
GPT-6, aucune valeur secrète, présence réelle dans le wheel construit).

### P13.4 — Remédiation post-audit (Mistral) — `DONE` (2026-09-23)

**Contexte** : un audit technique externe (méthode multi-agents, lecture
directe du code, jamais de la doc seule) a été mené sur le SHA
`75b3ef5e5d77f2fb0e516e2b4722dfcc1b8c2fbd` — voir
[`docs/reports/mistral-engine-audit-2026-09-23.md`](docs/reports/mistral-engine-audit-2026-09-23.md)
pour le rapport complet (11 findings, aucun CRITICAL, verdict `NO
CONFIRMED BLOCKER FOUND` pour M2). Principe appliqué : *external audits
are inputs, not authority* — chaque finding a été confirmé par lecture
directe avant correction, jamais accepté tel quel (voir §14, P16).

**Findings corrigés** :

| ID | Sévérité | Décision | Correctif |
|---|---|---|---|
| AUD-1 | HIGH | `FIXED` | `qa_protected_paths` ajouté au schéma `aido.yaml` (`ProjectConfig`), propagé par `ProjectRuntime.bootstrap()` jusqu'au vrai `MVPManager` — la protection de tests existait déjà (`qa_protection.py`) mais n'était câblée nulle part dans le chemin réel ; voir `docs/PROJECT_CONFIG.md`, "Protected test paths". Preuve : test bout-en-bout réel (`tests/test_cli.py::TestProtectedTestPaths`), jamais une construction manuelle de `MVPManager` contournant le problème. |
| AUD-2 | MEDIUM | déferré, documenté | Détection volontairement non construite dans cette tâche (voir "Non fait" ci-dessous) — reste un vrai gap connu, pas fermé silencieusement. |
| AUD-3 | MEDIUM | `FIXED` | `except Exception` → `except UnknownProjectError` dans `OrchestratorEngine._read_status`/`cli._print_project_status` : une corruption réelle propage désormais, jamais confondue avec `NOT_INITIALIZED`. |
| AUD-4 | MEDIUM | `FIXED` | `aido status` (CLI) consomme maintenant directement `OrchestratorEngine.status()` — la seconde traversée indépendante de `ProjectStatusReader` dans `cli.py` a été supprimée (REUSE FIRST). AUD-3 et AUD-4 corrigés dans le même changement, comme demandé : une seule source de vérité, un seul correctif suffit désormais. |
| AUD-5 | MEDIUM | documenté, `DEFERRED` | Aucun fingerprint générique construit (YAGNI — aucun MVP non-Python réel n'a encore établi le besoin) ; la limite (empreinte Python uniquement) est maintenant documentée explicitement dans `docs/QA_STRATEGY.md` §8.3. |
| AUD-6 | LOW | `FIXED` | `ClaudeCodeAdapter._availability_from_status` : seuls `"allowed"`/`"rejected"` (les deux seules valeurs réellement observées) sont mappés explicitement ; toute autre valeur reste `UNKNOWN`, jamais `QUOTA_EXHAUSTED` par défaut. |
| AUD-7 | LOW | `FIXED` | `project.id` validé par un motif explicite (lettres/chiffres/`.`/`_`/`-`, jamais de séparateur de chemin ni de `..`) avant de construire `state_dir` — fail-closed, aucune slugification silencieuse d'un id fourni. |
| AUD-8 | LOW | `FIXED` | Nouveau test réel (`tests/test_ralph_execution_engine.py::TestDefaultSubprocessRunnerTimeout`) : un vrai sous-processus lent local, timeout court, preuve que le process est réellement tué (jamais d'orphelin), pas seulement que l'exception est levée. |
| AUD-9 | LOW | `FIXED` | `docs/status.md` mis à jour (date, compte de tests) et reformulé en snapshot explicite plutôt qu'un quasi-contrat de compte exact — pas de CI dédiée ajoutée pour ça (YAGNI). |
| AUD-10 | LOW | classé `P16` | `Project.current_mvp_id` reste en l'état — candidat `DELETE` documenté, nécessite une analyse de compatibilité DB/API avant toute suppression ; voir §14, P16. |
| AUD-11 | LOW | `FIXED` | Nouveau test d'intégration réel (`tests/test_project_runtime.py::TestMultiMVPSequential`) : trois MVP séquentiels sous le même `project.id`/`state_dir`, sans conflit, sans fuite de WorkItem, sans dépendance à `current_mvp_id`. |

**Non fait, explicitement** :

- AUD-2 (fichiers non suivis supprimés par un worker) : le besoin
  (détecter, jamais bloquer automatiquement, jamais `git clean`, jamais
  de restauration automatique) reste réel mais n'a pas été implémenté
  dans cette tâche — un compromis explicite plutôt qu'une correction
  précipitée d'un mécanisme touchant `RalphExecutionEngine`/
  `GitGovernanceService`. Reste un `À VOTER` distinct, pas fermé.
- Aucune nouvelle dépendance ajoutée par cette remédiation.
- Aucun refactor au-delà du périmètre de chaque finding (AUD-3/AUD-4
  corrigés ensemble parce que le rapport d'audit les liait
  explicitement ; les autres corrections restent chacune locale à son
  propre fichier).

**Tests** : suite complète offline, aucune régression, tous les
nouveaux tests ci-dessus exercent le vrai chemin de production
(`ProjectConfig` → `ProjectRuntime`/`OrchestratorEngine` → `MVPManager`),
jamais une construction manuelle contournant le problème corrigé. Voir
`docs/status.md` pour le compte à jour.

### P13.5 — Frontière moteur/librairie : injection du `WorkerRegistry`, `workers:` optionnel — `DONE` (2026-09-24)

**Décision produit** : correction de frontière architecturale, évolution
directe du découplage P13/P1.1 — jamais une nouvelle architecture
parallèle. `ai-dev-orchestrator` ne doit plus considérer `aido.yaml`
comme la source de configuration complète du produit utilisateur ; le
moteur exécute désormais un plan (`ProjectConfig`) et un pool de workers
(`WorkerRegistry`) que l'application appelante (AIDO Code) peut construire
et fournir elle-même, sans jamais passer par un fichier `workers.yaml`
sur disque.

**Propriété inchangée** (comme P13) : `ai-dev-orchestrator` reste seul
propriétaire du **type/runtime** `WorkerRegistry`, de `WorkerSelector`
(seul propriétaire de la sélection DEV A/DEV B), de `QuotaManager`, des
`ProviderAdapter`s, de `RalphExecutionEngine`, de `MVPManager`/WorkItem
Flow, de la QA déterministe, de `GitGovernanceService`, et de l'état
persistant. Rien de tout cela n'est déplacé vers AIDO Code — seule
l'**instanciation** du `WorkerRegistry` pour un projet donné peut
désormais venir de l'appelant plutôt que d'un chemin de fichier lu par le
moteur.

**Changements (REUSE FIRST, changement minimal — aucun nouveau
framework DI, aucun loader universel)** :

- `ProjectConfig` (`project_config.py`) : la section `workers:` d'
  `aido.yaml` devient **optionnelle**. Absente, `workers_registry_path`
  reste `None` et `ProjectConfig` est un plan valide et complet pour la
  frontière moteur moderne — aucune régression pour un `aido.yaml`
  existant qui déclare encore `workers:` (chemin legacy entièrement
  conservé, testé, inchangé). `load_worker_registry()` lève désormais
  `NoWorkerRegistryConfiguredError` (sous-classe de `WorkerRegistryError`,
  jamais une seconde hiérarchie d'erreurs) quand aucun registry n'est
  configuré ni injecté.
- `OrchestratorEngine.__init__`/`.open()` acceptent un `worker_registry:
  WorkerRegistry | None = None` : quand fourni, utilisé pour
  `.workers()`/`.validate()`/`.probe_workers()`/`.run()`/la résolution du
  nom d'affichage worker dans `.status()` — jamais lu depuis
  `aido.yaml` dans ce cas. `None` retombe sur le chemin legacy
  (`ProjectConfig.load_worker_registry()`).
- `ProjectRuntime.open()` accepte le même `worker_registry=` et le
  transmet tel quel à la construction de `WorkerSelector` — **aucune
  logique de sélection n'est dupliquée ou réimplémentée** : c'est
  exactement la même construction `WorkerSelector(enabled_workers,
  quota_manager)` qu'avant, juste alimentée par un registry dont la
  provenance (fichier ou objet injecté) lui est indifférente.
- `ProjectConfig`'s propre constructeur Python (déjà public :
  `ProjectIdentity`/`ExecutionConfig`/`GitConfig`/`MVPConfig`/
  `WorkItemConfig`/`ValidationCommand`) sert directement de contrat
  « plan d'exécution typé » — **aucun second type introduit** pour ça, ni
  renommage (`ProjectConfig` reste le nom ; le distinguo legacy/moderne se
  fait uniquement sur la présence ou non de `workers_registry_path`).
- CLI historique `aido` (`cli.py`) : **inchangée fonctionnellement**,
  explicitement documentée comme surface legacy/transitoire dans son
  propre docstring de module — continue de lire `workers.registry`
  depuis `aido.yaml` comme avant, jamais migrée vers l'API moteur
  moderne par cette tâche. `orchestrator/resources/default_workers.yaml`
  documenté comme template legacy consommé uniquement par `aido init`,
  jamais la source de configuration worker du produit moderne.

**Tests** (`tests/test_project_config.py::TestOptionalWorkersSection`,
`tests/test_engine.py::TestWorkerRegistryInjection`/
`TestEngineLibraryBoundaryIntegration`,
`tests/test_project_runtime.py::TestWorkerRegistryInjection`) : injection
prouvée jusqu'au `WorkerSelector` réel (introspection directe de
`MVPManager._worker_selector`), un `ProjectConfig` sans section
`workers:` prouvé indépendant de tout chemin `workers.yaml`, deux projets
distincts partageant un seul `WorkerRegistry` injecté, le chemin legacy
prouvé toujours fonctionnel, et un test d'intégration bout en bout
construisant `WorkerRegistry` + `ProjectConfig` entièrement en Python
(aucun `aido.yaml`/`workers.yaml` sur disque) jusqu'à un WorkItem Flow
complet `COMPLETED`. Suite complète : voir `docs/status.md` pour le
compte à jour, zéro régression.

**Non fait, explicitement, par cette tâche** : ni GitLabRoadmap, ni M2
AIDO Code (sessions), ni CLI AIDO Code, ni framework DI/plugin, ni
parsing Markdown, ni suppression de la CLI `aido` historique, ni
déplacement du `WorkerRegistry`/`WorkerSelector`/`QuotaManager`/
`RalphExecutionEngine`/`MVPManager`/QA déterministe/`GitGovernanceService`
hors d'`ai-dev-orchestrator`.

### P13.6 — Retrait de la commande produit `aido` (cutover AIDO Code) — `DONE` (2026-09-24)

**Décision produit** : conclusion directe de P13/P13.5 — jamais une
nouvelle architecture. AIDO Code a atteint son propre cutover produit
(M1.4, puis M8 conclu par ce même changement côté AIDO Code) et devient
l'unique propriétaire de la commande utilisateur `aido`.
`ai-dev-orchestrator` cesse en conséquence d'installer cette commande :
il n'a plus jamais été qu'un moteur/librairie, et n'a désormais plus
aucune raison de revendiquer une surface CLI produit.

**Changement (minimal, YAGNI)** :

- `pyproject.toml` : la section `[project.scripts]` (`aido =
  "orchestrator.cli:main"`) est **retirée intégralement** — un `pip
  install ai-dev-orchestrator` seul n'installe plus aucune commande
  console.
- `orchestrator.cli` **reste dans le code source, importable** :
  utilisé directement par ses propres tests (`cli.main(argv)`, jamais un
  vrai sous-processus `aido`) et par le workflow de développement dual-
  repo déjà documenté (checkout sibling, `python -m orchestrator.cli
  ...`). Son docstring de module documente explicitement ce nouveau
  statut « legacy/internal — no longer installed ».
- `orchestrator/resources/default_workers.yaml` : **conservé**, toujours
  réellement consommé par `orchestrator.cli`'s propre chemin `init`
  legacy et ses tests (`tests/test_default_worker_registry.py`) — aucun
  ménage spéculatif ; documentation renforcée (« reachable only via a
  direct Python import, never a real end-user command »).
- Aucun nouveau binaire de compatibilité (`aido-engine`/`aido-legacy`,
  etc.) n'est ajouté — YAGNI, rien ne le justifie aujourd'hui.
- `README.md` : nouvelle section « CLI historique `orchestrator.cli`
  (legacy, interne) » remplaçant l'ancienne présentation « CLI `aido`
  (P1) » comme point d'entrée produit ; pointe désormais explicitement
  vers AIDO Code pour l'usage produit réel.

**Tests** (`tests/test_packaging.py`, `tests/test_cli.py`) :
`test_no_product_console_script_declared` (remplace l'ancienne
assertion positive sur la ligne d'entry point) prouve l'absence de
`[project.scripts]`/de toute déclaration `aido = ...` dans
`pyproject.toml` ; `test_wheel_builds_with_correct_metadata` prouve
qu'aucun wheel construit ne contient plus `aido` dans son
`entry_points.txt` (fichier absent ou présent-mais-sans-`aido`, les deux
acceptés) ; `test_legacy_cli_main_stays_importable_and_callable`
(remplace `test_pyproject_entry_point_matches_real_main`) prouve que
`orchestrator.cli.main` reste un callable Python valide malgré le
retrait de l'entry point. Suite complète, zéro régression.

**Non fait, explicitement, par cette tâche** : aucune suppression de
`orchestrator.cli`/`default_workers.yaml` eux-mêmes (encore réellement
utilisés en interne) ; aucun bump de version délibéré ; ni M2, ni
GitLabRoadmap.

### P13.7 — Pre-execution state safety : un WorkItem ne doit jamais rester `RUNNING` sans exécution — `DONE` (2026-09-25)

**Contexte, défaut réel** : le cutover M8 d'AIDO Code (P13.6) a révélé,
sur son propre `WI-M8-01`, un défaut réel du moteur : `prepare_work_item()`
(fail-closed, peut lever `DirtyWorkingTreeError`) était appelée **après**
`mark_work_item_running()`. Une erreur de préparation Git laissait donc
le WorkItem persisté `RUNNING`, sans qu'aucun `ExecutionRecord` n'ait
jamais existé — `RecoveryCoordinator.reconcile_work_item()` n'a
structurellement rien à réconcilier dans ce cas (`relevant = []` →
`return None`, voir `recovery.py`) : le WorkItem restait `RUNNING`
durablement, sans mécanisme de reprise. Ce défaut est réel, reproductible,
et distinct de l'invariant de recovery existant (une exécution RUNNING
*orpheline mais réelle* reste correctement réconciliée par
`RecoveryCoordinator` — inchangé par cette tâche).

**Invariant cible** : un WorkItem/son MVP ne sont marqués `RUNNING` que
lorsque tous les prérequis pré-exécution qui peuvent échouer ont déjà
réussi — jamais avant :

```
pre-execution validation/preparation
        ↓ success
mark RUNNING
        ↓
create/launch execution
```

Une erreur de pré-exécution laisse le WorkItem exactement dans son état
précédent (`READY`/`NEEDS_REWORK`/`RECOVERY_REQUIRED`/`WAITING`, selon
le chemin), jamais un nouvel état orphelin.

**Deux sites réels avaient cette forme**, tous deux dans
`mvp_manager.py`, corrigés par un simple réordonnancement (aucune
nouvelle machine à états, aucun rollback générique, aucun
`except Exception` catch-all, aucune mutation SQL directe, aucun retry
automatique, aucun faux `ExecutionRecord`) :

- **`_execute_work_item`** (partagé par le chemin frais `READY`/
  `NEEDS_REWORK`, `_try_resume_due_wait`, et
  `_try_resume_recovery_required` — les trois délèguent à cette même
  méthode) : `mark_mvp_running()`/`mark_work_item_running()` déplacés
  **après** `get_project()`, `ensure_runtime_exclusion()`,
  `prepare_work_item()`, et la résolution du profil DEV
  (`dev_worker.profile()`) — exactement le cas réel de `WI-M8-01`.
- **`_resume_dev_b_wait`** (reprise d'un wait `DEV_B_REVIEW` — un
  second site indépendant, découvert par l'inspection obligatoire de
  cette tâche, jamais mentionné dans le rapport WI-M8-01 initial) :
  `wait_coordinator.resolve()`/`mark_work_item_running()` déplacés
  **après** `_reconcile_governed_head()` (peut lever
  `GitHeadDriftError`). Ce site est particulièrement insidieux :
  l'`ExecutionRecord` de DEV A existe déjà (réussi, avant le wait), donc
  `RecoveryCoordinator` verrait un WorkItem `RUNNING` avec une dernière
  exécution déjà `SUCCEEDED` et ne ferait rien (`recovery.py`, "already
  SUCCEEDED ... out of scope here") — exactement la même forme
  d'orphelin durable que `WI-M8-01`, atteinte différemment. Le wait
  lui-même n'est plus consommé avant que la reconciliation réussisse :
  une reprise ratée reste due, retentable proprement, jamais perdue.

**MVP status** (§10 de la tâche) : `mark_mvp_running()` déplacé au même
point que `mark_work_item_running()` — idempotent
(`ProjectStateStore.mark_mvp_running` no-op si déjà `RUNNING`), aucun
autre point du code ne dépend de l'instant précis où le MVP passe
`RUNNING` par rapport à la préparation d'un WorkItem (vérifié par
recherche exhaustive).

**Invariant de recovery existant, inchangé** : une exécution `RUNNING`
réellement orpheline/interrompue (un `ExecutionRecord` existe, le
process qui la possédait a disparu) reste réconciliée exactement comme
avant, vers `RECOVERY_REQUIRED` puis une nouvelle tentative — cette
tâche ne touche à rien de `recovery.py` lui-même. Les deux concepts
restent nettement distincts : **échec pré-exécution** (aucun état
`RUNNING` jamais commité) vs. **échec/orphelin d'exécution** (mécanisme
de recovery existant, réel, inchangé).

**Pas de réparation rétroactive** : `WI-M8-01` (le cas historique réel
dans AIDO Code) n'est pas modifié — aucune migration/scan automatique
des anciens WorkItems `RUNNING` sans `ExecutionRecord` n'est tentée dans
cette tâche (YAGNI ; la cause d'un état historique arbitraire ne peut
pas être déduite automatiquement sans contrat supplémentaire).

**Tests** (`tests/test_mvp_manager_workitem_flow.py::
TestPreExecutionFailureNeverOrphansAWorkItem`, 4 nouveaux, chacun
vérifié rouge sans le correctif avant d'être vérifié vert avec) :
reproduction exacte du cas réel (arbre de travail avec une modification
TRACKED non commitée, WorkItem `READY`, `DirtyWorkingTreeError` observée
selon le contrat existant, WorkItem durablement `READY`, zéro
`ExecutionRecord`, zéro exécution worker, aucun handoff mensonger, MVP
non prématurément `RUNNING`) ; reprise après nettoyage (le même WorkItem
redémarre normalement, jusqu'à `COMPLETED`, sans changement manuel
d'id/MVP) ; équivalent `NEEDS_REWORK` (contexte QA précédent préservé,
aucun cycle QA incrémenté artificiellement) ; équivalent
`RECOVERY_REQUIRED` (reprise via `_try_resume_recovery_required`) ;
`GitHeadDriftError` sur reprise `DEV_B_REVIEW` (`_resume_dev_b_wait`,
le second site). Suite complète : 1310 passed (1306 avant), zéro
régression, `git diff --check` clean.

### P14 — Observabilité de consommation et efficacité économique — `APPROUVÉ`, après P13 (2026-09-19, non implémenté)

**Décision produit** : approuvée, statut `APPROUVÉ — APRÈS P13`. Aucun
WorkItem d'implémentation créé à ce jour. Cette sous-section documente le
contrat attendu pour que la future implémentation ait une référence
stable ; elle ne constitue pas un engagement de conception définitif.

**Objectif** : permettre d'identifier les phases, workers, providers et
mécanismes d'orchestration qui consomment le plus de ressources, afin de
pouvoir optimiser ultérieurement les parties les moins économiques du
produit. Cette capacité appartient exclusivement au moteur. AIDO Code
pourra ensuite présenter ces données, mais ne doit jamais recalculer
lui-même les métriques (même séparation de responsabilité que le reste
de cette roadmap : le moteur décide/calcule, le frontend affiche).

**Métriques à collecter**, par exécution, lorsque l'information est
réellement disponible : `project_id`, `mvp_id`, `work_item_id`, phase
(DEV A, DEV B, DEV FIX, QA, et toute autre phase réelle existante),
`worker_id`, provider, backend, modèle/`ExecutionProfile`, timestamps
début/fin, durée, statut, tokens d'entrée, tokens de sortie, tokens
d'entrée mis en cache si disponibles, tokens de raisonnement si
réellement exposés, nombre d'appels provider, nombre de tentatives,
nombre de reprises, `permission_mode`, coût observé ou estimé le cas
échéant. Une métrique qu'un backend ne fournit pas n'est jamais
fabriquée : elle reste `UNKNOWN`/`None`.

**Coût** : jamais de tarifs fournisseurs codés en dur dans la logique
métier. Ordre de préférence : (1) coût réellement fourni par le
provider ; (2) une grille tarifaire explicitement configurée et
versionnée, séparée du cœur de l'orchestration ; (3) à défaut, aucun
coût monétaire, seulement les métriques de consommation brutes. Tout
coût calculé distingue explicitement `OBSERVED_COST`, `ESTIMATED_COST`
et `UNKNOWN_COST`, jamais un montant présenté sans préciser sa
provenance.

**Agrégations** minimales : exécution, phase, WorkItem, MVP, projet,
worker, provider, modèle/profil. Notamment : tokens totaux, durée
totale, coût total quand disponible, coût moyen par WorkItem,
consommation moyenne par phase, nombre de retries, nombre de QA FAIL,
nombre de DEV FIX, ratio consommation / WorkItem `COMPLETED`.

**Top consommateurs** : une API permettant par exemple top phases par
tokens, top phases par coût estimé, top workers par consommation, top
providers par consommation, top WorkItems par consommation, top
surcoût retry/fix. Un rapport ne présente jamais un pourcentage calculé
à partir de données manquantes sans le signaler explicitement.

**Analyse d'efficacité** : comparer deux versions du moteur, deux
`ExecutionProfile`s, deux providers, deux stratégies de sélection, ou
deux périodes, pour détecter par exemple une augmentation du nombre
moyen d'exécutions par WorkItem, un contexte envoyé en hausse, un DEV B
systématiquement trop coûteux, trop de DEV FIX, des retries fréquents,
des phases longues sans valeur mesurable, ou un provider coûteux
utilisé pour des tâches simples. Séquence explicite, jamais inversée :
**MESURER → OBSERVER → COMPARER → OPTIMISER**. Aucune décision de
routing (`WorkerSelector` ou stratégie de sélection) n'est modifiée
automatiquement à partir de ces métriques ; une éventuelle automatisation
serait une décision produit séparée, non couverte par P14.

**Persistance** : REUSE FIRST. Réutiliser `ExecutionRecord`/
`ExecutionStore`/`QARunStore`/`ActivityReport` autant que possible avant
de créer un nouveau store ; ne jamais dupliquer une donnée déjà
disponible.

**API moteur (conceptuelle, noms définitifs à l'implémentation)** :
`engine.consumption(...)`, `engine.consumption_by_phase(...)`,
`engine.top_consumers(...)`, `engine.efficiency_report(...)` : la même
philosophie de façade que `orchestrator.engine.OrchestratorEngine` (P13),
jamais un second point d'entrée public parallèle.

**Côté AIDO Code** (une fois P14 implémenté) : `/usage`, `/cost`,
`/efficiency` dans le REPL, ou `aido usage`/`aido usage --by phase`/
`aido usage --by provider`/`aido usage --work-item WI-42` en mode non
interactif, toujours en consommant l'API moteur ci-dessus, jamais en
recalculant quoi que ce soit côté frontend.

**Critères d'acceptation initiaux** (P14 pourra passer `DONE`
lorsque) : (1) chaque nouvelle exécution conserve les métriques
réellement disponibles ; (2) aucune donnée inconnue n'est inventée ;
(3) les métriques historiques restent rétrocompatibles ; (4) les
agrégations phase/worker/provider/WorkItem sont déterministes ; (5) un
rapport permet d'identifier les plus gros consommateurs ; (6) le coût
observé et le coût estimé sont distingués ; (7) les tarifs ne sont pas
codés en dur dans la logique d'orchestration ; (8) aucune sélection de
worker n'est automatiquement modifiée par cette fonctionnalité ; (9)
les tests sont entièrement offline ; (10) les métriques peuvent être
consommées par un frontend externe via l'API moteur.

#### Mode de permission d'exécution des workers — implémenté

Constat factuel qui motivait cette exigence : Claude Code et Codex
tournent sans surveillance sur la machine du mainteneur parce que leur
configuration CLI/environnement locale est déjà permissive ("YOLO"/
bypass), en dehors d'AI Dev Orchestrator — un contributeur clonant le
dépôt pouvait donc rencontrer des invites interactives ou des exécutions
bloquées, sans qu'AIDO ne porte de politique de permission explicite par
projet. Voir aussi `CONTRIBUTING.md`, « Real worker execution and
permissions ».

Contrat implémenté (`orchestrator.execution_policy.ExecutionPermissionMode`,
`aido.yaml`'s `execution.permission_mode`) :

```
execution permissions:
  standard       # AIDO ne demande jamais l'exécution non surveillée/bypass ;
                  # demande explicitement le mécanisme sûr/normal du provider quand il existe
  unrestricted   # AIDO demande explicitement l'exécution non surveillée vérifiée
                  # ("YOLO"/bypass) là où le backend le supporte honnêtement
```

Invariants respectés par l'implémentation (`RalphExecutionEngine`,
`vibe_ralph_bridge.py`) :

- `unrestricted` est un opt-in explicite — jamais un défaut silencieux ;
  un moteur non configuré (`permission_mode=None`) n'ajoute aucun flag de
  permission du tout (comportement identique à avant P12), jamais
  `unrestricted` par défaut.
- AIDO n'infère jamais `unrestricted` de la configuration globale de la
  machine du développeur.
- Le mode effectif est observable/auditable par exécution :
  `ExecutionRecord.permission_mode` (P12), migration SQLite idempotente,
  lignes historiques décodées honnêtement comme `None`, jamais fabriquées.
- Aucun identifiant/secret n'est jamais stocké dans la configuration de
  projet — l'authentification provider reste possédée par le CLI/
  l'environnement du provider.
- `MVPManager` ne contient aucun branchement de permission spécifique à
  un provider ; `WorkerSelector` ne connaît rien des flags de permission
  CLI — toute la traduction vit à la frontière `RalphExecutionEngine`/
  `vibe_ralph_bridge.py`, vérifiée contre les CLI réellement installées
  (`claude --help`/`codex --help`/`vibe --help`), jamais inventée. Détail
  complet, y compris la table de mapping vérifiée par backend :
  `docs/PROJECT_CONFIG.md`.
- Une combinaison non supportée échoue avant tout lancement de
  subprocess (`UnsupportedPermissionModeError`), jamais une dégradation
  silencieuse.

### P15 — Prompt optimization externe — `APPROUVÉ POUR ÉTUDE`, pas d'intégration (2026-09-23)

**Décision produit** : approuvée pour étude uniquement. Aucune
dépendance installée, aucun WorkItem d'implémentation. Candidat
principal : **Opik Optimizer**. Comparaison complète des candidats
étudiés : `docs/ECOSYSTEM.md`.

**Besoin couvert, s'il est un jour implémenté** : prompt existant +
dataset réel + métrique réelle → variantes générées → comparaison →
candidat amélioré → **validation par notre propre QA déterministe**
(`InternalQAEngine`, jamais l'auto-évaluation de l'outil d'optimisation
lui-même). Séquence obligatoire, jamais inversée :

```
MESURER → IDENTIFIER → DATASET → OPTIMISER → QA → COMPARER → DÉCISION HUMAINE
```

**Jamais** : Opik (ou tout autre optimiseur) modifiant automatiquement
un prompt en production. Toute adoption d'une variante reste une
décision humaine explicite, jamais automatisée par cette capacité.

**Contrat de dépendance avec P14** : P14 (observabilité de consommation,
`APPROUVÉ — APRÈS P13`, non implémenté) est la seule source de
télémétrie (tokens, durées, QA FAIL, DEV FIX, retries, coût). P15
réutiliserait ces données telles quelles — **aucun second système de
télémétrie** ne serait construit pour Opik. P15 reste donc lui-même
non actionnable tant que P14 n'est pas implémenté.

**Contraintes si jamais implémenté** (non construites ici, pour
référence future) : Opik Optimizer reste optionnel, externe,
désactivable, hors du cœur (`src/orchestrator/`), et jamais câblé dans
le WorkItem Flow nominal (§5) — une capacité additionnelle invoquée
explicitement, jamais enchaînée automatiquement, exactement comme
`PlanningCoordinator`/`ApprovalCoordinator` (§10).

### P16 — Revue de simplification YAGNI/REUSE FIRST — `APPROUVÉ POUR REVUE`, pas de refactor automatique (2026-09-23)

**Décision produit** : approuvée pour une revue structurée, jamais une
autorisation de réécrire le projet. Ordre obligatoire pour tout candidat
de simplification ou toute recommandation d'audit externe :

```
1 DELETE
2 STDLIB
3 EXISTING PROJECT PRIMITIVE
4 EXISTING DEPENDENCY
5 MATURE EXTERNAL PACKAGE
6 BUILD
```

**Principe** : *external audits are inputs, not authority.* Un finding
d'audit ne déclenche jamais automatiquement un refactor, une dépendance,
ou un changement d'architecture. Workflow obligatoire pour chaque
finding :

```
AUDIT FINDING → REPRODUCE → CONFIRM → DELETE/STDLIB/REUSE/PACKAGE/BUILD → TEST → MEASURE → ACCEPT/REJECT
```

C'est exactement le processus suivi par P13.4 ci-dessus : chaque finding
Mistral a été relu/reproduit dans le code réel avant toute correction
(voir `docs/reports/mistral-engine-audit-2026-09-23.md`), jamais accepté
tel quel.

**Candidats déjà identifiés** (à traiter au fil de l'eau, jamais comme
un refactor global d'un seul coup) :

- **AUD-4** (duplication `aido status` CLI / `OrchestratorEngine.status()`)
  — déjà corrigé par P13.4 (REUSE FIRST immédiat, pas différé).
- **`Project.current_mvp_id`** (AUD-10) — candidat `DELETE` : écrit à
  chaque `bootstrap()`, lu par aucune décision (`engine.py`/
  `mvp_manager.py` utilisent tous `cfg.mvp.id`, jamais ce champ).
  Suppression non faite ici : nécessite une analyse de compatibilité
  SQLite/API avant tout retrait, pas justifiée sans preuve de gain net
  pour ce seul correctif.
- **Fingerprint d'environnement générique non-Python** (AUD-5) — chemin
  `BUILD` explicitement rejeté tant qu'aucun MVP non-Python réel
  n'établit le besoin (YAGNI) ; documenté comme limitation connue
  plutôt que construit par anticipation.

**Chaque candidat de simplification** doit démontrer un bénéfice concret
avant correction — safety, correctness, maintainability, testability, ou
future feature enablement — jamais "plus joli" comme seule
justification. Format attendu pour toute proposition future :

```
BEFORE : LOC / fichiers / complexité / dépendances
AFTER  : LOC / fichiers / complexité / dépendances
NET GAIN
```

Une dépendance n'est jamais acceptée pour économiser quelques lignes
simples ; évaluer aussi maintenance, licence, activité du projet,
stabilité d'API, surface de supply-chain.

**Enseignements étudiés sans adoption** (voir `docs/ECOSYSTEM.md` pour
le détail) :

- **Superpowers** (discipline de cadrage Claude Code, séparation
  planning/exécution, review gates, patterns TDD) : `INSPIRE ONLY`,
  jamais intégré. Comparé à ce que ce projet fait déjà (WorkItem Flow,
  DEV B corrective review, QA déterministe) — pas de gap identifié
  justifiant une dépendance.
- **Ponytail** (YAGNI/stdlib-first comme benchmark intellectuel de
  simplicité) : `REFERENCE / A-B BENCHMARK ONLY`, jamais intégré — les
  principes qu'il incarne (stdlib avant dépendance, solution minimale)
  sont déjà la politique explicite de ce projet (§1, `CONTRIBUTING.md`).

### P17 — Quota-aware worker routing — `DONE`

Défaut observé : Alice/Anthropic et Victor/OpenAI ont tous deux une
priorité de 100. Le sélecteur ignorait `quota_windows[].utilization` et
tranchait donc par `worker_id`, ce qui favorisait lexicalement Alice même
quand le quota OpenAI était moins utilisé. `QuotaManager` fournissait déjà
ces observations lors du probe de disponibilité.

Après capacités, exclusions/auteur distinct, qualité minimale et
disponibilité, la policy d'indépendance provider pour la review est
appliquée avant le classement quota. La pression vaut le maximum des
`utilization` connues des fenêtres. Les `None` sont ignorées ; si toutes
sont inconnues, la pression reste `None` et n'est jamais fabriquée à 0 %.
Les candidats inconnus restent éligibles et sont neutres dans la bande.

La bande de tolérance vaut 0,10 (10 points de pourcentage) au-dessus de
la plus faible pression connue. Les providers connus dans cette bande et
les providers à pression inconnue sont départagés par priorité
décroissante, puis `worker_id` lexical ; les pressions connues situées
hors de cette bande ne préemptent pas les candidats de la bande. Aucun
second probe n'est effectué : le classement réutilise le même résultat
`QuotaManager.get()` que la disponibilité. P17 ne concerne que le routing
à partir du quota observé et reste indépendant de P14 (tokens, coûts et
métriques d'efficacité).

### P18 — Live execution events and graceful interruption — `DONE` (GO humain 2026-09-26 ; P18-01/P18-02/P18-03 `DONE`)

**Constat réel**, vérifié par inspection directe de `src/orchestrator/
engine.py`/`mvp_manager.py`/`execution_store.py`/`adaptive_execution.py`/
`recovery.py` le 2026-09-26, à l'occasion de la préparation de M3 côté
AIDO Code (`~/projects/aido-code`) :

1. `OrchestratorEngine.run()` n'émet qu'un événement grossier
   `work_item.<status>` par appel à `run_next_work_item()`, une fois que
   toute la séquence DEV A/DEV B/QA/merge de ce WorkItem est déjà
   terminée (`MVPManager._execute_work_item` exécute cette séquence de
   façon synchrone, sans bus d'événements interne). C'est exactement le
   comportement que le propre docstring d'`EngineEvent` documente déjà
   comme un manque non comblé, jamais une régression de cette revue.
2. `ExecutionSnapshot` (la façade publique) n'expose ni `backend`, ni
   `model`, ni un identifiant de profil d'exécution, ni `quality_tier`,
   ni `reasoning_effort` — alors que `backend`/`model`/`reasoning_effort`
   existent déjà, durablement, sur `ExecutionRecord`
   (`execution_store.py`), et que `profile_id`/`quality_tier` existent
   déjà, durablement, sur `AdaptiveExecutionDecision`
   (`adaptive_execution.py`) lorsque l'Adaptive Execution Selector est
   configuré. Rien de tout cela n'est aujourd'hui joint/exposé par
   `OrchestratorEngine`.
3. Aucun point du moteur ne traite explicitement l'interruption d'un
   `Ctrl+C` pendant `.run()` (précision apportée ci-dessous : via
   `asyncio.run()`, le fait réellement observable à l'intérieur des
   coroutines est d'abord `asyncio.CancelledError`, pas directement
   `KeyboardInterrupt` — voir "Interruption — comportement actuel exact
   et contrat cible"). Le mécanisme de reprise durable existe déjà et
   fonctionne indépendamment de ce manque : `execution_store` est déjà
   transmis à `MVPManager` par `ProjectRuntime.open()`, ce qui active
   `RecoveryCoordinator` en production ; une exécution laissée
   `RUNNING` par une interruption est donc déjà reconciliée en
   `RECOVERY_REQUIRED` au prochain `run_next_work_item()`. Le manque
   réel est seulement l'absence d'une gestion explicite/propre de
   l'interruption elle-même (jamais une absence de mécanisme de
   reprise).

**Besoin produit** : AIDO Code (M3 — pilotage conversationnel et
exécution live, `DRAFT`) a besoin d'une timeline live (DEV A/DEV B/DEV
FIX/QA/Git, avec worker/provider/backend/profil/modèle/quality_tier/
reasoning_effort réellement décidés) et d'une interruption Ctrl+C
propre pendant un `aido run` réel, sans jamais reconstruire cela en
lisant Git/SQLite/stdout depuis AIDO Code — frontière cross-repo stricte
(`ai-dev-orchestrator`/`aido-code` restent deux dépôts séparés).

**REUSE FIRST — rien de ce qui suit n'est une nouvelle donnée** :
`ExecutionStore`, `AdaptiveExecutionDecisionStore`, `QARunStore`,
`GitGovernanceService`/`GitWorkItemStatus`, et `RecoveryCoordinator`
possèdent déjà toutes les données et toute la mécanique de reprise
nécessaires. Ce qui manque réellement :

- un mécanisme d'exposition en temps réel de transitions plus fines que
  `work_item.<status>` (callback, itérateur/générateur, ou event sink —
  la forme exacte reste à trancher côté moteur, jamais imposée depuis
  AIDO Code) ;
- l'enrichissement de la snapshot d'exécution publique avec les champs
  déjà persistés (`backend`/`model`/`reasoning_effort`/`profile_id`/
  `quality_tier`) ;
- un traitement explicite de l'interruption (`asyncio.CancelledError`
  côté coroutines, `KeyboardInterrupt` à la frontière synchrone) autour
  de la boucle de `.run()`, qui s'appuie sur le `RecoveryCoordinator`
  existant — jamais un second mécanisme de recovery.

**Hors périmètre de cette proposition** : aucune nouvelle politique de
sélection de worker, aucune seconde autorité de QA/merge/recovery,
aucun changement de comportement de P17 (quota-aware worker routing,
désormais fusionné dans `main`, `DONE` — voir sous-section P17
ci-dessus — indépendant de P18).

**Relecture de code complémentaire (2026-09-26, avant approbation)**,
sur le chemin réel d'exécution/interruption :

- `RalphExecutionEngine.execute()` (`ralph_execution_engine.py`) crée
  l'`ExecutionRecord` en `RUNNING` **avant** de lancer le sous-processus
  `ralph` (`_default_subprocess_runner`,
  `asyncio.create_subprocess_exec` + `await
  asyncio.wait_for(process.communicate(), timeout=...)`). Seul un
  `asyncio.TimeoutError` interne déclenche `process.kill()` +
  `mark_interrupted()`. Un `KeyboardInterrupt` levé pendant cet `await`
  n'est intercepté nulle part sur ce chemin : il se propage tel quel,
  **le sous-processus `ralph` n'est jamais tué explicitement**, et
  l'`ExecutionRecord` reste `RUNNING` — jamais `INTERRUPTED` — jusqu'à
  la reconciliation suivante. C'est le seul vrai risque de process
  enfant orphelin identifié.
- Le QA déterministe (`internal_qa_engine.py`) lance ses propres
  sous-processus (`pytest`, `git diff`) via `subprocess.run()`
  **synchrone**, dont l'implémentation standard de la bibliothèque tue
  déjà le child et l'attend (`process.kill()`/`wait()`) avant de
  repropager toute exception — y compris `KeyboardInterrupt`. Le risque
  de process orphelin n'existe donc pas côté QA ; seul le statut du
  `QARun`/du WorkItem doit rester non ambigu (aucun verdict fabriqué).
- `RecoveryCoordinator.reconcile_work_item()` (`recovery.py`) traite
  déjà, de façon strictement équivalente, une exécution restée
  `RUNNING` (orpheline, fatalité inconnue) et une exécution déjà
  `INTERRUPTED` (fatalité connue, ex. timeout Ralph) : dans les deux
  cas, la même reconciliation bascule le WorkItem en
  `RECOVERY_REQUIRED` avec un handoff durable. **Rien à construire
  ici** : le seul manque réel est de faire terminer l'exécution
  interrompue par `mark_interrupted()` (plutôt que de la laisser
  `RUNNING`), et de tuer effectivement le sous-processus.
- `_try_resume_recovery_required()` relance un WorkItem
  `RECOVERY_REQUIRED` en rappelant directement `_execute_work_item` —
  c'est-à-dire exactement le même chemin DEV A/DEV B qu'une tentative
  fraîche. Il n'existe donc **aucun** fait distinct "recovery
  started"/"recovery resumed" à observer : la reprise réelle est déjà
  entièrement couverte par les événements `dev_a.*`/`dev_b.*` de la
  tentative relancée.
- `GitGovernanceService.merge()` est un appel Git local unique, rapide,
  non subdivisable en une phase "started" observable séparément de son
  issue — il réussit, échoue (`GitHeadDriftError`/`NotMergeableError`),
  ou bascule `CONFLICT`/`FAILED`, sans fenêtre d'interruption réaliste
  entre un "started" et un "completed" distincts.

Ces constats affinent la conception ci-dessous : DTO, catalogue
d'événements, et découpage en trois WorkItems.

#### Architecture live retenue

**Callback synchrone optionnel, filé dans l'appel existant — pas de
flux/itérateur, pas de thread, pas de daemon, pas de broker.**

```python
def run(
    self, *, max_cycles: int = DEFAULT_MAX_CYCLES,
    on_event: Callable[[EngineEvent], None] | None = None,
) -> RunResult:
    ...
```

Justification : `MVPManager._execute_work_item` exécute déjà DEV A →
DEV B → QA → merge de façon strictement synchrone, un seul `await` à la
fois, sur une seule boucle asyncio, jamais en parallèle. Un callback
appelé en ligne, au moment exact de chaque transition réelle, est donc
suffisant et strictement plus simple qu'un itérateur/générateur ou
qu'un event bus : aucune queue, aucune sérialisation, aucun buffering,
aucune vie propre au-delà de l'appel `run()` lui-même. `on_event=None`
(défaut) laisse `run()` strictement inchangé — compatibilité totale
avec tout appelant existant (AIDO Code y compris), zéro changement de
signature retour (`RunResult` inchangé).

Le callback est filé, en paramètre optionnel avec défaut `None`, à
travers exactement la même chaîne d'appel existante, sans la
réorganiser :

```
OrchestratorEngine.run(on_event=...)
  -> ProjectRuntime.manager.run_next_work_item(mvp_id, on_event=...)
    -> MVPManager._execute_work_item(..., on_event=...)
      -> _run_development(..., on_event=..., phase="dev_a"|"dev_b"|"dev_fix")
      -> (QA phase existante, on_event=...)
      -> (merge existant, on_event=...)
    -> RecoveryCoordinator.reconcile_mvp(..., on_event=...)  # work_item.recovery_required
```

Un appel `on_event(event)` qui lève est de la responsabilité de
l'appelant — l'engine ne doit ni l'avaler silencieusement ni
interrompre le WorkItem Flow à cause d'un callback fautif ; propagation
directe, jamais un `except Exception: pass` autour de l'appel.

**Note d'implémentation, pas une invention de transition** :
`_run_development` étant l'unique fonction partagée par DEV A/DEV
B/DEV FIX (même rôle interne `"developer"` dans les trois cas), c'est
chaque site d'appel de `_execute_work_item` qui doit fournir
explicitement le label de phase (`"dev_a"`/`"dev_b"`/`"dev_fix"`) —
`_run_development` elle-même ne peut pas déduire la phase depuis les
données dont elle dispose aujourd'hui.

#### Frontière de couches : où vit `EngineEvent` (précision 2026-09-26)

Vérifié par lecture directe des imports réels : `orchestrator.engine`
importe déjà `orchestrator.project_runtime` (qui construit
`MVPManager`) — `engine.py` dépend donc de la couche
`ProjectRuntime`/`MVPManager`, jamais l'inverse
(`mvp_manager.py` n'importe `orchestrator.engine` nulle part
aujourd'hui). Or ce sont les faits DEV A/DEV B/QA/Git produits **dans**
`MVPManager` qui doivent construire des `EngineEvent` pour les passer à
`on_event`. Si `EngineEvent` reste défini tel quel dans
`orchestrator.engine`, `mvp_manager.py` devrait l'importer depuis là —
un import inverse qui, combiné à la dépendance existante
`engine.py` → `project_runtime.py` → `mvp_manager.py`, fermerait un
cycle d'import.

**Décision retenue (option A du contrat de revue, la plus simple)** :
le dataclass `EngineEvent` (et le petit alias de type
`EngineEventKind`/la liste des `kind` valides, si utile) déménage dans
un module neutre, sans aucune dépendance vers `engine.py` ni
`mvp_manager.py` — par exemple `orchestrator.engine_events` — et
`orchestrator.engine` le **ré-exporte** (`from orchestrator.engine_events
import EngineEvent`), pour que `from orchestrator.engine import
EngineEvent` continue de fonctionner à l'identique pour tout code
existant. `mvp_manager.py` importe le DTO depuis ce même module neutre,
jamais depuis `orchestrator.engine`.

Option B (un émetteur interne injecté dans `MVPManager`, traduisant des
faits internes en `EngineEvent` ailleurs) a été écartée : elle
ajouterait une couche d'indirection (un second type de "fait interne"
à traduire) pour un problème que déplacer un dataclass sans
comportement suffit déjà à résoudre — moins simple, sans bénéfice
réel (KISS).

**Invariants que P18 doit respecter, quelle que soit l'implémentation
retenue au moment du code** :

- un seul contrat public `EngineEvent` — jamais deux DTO concurrents ;
- `OrchestratorEngine` reste l'unique façade publique (`.run(...,
  on_event=...)`) — le module où vit la définition du DTO n'est pas une
  nouvelle surface publique, seul l'import réexporté depuis
  `orchestrator.engine` compte comme contrat pour un appelant externe ;
- `MVPManager` ne dépend jamais de la façade `orchestrator.engine` —
  aucun nouvel import de `mvp_manager.py` vers `engine.py`, sous aucune
  forme.

#### `RunResult.events` vs `on_event` — deux canaux distincts, jamais fusionnés

Pour que `on_event=None` garantisse réellement un comportement
historique inchangé, P18 **ne change ni la forme ni le contenu** de
`RunResult.events` :

- `RunResult.events` reste exactement ce qu'il est aujourd'hui : un
  `work_item.<status>` grossier par appel réel à
  `run_next_work_item()`, jamais une liste de dizaines d'événements
  DEV A/DEV B/QA/Git — un appelant existant qui inspecte
  `RunResult.events` après un `run()` ne voit **aucun** changement.
- `on_event` est le seul canal des événements fins (DEV
  A/B/FIX/QA/Git/interruption) introduits par P18-02/P18-03 — livrés en
  direct, jamais accumulés dans `RunResult`.
- Un `work_item.<status>` continue d'exister dans `RunResult.events`
  **et** est, en plus, livré à `on_event` quand il est fourni (P18-01) —
  jamais l'un à la place de l'autre.
- AIDO Code M3 consomme `on_event` pour sa timeline live ;
  `RunResult.events` reste ce que `M1`-`M2.1` connaissent déjà.
- P18 n'ajoute **aucune persistance** des événements fins (pas de
  table, pas de replay, pas d'historique) — un besoin réel de
  relecture/replay d'événements est une décision produit séparée,
  future, YAGNI ici.

#### Sémantique du callback `on_event`

- Appelé **synchrone**, dans le même thread/la même boucle asyncio que
  `run()` — jamais un thread dédié, jamais une queue, jamais un
  callback `async def`, jamais un broker.
- Appelé en **ordre déterministe**, exactement dans l'ordre où chaque
  fait réel se produit — jamais réordonné, jamais batché.
- Doit rester léger : `on_event` appartient à l'appelant/frontend, il
  ne fait jamais partie de l'autorité d'orchestration — l'engine ne lui
  délègue aucune décision, ne l'attend jamais pour décider de la suite.
- **Règle exacte en cas d'exception levée par `on_event` lui-même** :
  l'exception se propage **immédiatement et telle quelle** à l'appelant
  de `.run()` — jamais avalée (`except Exception: pass` interdit),
  jamais convertie en `EngineError`/un type interne. L'exécution en
  cours n'est ni finalisée en `SUCCEEDED` ni relancée automatiquement à
  cause de ce callback fautif : son état persisté reste exactement ce
  qu'il était juste avant l'appel `on_event` qui a levé — un WorkItem
  dont l'exécution avait déjà été finalisée (ex. `SUCCEEDED`) juste
  avant l'appel `on_event` qui a levé reste finalisé ainsi ; un
  callback fautif ne doit jamais pouvoir corrompre l'état déjà persisté
  d'un fait déjà accompli. C'est un contrat déterministe, couvert par
  test (voir P18-01).

#### `EngineEvent` — DTO public étendu

Le type existant `EngineEvent`
(`kind`/`timestamp`/`project_id`/`mvp_id`/`work_item_id`/`payload`) est
**étendu**, jamais remplacé par un second type : les événements
grossiers `work_item.<status>` déjà retournés par `RunResult.events`
gardent exactement la même forme pour tout consommateur existant. Tous
les nouveaux champs sont optionnels, `None` par défaut :

```python
@dataclass(frozen=True, slots=True)
class EngineEvent:
    kind: str                          # event_type, ex. "dev_a.started" — inchangé de nom (déjà "kind")
    timestamp: str
    project_id: str
    mvp_id: str
    work_item_id: str
    payload: dict[str, Any]            # inchangé — détail structuré minimal, jamais un blob libre

    execution_id: str | None = None
    phase: str | None = None           # "dev_a" | "dev_b" | "dev_fix" | "qa" | "git" | "run" | None
    status: str | None = None          # statut ExecutionStatus/QAVerdict/GitWorkItemStatus concerné, en str
    worker_id: str | None = None
    worker_display_name: str | None = None
    provider: str | None = None
    backend: str | None = None
    profile_id: str | None = None
    model: str | None = None
    quality_tier: str | None = None
    reasoning_effort: str | None = None
    commit_sha: str | None = None
```

`payload` reste le seul emplacement pour un détail structuré
supplémentaire réellement nécessaire (ex. `resumed_from_recovery: bool`
sur un `dev_a.selected`/`dev_b.selected` de reprise — voir ci-dessous) ;
jamais un doublon des champs typés ci-dessus. Une donnée inconnue au
moment de l'événement reste `None`, jamais fabriquée. Aucun champ n'est
déductible par AIDO Code depuis `worker_id` seul —
`provider`/`backend`/`profile_id`/`model`/`quality_tier`/
`reasoning_effort` sont toujours portés explicitement par l'événement
lui-même.

#### Catalogue d'événements — uniquement des faits réels

```
work_item.started
work_item.waiting
work_item.recovery_required
work_item.blocked
work_item.failed
work_item.completed

dev_a.selected
dev_a.started
dev_a.completed
dev_a.failed
dev_a.interrupted

dev_b.selected
dev_b.started
dev_b.completed
dev_b.failed
dev_b.interrupted

dev_fix.selected
dev_fix.started
dev_fix.completed
dev_fix.failed
dev_fix.interrupted

qa.started
qa.pass
qa.fail
qa.inconclusive

git.merge_ready
git.merge_completed

run.interruption_requested
run.interrupted
```

Écarts assumés, documentés, par rapport à la liste cible initialement
envisagée :

- **`recovery.required` fusionné dans `work_item.recovery_required`** :
  un seul fait réel (la reconciliation bascule le WorkItem), jamais
  deux noms pour la même transition.
- **`recovery.started`/`recovery.resumed` retirés** :
  `_try_resume_recovery_required` relance littéralement
  `_execute_work_item` — la reprise réelle EST la séquence
  `dev_a.selected`/`dev_a.started` (ou `dev_b.*`) de la tentative
  relancée. Un `payload={"resumed_from_recovery": true}` sur cet
  événement porte l'information sans dupliquer la transition.
- **`git.merge_started` retiré** : `GitGovernanceService.merge()` est
  un appel Git local atomique et rapide, sans phase intermédiaire
  réellement observable entre "started" et son issue — voir la
  relecture de code ci-dessus.

#### Modèles/tiers/reasoning réellement configurés (`config/workers.yaml`, vérifié 2026-09-26)

Conforme à l'attendu, sans écart :

| Worker | Provider/backend | economy | standard | deep |
|---|---|---|---|---|
| Alice / Lydie | anthropic / claude_code | haiku, `SIMPLE` | sonnet, `STANDARD` | sonnet, `COMPLEX` |
| Victor / Yannick(oscar) | openai / codex | gpt-6-luna, `SIMPLE`, `reasoning_effort=low` | gpt-6-sol, `STANDARD`, `reasoning_effort=medium` | gpt-6-astra, `COMPLEX`, `reasoning_effort=high` |
| Nathaniel (milo) / Juno | mistral / vibe | — | vibe-default, `STANDARD` (pas de `reasoning_effort`) | — |
| Arthur / Nora | gravity / gravity | — | claude-sonnet-4-6, `STANDARD` (pas de `reasoning_effort`) | — |

(Dana/Kai, DeepSeek/Kimi, retirés en P20.) `quality_tier` (capacité minimale requise) et
`reasoning_effort` (effort du provider, quand il en expose un) restent
deux notions distinctes, jamais confondues dans le DTO ni dans son
mapping.

#### État réel d'Adaptive Execution — non branché, P18 ne le branche pas

Confirmé de nouveau par lecture directe : `ProjectRuntime.open()` ne
construit et ne passe **aucun** `adaptive_execution_selector` à
`MVPManager` (`project_runtime.py`). En conséquence, `_select_dev_worker`
retourne toujours `(worker, None, None)`, et `_execute_work_item`
retombe systématiquement sur `dev_worker.profile()` (le
`default_profile_id` du worker, ex. `standard`). **P18 n'active pas
Adaptive Execution** : les événements DEV A/DEV B/DEV FIX exposent le
`profile_id`/`model`/`quality_tier`/`reasoning_effort` **réellement
exécutés aujourd'hui**, c'est-à-dire ceux du profil par défaut du
worker sélectionné par `WorkerSelector` — jamais une valeur
qu'Adaptive Execution *aurait* choisie si elle était branchée. Le
branchement réel d'Adaptive Execution en production reste une décision
produit séparée, non couverte par P18.

**Provenance exacte de chaque champ (précision 2026-09-26)** — aucun
n'est déduit du seul `worker_id` :

- `model`/`reasoning_effort` : déjà portés par `ExecutionRecord`
  (`execution_store.py`) — l'événement les lit directement depuis le
  `ExecutionRecord`/`ExecutionResult` réellement produit par
  `RalphExecutionEngine.execute()`, jamais recalculés séparément.
- `profile_id`/`quality_tier` : **absents d'`ExecutionRecord`
  aujourd'hui** (seule `AdaptiveExecutionDecision`, non alimentée en
  production, les porte). P18 ne les fait pas migrer dans
  `ExecutionRecord`/SQLite — YAGNI, aucune persistance supplémentaire
  pour la seule timeline. Le moteur les résout **au moment réel où le
  profil est choisi**, dans `_select_dev_worker`/`_execute_work_item`
  (aujourd'hui : `dev_worker.profile()`, qui expose déjà `profile_id`
  implicitement via `default_profile_id` et l'objet profil
  lui-même, et `quality_tier` via ce même profil) — cette valeur est
  portée directement dans le contexte qui construit l'événement
  `*.selected`/`*.started`, jamais relue depuis une table après coup.
- Si Adaptive Execution est branché plus tard (hors P18), le même point
  de construction d'événement lira `AdaptiveExecutionDecision` à la
  place de `dev_worker.profile()` — sans changer le DTO `EngineEvent`
  ni sa sémantique.
- AIDO Code ne déduit jamais aucun de ces champs depuis `worker_id` —
  toute valeur vient de l'événement lui-même, ou reste `None`.

#### Interruption — comportement actuel exact et contrat cible

**Chemin réel aujourd'hui** : `KeyboardInterrupt` pendant `await
process.communicate()` (dans `_default_subprocess_runner`) n'est
intercepté par rien — ni par `RalphExecutionEngine.execute()` (seuls
`RalphTimeoutError`/`FileNotFoundError`/`RalphExecutionEngineError`
sont attrapés, jamais `BaseException`), ni par `MVPManager`, ni par
`OrchestratorEngine.run()`/`_drive()`. Conséquences observées par
lecture de code :

- le sous-processus `ralph` (et son propre enfant provider) **n'est
  pas tué** — orphelin tant que rien ne le fait ;
- l'`ExecutionRecord` reste `RUNNING` (jamais `INTERRUPTED`) ;
- `finally: shutil.rmtree(runtime_dir, ...)` s'exécute quand même
  (nettoyage du répertoire Ralph temporaire, sans effet sur le
  sous-processus) ;
- `finally: runtime.close()` dans `OrchestratorEngine.run()` s'exécute
  aussi (fermetures SQLite propres) ;
- au `run_next_work_item()` suivant, `RecoveryCoordinator` détecte
  l'exécution `RUNNING` sans propriétaire et bascule le WorkItem en
  `RECOVERY_REQUIRED` — mais **sans avoir jamais tué le process
  orphelin**.

**Correction 2026-09-26 — ce n'est pas seulement `KeyboardInterrupt`** :
`_drive()` appelle `asyncio.run(runtime.manager.run_next_work_item(...))`.
Sur un vrai SIGINT/Ctrl+C, le comportement réel de `asyncio.run()`
(`asyncio/runners.py`, toutes versions Python supportées par ce
projet) est en deux temps :

1. Le `KeyboardInterrupt` interrompt d'abord la boucle asyncio elle-même
   (typiquement à l'intérieur de son `select`/`epoll` d'attente) — pas
   forcément à l'intérieur de la coroutine applicative.
2. `asyncio.run()` intercepte cela dans son propre `finally` et appelle
   `_cancel_all_tasks(loop)` : chaque tâche encore en cours — donc la
   coroutine `run_next_work_item()` en train de tourner — reçoit un
   `task.cancel()`, et la boucle est relancée brièvement pour laisser
   cette annulation se propager. **C'est à ce moment précis que le code
   applicatif, à l'intérieur de `await process.communicate()` ou de
   `await asyncio.wait_for(...)`, observe `asyncio.CancelledError` — pas
   `KeyboardInterrupt`.** Le `KeyboardInterrupt` original n'est
   re-levé par `asyncio.run()` vers l'appelant synchrone
   (`OrchestratorEngine.run()`/le frontend) qu'**après** que cette passe
   d'annulation soit terminée.

Le contrat P18-03 initial, formulé comme "capturer `KeyboardInterrupt`
autour de l'`await` du sous-processus", était donc incomplet : à cet
endroit précis (à l'intérieur d'une coroutine), le signal réellement
observable est `asyncio.CancelledError`.

**Contrat cible P18-03**, construit sur ce constat, sans nouveau
mécanisme de recovery :

- **Couche async (à l'intérieur des coroutines)** : le point qui attend
  le sous-processus (`_default_subprocess_runner`, ou l'appelant
  immédiat dans `RalphExecutionEngine.execute()`) intercepte
  `asyncio.CancelledError` explicitement, en plus de
  `asyncio.TimeoutError` — jamais un `except Exception` générique
  (`CancelledError` hérite de `BaseException` depuis Python 3.8+, donc
  un `except Exception` ne l'attraperait de toute façon pas).
  `asyncio.CancelledError` ne doit jamais court-circuiter le nettoyage
  du sous-processus actif : le kill de l'arbre de processus (voir
  ci-dessous) et `mark_interrupted()` doivent être **terminés avant**
  toute re-propagation de l'annulation. Une fois le nettoyage terminé,
  `asyncio.CancelledError` est **re-levée** (jamais avalée, jamais
  transformée silencieusement en un retour "succès" ou en une valeur
  par défaut) — c'est ce qui permet à `asyncio.run()`'s propre logique
  d'annulation de se terminer normalement.
- **Frontière synchrone** : une fois `asyncio.run()` revenu (après sa
  propre passe d'annulation), c'est là — dans `OrchestratorEngine.run()`
  ou plus haut, côté frontend — que `KeyboardInterrupt` lui-même peut
  être observé/traité (message propre, pas de trace Python brute).
  **Aucun handler `SIGINT` global n'est installé dans la bibliothèque**
  (`signal.signal(signal.SIGINT, ...)`) — le comportement par défaut de
  Python/asyncio (décrit ci-dessus) suffit ; un handler global
  changerait un comportement observable par tout appelant de ce
  package, bien au-delà de `OrchestratorEngine.run()`, ce que ce projet
  ne fait déjà nulle part ailleurs.
- l'exécution est finalisée par `mark_interrupted()` — jamais laissée
  `RUNNING`, jamais marquée `SUCCEEDED`/`COMPLETED` ;
- aucune QA n'est lancée, aucun merge n'est tenté, après une
  interruption DEV A/DEV B/DEV FIX (déjà garanti par construction : la
  chaîne d'appel synchrone ne peut pas atteindre QA/merge si l'`await`
  du développement n'est jamais revenu — une `CancelledError`
  re-propagée interrompt cette chaîne exactement comme le ferait toute
  autre exception) ;
- `RecoveryCoordinator` (inchangé) reprend cette exécution
  `INTERRUPTED` exactement comme il le fait déjà pour un timeout Ralph ;
- `run.interruption_requested` puis
  (`dev_a`/`dev_b`/`dev_fix`)`.interrupted` puis `run.interrupted` sont
  émis via `on_event`, dans cet ordre, **avant** que l'annulation ne
  soit re-propagée ;
- `.run()` retourne ou lève de façon propre (jamais une trace Python
  brute) — la forme exacte (retour normal avec un `RunResult` partiel
  vs. exception typée à la frontière synchrone) est un détail
  d'implémentation de P18-03, à trancher sans introduire un second
  système de statut.

Ce qui précède couvre l'interruption pendant une exécution DEV
(`ExecutionStore`) — **une interruption pendant QA elle-même est un cas
distinct, réellement invisible du `RecoveryCoordinator` tel que décrit
ci-dessus** ; voir "Gap réel découvert en construisant P18-03" plus loin
pour le constat exact et son correctif (extension de
`RecoveryCoordinator` à `QARunStore`, jamais un second moteur).

#### Arrêt de l'arbre de processus, pas seulement de `ralph` (précision 2026-09-26)

**Constat** : aujourd'hui, `process.kill()` (appelé uniquement sur
`asyncio.TimeoutError`) tue le seul processus `ralph` directement lancé
par `asyncio.create_subprocess_exec(...)`. Rien ne garantit l'arrêt des
descendants que `ralph` lui-même lance (le CLI provider réel — `claude`/
`codex`/`vibe`/un autre backend) : `process.kill()` sur le PID de
`ralph` seul ne tue pas nécessairement un enfant déjà forké. **Ce projet
ne revendique aujourd'hui aucun support Windows** (aucune mention dans
`README.md`/`CONTRIBUTING.md`/`pyproject.toml`, aucun classifieur
d'OS, `asyncio.create_subprocess_exec` déjà utilisé sans accommodation
Windows) — la stratégie ci-dessous cible donc POSIX (Linux/macOS, les
plateformes réellement utilisées par ce projet) ; un support Windows
resterait `À VOTER`/YAGNI tant qu'un besoin réel n'est pas démontré.

**Stratégie minimale et déterministe (POSIX)**, sans dépendance externe :

- Lancer `ralph` dans son propre groupe de processus/sa propre session
  (`asyncio.create_subprocess_exec(..., start_new_session=True)` —
  paramètre déjà supporté nativement par la stdlib, aucune dépendance
  ajoutée) plutôt que de partager le groupe du processus Python parent.
- À l'interruption (annulation) ou au timeout existant, terminer le
  **groupe entier**, pas seulement le PID direct : `os.killpg(pgid,
  signal.SIGTERM)` (pgid = PID du process, puisqu'il est chef de son
  propre groupe grâce à `start_new_session=True`), puis, après un court
  délai/`wait()` sans succès, `os.killpg(pgid, signal.SIGKILL)` —
  strictement analogue au `process.kill()` déjà utilisé aujourd'hui pour
  `RalphTimeoutError`, étendu au groupe plutôt qu'au seul PID.
- `await process.wait()` (déjà fait aujourd'hui pour le timeout) après
  le/les signaux, pour reaper le process et éviter tout zombie.
- Cette même stratégie remplace le simple `process.kill()` du chemin
  `RalphTimeoutError` existant — un seul mécanisme de terminaison de
  process pour les deux cas (timeout et interruption), jamais deux
  implémentations parallèles.

**Résultat fonctionnel exigé, vérifiable sans lancer un vrai provider** :

- `ralph` est arrêté ;
- tout descendant réellement présent dans le même groupe de processus
  est arrêté avec lui ;
- le process est reaped (`wait()`), aucun zombie ;
- aucun provider ne continue de travailler après le Ctrl+C.

**Limite honnête, non dissimulée** : cette stratégie arrête tout ce qui
reste dans le groupe de processus de `ralph`. Si `ralph`, ou le CLI
provider qu'il lance, détache explicitement un de ses propres enfants
dans un nouveau groupe/une nouvelle session (double fork), ce
sous-descendant échapperait à `killpg` — comportement que ce projet ne
contrôle pas (`ralph`/les CLIs provider sont des binaires externes déjà
traités comme tels ailleurs dans ce code) ; documenté comme limite
connue, jamais présenté comme une garantie absolue. `process.kill()`
seul (sans groupe de processus) ne suffit **pas** à couvrir le cas
normal (un provider lancé comme enfant direct de `ralph`, sans double
fork) — c'est précisément ce que cette stratégie corrige.

#### P18-01 — Public live event transport

**Objectif** : introduire le paramètre `on_event` optionnel sur
`OrchestratorEngine.run()`, filé jusqu'à
`MVPManager.run_next_work_item()`, sans changer un seul comportement
existant quand `on_event=None`.

Dépendances : aucune (première tranche).

Critères d'acceptation :

- `OrchestratorEngine.run(max_cycles=..., on_event:
  Callable[[EngineEvent], None] | None = None)` ; `RunResult` inchangé.
- `EngineEvent` vit dans un module neutre sans dépendance vers
  `engine.py` ni `mvp_manager.py` (ex. `orchestrator.engine_events`),
  ré-exporté par `orchestrator.engine` pour compatibilité — un seul DTO
  public, jamais deux ; aucun nouvel import de `mvp_manager.py` vers
  `orchestrator.engine` (voir "Frontière de couches").
- `on_event` filé, inchangé de forme, jusqu'à
  `MVPManager.run_next_work_item(mvp_id, on_event=...)`.
- `RunResult.events` garde exactement sa forme et son contenu actuels
  (le seul `work_item.<status>` grossier) — P18 ne le transforme jamais
  en flux d'événements fins ; les événements fins n'existent que via
  `on_event` (voir "`RunResult.events` vs `on_event`").
- Au minimum, les événements `work_item.started` et
  `work_item.recovery_required` sont réellement émis (nouveaux — les
  autres `work_item.<status>` existent déjà via `RunResult.events`,
  cette tranche les fait aussi passer par `on_event` en plus du tuple
  retourné, jamais l'un à la place de l'autre).
- Ordre déterministe : chaque événement est émis synchrone, dans
  l'ordre exact où le fait qu'il décrit se produit — jamais réordonné,
  jamais batché.
- Une exception levée par `on_event` se propage immédiatement et telle
  quelle à l'appelant de `.run()` — jamais avalée, jamais convertie ;
  l'état déjà persisté avant cet appel `on_event` n'est jamais modifié
  rétroactivement à cause de cette exception (voir "Sémantique du
  callback `on_event`").
- `on_event=None` : comportement, signature de retour, et
  `RunResult.events` strictement identiques à avant P18 (non-régression
  prouvée par test).

Tests attendus (offline, seams existants) :

- `run()` sans `on_event` : comportement historique inchangé (fixture
  de régression directe sur un test existant), `RunResult.events`
  identique octet pour octet à avant P18 sur un scénario existant.
- `on_event` fourni : reçoit bien `work_item.started` avant tout
  `work_item.<status>` terminal du même WorkItem, en plus (jamais à la
  place) du contenu inchangé de `RunResult.events`.
- `on_event` fourni : reçoit `work_item.recovery_required` quand
  `RecoveryCoordinator` reconcilie une exécution orpheline (fixture
  reprenant `test_recovery.py`).
- `on_event` qui lève : l'exception remonte jusqu'à l'appelant de
  `run()` ; le WorkItem/l'exécution déjà finalisés avant cet appel
  restent finalisés tels quels (pas de "rollback" implicite, pas de
  double exécution au run suivant).
- Ordre déterministe vérifié sur un scénario multi-WorkItems (2+
  WorkItems, plusieurs cycles).
- `import orchestrator.mvp_manager` ne déclenche aucun import
  d'`orchestrator.engine` (test statique/`sys.modules`, garde contre
  toute régression de layering future).

Non-objectifs : aucun événement DEV A/DEV B/DEV FIX/QA/Git fin — c'est
P18-02. Aucune gestion d'interruption — c'est P18-03.

#### P18-02 — Fine-grained events and execution metadata

**Objectif** : émettre, via `on_event`, la granularité DEV A/DEV B/DEV
FIX/QA/Git du catalogue ci-dessus, avec les métadonnées réellement
décidées
(`worker_id`/`worker_display_name`/`provider`/`backend`/`profile_id`/
`model`/`quality_tier`/`reasoning_effort`/`commit_sha`).

Dépendances : P18-01.

Critères d'acceptation :

- `profile_id`/`quality_tier` ne migrent vers aucune nouvelle colonne
  SQLite/`ExecutionRecord` (YAGNI, aucune persistance ajoutée pour la
  seule timeline) : ils sont portés dans l'événement directement depuis
  le contexte réel de sélection du profil (`dev_worker.profile()`
  aujourd'hui, voir "Provenance exacte de chaque champ"), au moment où
  `*.selected` est construit.
- Chaque site d'appel de `_run_development` (DEV A, DEV B, DEV FIX)
  fournit explicitement son label de phase
  (`"dev_a"`/`"dev_b"`/`"dev_fix"`) à l'événement — jamais déduit du
  rôle interne `"developer"`, identique dans les trois cas.
- `*.selected` : émis juste après `_select_dev_worker`, avant tout
  appel `execute()` — porte
  `worker_id`/`worker_display_name`/`provider`/`backend`/`profile_id`/
  `model`/`quality_tier`/`reasoning_effort` (le profil réellement
  résolu aujourd'hui — `dev_worker.profile()` en l'absence d'Adaptive
  Execution, voir ci-dessus).
- `*.started` : émis juste avant `RalphExecutionEngine.execute()`.
- `*.completed`/`*.failed` : émis juste après `execute()`, selon
  `ExecutionStatus.SUCCEEDED`/`FAILED` ; `commit_sha` =
  `git_sha_after` quand connu.
- `qa.started` : émis avant l'appel au moteur QA ;
  `qa.pass`/`qa.fail`/`qa.inconclusive` : émis juste après, un seul des
  trois, selon le verdict réel (`QAVerdict`).
- `git.merge_ready` : émis quand
  `compute_merge_eligibility`/`mark_merge_ready` déclare PASS.
  `git.merge_completed` : émis juste après
  `GitGovernanceService.merge()`, avec `commit_sha` (SHA mergé) et, si
  un tag est créé (`create_tag`), le nom du tag dans `payload`.
- Aucune valeur n'est devinée depuis `worker_id` : toute métadonnée
  vient de `Worker`/`ExecutionRecord`/`ExecutionResult`/
  `GitWorkItemRecord` réellement produits par ce passage.
- Aucun changement à `AdaptiveExecutionSelector`/`adaptive_execution.py` :
  ni branché, ni modifié — P18-02 lit seulement ce que
  `_execute_work_item` décide déjà (profil par défaut du worker, voir
  ci-dessus).
- Aucun changement à `WorkerSelector`/P17.

Tests attendus (offline) :

- `dev_a.selected`/`.started`/`.completed` reçus, dans cet ordre, avec
  `worker_id`/`provider`/`backend`/`profile_id`/`model`/`quality_tier`
  corrects pour un fake worker/profil de test.
- Idem `dev_b.*` (corrective review) et `dev_fix.*` (après un QA FAIL
  réel, fixture reprenant un scénario existant de rework).
- `reasoning_effort` correct pour un worker OpenAI de test
  (`low`/`medium`/`high`) et `None` pour un worker Anthropic/Mistral de
  test dont le profil n'en déclare pas.
- `qa.started` puis exactement un de
  `qa.pass`/`qa.fail`/`qa.inconclusive`, cohérent avec le `QAVerdict`
  fake injecté.
- `git.merge_ready` puis `git.merge_completed` avec le bon
  `commit_sha`, sur un scénario de merge réel offline (fixture Git
  temporaire existante).
- Un champ non résolu (ex. `reasoning_effort` pour un profil qui n'en
  déclare pas) reste `None`, jamais une chaîne vide ni une valeur
  devinée.
- `RunResult`/`ExecutionSnapshot` publics inchangés au-delà de ce que
  P18-01 a déjà changé (pas de régression sur `.status()`).

Non-objectifs : aucune gestion d'interruption (P18-03) ; aucun
branchement Adaptive Execution ; aucune modification de
`WorkerSelector`.

#### Gap réel découvert en construisant P18-03 — recovery pendant QA (corrigé)

**Constat, confirmé empiriquement (2026-09-27)**, avant toute
implémentation de P18-03 : le contrat approuvé supposait qu'une
interruption pendant QA serait reprise « exactement comme pour un
timeout Ralph », via `RecoveryCoordinator`. C'est faux. QA est suivie
dans `QARunStore`, jamais dans `ExecutionStore` — `RecoveryCoordinator`
ne regardait que ce dernier. Séquence réelle, reproduite par un script
direct contre `RecoveryCoordinator.reconcile_work_item` : DEV A/DEV B
réussissent (`ExecutionRecord` `SUCCEEDED`) → WorkItem `RUNNING` → QA
démarre → interruption → `reconcile_work_item` voit `SUCCEEDED`,
retourne `None`, **le WorkItem reste `RUNNING` indéfiniment** — plus
aucun mécanisme existant ne le touche jamais.

**Décision produit** : P18-03 couvre bien Ctrl+C pendant DEV A, DEV B,
DEV FIX **et QA** — ce gap est corrigé dans le mécanisme existant,
jamais dans un second moteur de recovery.

**Correctif — extension de `RecoveryCoordinator` (une seule
coordination, deux familles reconnues)** :

- **Famille A** (inchangée) : `ExecutionStore`, `ExecutionRecord`
  `RUNNING`/`INTERRUPTED`.
- **Famille B** (nouvelle) : quand le dernier `ExecutionRecord`
  développeur est `SUCCEEDED` (famille A n'a plus rien à dire) et que le
  dernier `QARun` de ce WorkItem est `RUNNING` (orphelin) ou déjà
  `INTERRUPTED` — jamais quand un verdict durable existe déjà
  (`COMPLETED`/`FAILED`). Un `QARun` `RUNNING` orphelin est basculé
  `INTERRUPTED` (transition déjà permise par `QARunStatus`, jamais un
  faux verdict PASS/FAIL) ; le WorkItem devient `RECOVERY_REQUIRED`,
  exactement comme pour la famille A. `RecoveryCoordinator` reste
  construit avec `qa_run_store=None` par défaut (opt-in, comme toute
  autre capacité de `MVPManager` — aucun changement pour un appelant qui
  n'active pas QA).

**Correctif — reprise QA seule, DEV jamais rejoué** :
`MVPManager._try_resume_recovery_required` distingue désormais les deux
cas au moment de reprendre un WorkItem `RECOVERY_REQUIRED` : si le
dernier `ExecutionRecord` développeur est `SUCCEEDED`, la reprise passe
par une nouvelle méthode, `_resume_qa_only` — elle lit
`expected_base_sha`/`expected_head_sha` directement sur le `QARun`
interrompu (jamais reconstruits), reconcilie le HEAD gouverné
(`_reconcile_governed_head`, exactement le même invariant que
`_resume_dev_b_wait` utilise déjà — `GitHeadDriftError` fail-closed en
cas de dérive réelle), puis relance `_run_qa_and_finalize` directement.
**DEV A et DEV B ne sont jamais rejoués** — prouvé par test (l'engine de
développement fake n'est jamais appelé). Sinon (dernier développeur
`RUNNING`/`INTERRUPTED`), le chemin de reprise DEV existant, inchangé,
s'applique.

**Correctif — `asyncio.to_thread` ne suffisait pas** : `_run_qa_cycle`
lançait `self._qa_engine.run` via `asyncio.to_thread`.
`InternalQAEngine.run()` (méthode synchrone du Protocol `QAEngine`)
enveloppe elle-même `asyncio.run(self.run_async(...))` — exécutée dans
un thread séparé, cette boucle asyncio interne est totalement hors
d'atteinte d'une annulation de la tâche externe : `to_thread` se
détache simplement du thread, qui continue de tourner jusqu'à sa fin
(vrai sous-processus `pytest`/`ruff`/... compris), avec un risque réel
d'écriture tardive dans `QARunStore`/`ValidationStore` après que le run
principal s'est déjà arrêté. **Correctif retenu (le plus petit
possible, REUSE FIRST)** : `_run_qa_cycle` détecte (duck-typing, comme
`build_plan`/`build_manifest` déjà) un `run_async(request)` sur le
`QAEngine` configuré et l'attend directement, sur la même tâche/boucle
que le reste du `WorkItem Flow` — une vraie annulation atteint alors QA
exactement comme elle atteint DEV A/DEV B/DEV FIX. Le Protocol
`QAEngine` lui-même est inchangé (`.run(request) -> QAResult`) ;
`asyncio.to_thread` reste le repli pour un moteur externe strictement
synchrone. `InternalQAEngine.run_async` existait déjà — aucune
modification de `internal_qa_engine.py` n'a été nécessaire.

**Correctif — arrêt de process réel côté QA** : `QualityGateRunner`
(`validation.py`) avait exactement le même défaut que
`RalphExecutionEngine` (un `process.kill()` sur le seul PID direct au
timeout, rien à l'annulation). Un seul primitive partagé, nouveau
module `orchestrator.posix_subprocess`
(`run_in_new_process_group`), est désormais utilisé par les deux
runners (`ralph_execution_engine.py` et `validation.py`) — jamais deux
implémentations divergentes. Lance la commande dans sa propre
session/groupe de processus POSIX (`start_new_session=True`) ; au
timeout **ou** à l'annulation, `SIGTERM` le groupe entier, escalade
`SIGKILL` après un court délai fixe, puis `wait()` pour reaper — jamais
un simple `process.kill()`. `asyncio.TimeoutError`/
`asyncio.CancelledError` sont gérés par le même code, jamais deux
chemins. Aucune nouvelle dépendance (stdlib uniquement). Limite
assumée, non dissimulée : un descendant qui se détache lui-même
(double fork) échappe au groupe — hors du contrôle de ce projet.

#### P18-03 — Graceful interruption and recovery integration

**Objectif** : rendre l'interruption d'un `run()` actif explicite et
propre pendant DEV A/DEV B/DEV FIX **et QA**, en étendant
`RecoveryCoordinator` (jamais un second moteur de recovery), avec les
événements `run.*`/`*.interrupted`/`qa.interrupted` correspondants.

Dépendances : P18-01, P18-02.

Critères d'acceptation :

- `asyncio.CancelledError` est explicitement intercepté au point qui
  attend le sous-processus (`_default_subprocess_runner`, ou
  l'appelant immédiat dans `RalphExecutionEngine.execute()`), en plus
  du `asyncio.TimeoutError` déjà géré — jamais un `except Exception`
  générique (qui n'attraperait de toute façon pas
  `asyncio.CancelledError`, `BaseException`).
- Le nettoyage (arrêt de l'arbre de processus + `mark_interrupted()` +
  émission des événements d'interruption) est **entièrement terminé
  avant** toute re-propagation de `asyncio.CancelledError` — jamais
  l'inverse.
- `asyncio.CancelledError` est **re-levée** après ce nettoyage, jamais
  avalée, jamais transformée silencieusement en un résultat "succès".
- `KeyboardInterrupt` lui-même n'est observé/géré qu'à la frontière
  synchrone (`OrchestratorEngine.run()` ou au-dessus, après le retour
  de `asyncio.run()`) — jamais capturé à l'intérieur d'une coroutine à
  la place de `asyncio.CancelledError`.
- **Aucun handler `SIGINT` global** (`signal.signal(signal.SIGINT,
  ...)`) n'est installé par cette bibliothèque — le comportement
  d'annulation standard d'`asyncio.run()` (décrit ci-dessus) est jugé
  suffisant.
- Le sous-processus `ralph` est lancé dans son propre groupe de
  processus/sa propre session POSIX
  (`start_new_session=True`) ; à l'interruption **et** au timeout
  existant (chemin `RalphTimeoutError` unifié avec celui-ci — un seul
  mécanisme de terminaison, jamais deux), le **groupe entier** est
  terminé (`os.killpg(pgid, SIGTERM)` puis, sans succès après un court
  délai, `os.killpg(pgid, SIGKILL)`), puis reaped (`await
  process.wait()`) — jamais un simple `process.kill()` sur le seul PID
  direct.
- Aucun support Windows n'est prétendu ou testé par cette tranche
  (aucune revendication de ce type ailleurs dans ce projet) ; `À
  VOTER`/YAGNI si un besoin réel apparaît.
- L'exécution interrompue est finalisée par
  `ExecutionStore.mark_interrupted()` — jamais laissée `RUNNING`,
  jamais `SUCCEEDED`.
- Aucune QA n'est lancée et aucun merge n'est tenté après une
  interruption DEV A/DEV B/DEV FIX (prouvé par test, pas seulement par
  construction) ; aucun merge n'est tenté après une interruption QA
  elle-même.
- `_run_qa_cycle` préfère un `run_async(request)` réellement exposé par
  le `QAEngine` configuré (duck-typing, `InternalQAEngine` en
  production) sur `asyncio.to_thread(self._qa_engine.run, ...)` — le
  Protocol `QAEngine` (`.run(request) -> QAResult`) reste inchangé,
  `to_thread` reste le repli pour un moteur externe strictement
  synchrone.
- `RecoveryCoordinator` est **étendu** (jamais un second moteur) avec
  une seconde famille reconnue, opt-in via `qa_run_store=` : un dernier
  `ExecutionRecord` développeur `SUCCEEDED` combiné à un dernier `QARun`
  `RUNNING` (orphelin, basculé `INTERRUPTED`, jamais un faux verdict) ou
  déjà `INTERRUPTED` bascule le WorkItem en `RECOVERY_REQUIRED` — sans
  toucher à la famille A existante (`ExecutionStore`).
- Le prochain `run()` reprend via `_try_resume_recovery_required`
  (étendu, jamais un second mécanisme) : si le dernier développeur est
  `SUCCEEDED`, `_resume_qa_only` relance uniquement une nouvelle QA
  (lisant `expected_base_sha`/`expected_head_sha` du `QARun` interrompu,
  reconciliant le HEAD gouverné, fail-closed sur dérive réelle) —
  **DEV A/DEV B ne sont jamais rejoués**, prouvé par test. Sinon, le
  chemin de reprise DEV existant, inchangé, s'applique.
- `run.interruption_requested` est émis dès la capture de
  `asyncio.CancelledError` (côté DEV comme côté QA) ;
  `(dev_a|dev_b|dev_fix).interrupted` ou `qa.interrupted` est émis pour
  l'exécution/l'attempt concerné ; `run.interrupted` est émis juste
  avant la re-propagation de l'annulation — tous les trois via
  `on_event`, avant que `.run()` ne retourne/lève à la frontière
  synchrone.
- `.run()` ne laisse jamais fuiter une trace Python brute pour ce cas —
  géré au même niveau que `EngineError`/`EngineConfigError` existants
  (jamais un troisième type d'erreur non documenté).
- `aido resume`/`/resume` (frontend, M2) restent hors de portée : P18
  ne touche à rien côté session AIDO Code.

Tests attendus (offline, fakes capables de simuler une annulation
mi-exécution — jamais un vrai `ralph`/provider) — **tous réellement
écrits et verts** :

- `tests/test_ralph_execution_engine.py` : `execute()` annulé
  (`subprocess_runner` fake levant `CancelledError`) finalise
  `INTERRUPTED` et **re-lève**, ne retourne jamais un `ExecutionResult`.
- `tests/test_mvp_manager_workitem_flow.py::TestDevPhaseInterruption` :
  annulation pendant DEV A et pendant DEV B — WorkItem jamais
  `COMPLETED`/`FAILED`, QA jamais appelée, événements
  `run.interruption_requested`/`{phase}.interrupted`/`run.interrupted`
  reçus dans cet ordre.
- `tests/test_mvp_manager_workitem_flow.py::TestQAPhaseInterruption` :
  annulation pendant une QA réellement en vol (`asyncio.Event`
  synchronise le test avec l'attempt réel, jamais un sleep arbitraire) —
  `QARun` finalisé `INTERRUPTED` (`verdict is None`), WorkItem jamais
  `COMPLETED`, aucun `git.merge_*`, `.run()` (synchrone) n'est jamais
  appelée (preuve que le chemin async réel est utilisé, jamais
  `to_thread`).
- `tests/test_recovery.py::TestQARecovery` : `QARun` `RUNNING` orphelin
  → `INTERRUPTED` + WorkItem `RECOVERY_REQUIRED` ; `QARun` déjà
  `INTERRUPTED` → inchangé, WorkItem quand même `RECOVERY_REQUIRED` ;
  `QARun` `COMPLETED` → jamais touché ; sans `qa_run_store` configuré →
  jamais déclenché ; aucun `QARun` du tout → laissé tel quel.
- `tests/test_mvp_manager_workitem_flow.py::TestQAOnlyResumeAfterInterruption` :
  reprise après un `QARun` `INTERRUPTED` — un nouveau `QARun` est créé,
  PASS → merge normal, **l'engine de développement fake n'est jamais
  appelé** (`dev_engine.requests == []`) ; HEAD dérivé depuis
  l'interruption → `GitHeadDriftError`, aucune QA tentée, aucun merge.
- `tests/test_posix_subprocess.py` : un vrai sous-processus (`sh`/
  `sleep`, jamais un provider) lançant lui-même un vrai enfant — timeout
  et annulation tuent le **groupe entier** (l'enfant réellement vérifié
  mort via son PID), reaping sans zombie ; l'annulation n'est jamais
  observée comme un succès.

Non-objectifs : aucun second système de recovery (une seule
`RecoveryCoordinator`, deux familles) ; aucun changement à la taxonomie
existante d'`ExecutionStore`/`QARunStore`
(`INTERRUPTED`/`RECOVERY_REQUIRED` déjà là) ; aucun checkpoint intra-QA,
aucun replay d'un `QARun` interrompu au niveau commande individuelle
(YAGNI — un `QARun` interrompu est abandonné, une QA neuve et complète
est relancée) ; aucune UI/session AIDO Code ; aucun changement à
`WorkerSelector`/P17 ; aucun serveur/queue/broker.

#### Hors périmètre de P18 (l'ensemble des trois WorkItems)

- UI AIDO Code, conversation, sessions M3 (frontend, hors de ce dépôt).
- P14 (observabilité de consommation/coûts).
- Branchement réel d'Adaptive Execution en production (décision produit
  séparée).
- Tout changement à `WorkerSelector`/P17.
- WebSocket, serveur HTTP, daemon, broker, Redis/Kafka, toute nouvelle
  base de données.
- Refonte générale de `MVPManager`.
- Streaming JSON public (M5, côté AIDO Code).
- Parsing Git/stdout par un consommateur — la seule API publique est
  `on_event`/`EngineEvent`.

**Statut** : `APPROUVÉ` (GO humain 2026-09-26) — **`IN PROGRESS`**.
**P18-01 `DONE`** (2026-09-26) : `EngineEvent` déménagé dans le module
neutre `orchestrator.engine_events` (ré-exporté, inchangé, par
`orchestrator.engine`) ; `OrchestratorEngine.run(on_event=...)` filé
jusqu'à `MVPManager.run_next_work_item(mvp_id, on_event=...)`,
`_execute_work_item`, `_try_resume_due_wait`,
`_try_resume_recovery_required` ; `work_item.started` (après
`mark_work_item_running`) et `work_item.recovery_required` (après
reconciliation) réellement émis en direct ; `RunResult.events` inchangé
(toujours le seul `work_item.<status>` grossier) ; 1332 tests (1325 +
7 nouveaux), `git diff --check` propre.
**P18-02 `DONE`** (2026-09-26) : `EngineEvent` étendu
(`execution_id`/`phase`/`status`/`worker_id`/`worker_display_name`/
`provider`/`backend`/`profile_id`/`model`/`quality_tier`/
`reasoning_effort`/`commit_sha`, tous optionnels, `None` par défaut) ;
`_run_development` (partagée par DEV A/DEV B/DEV FIX, labellisée par
site d'appel via un nouveau paramètre `phase`) émet
`{phase}.selected/started/completed/failed` — et `.interrupted` en
réutilisant honnêtement le statut `INTERRUPTED` déjà réel de
`RalphExecutionEngine` (timeout), sans attendre P18-03 ; `profile_id`/
`quality_tier` proviennent de `Worker.profile()` résolu au moment de la
sélection (Adaptive Execution toujours non branché) ; `model`/
`reasoning_effort` viennent de l'exécution réellement lancée.
`_run_qa_and_finalize` émet `qa.started` puis exactement un de
`qa.pass`/`qa.fail`/`qa.inconclusive` (verdict `None` mappé
honnêtement sur `qa.inconclusive`, jamais PASS fabriqué) ; `git.
merge_ready`/`git.merge_completed` (avec SHA + tag) émis d'après l'état
réel de `GitGovernanceService` après coup — `git.merge_started` reste
volontairement absent (pas de fait intermédiaire réel, voir la
relecture de code plus haut). `RunResult.events` toujours inchangé.
1335 tests (1332 + 3 nouveaux), `git diff --check` propre.
**P18-03 : `DONE`** — voir la sous-section P18 ci-dessus pour le détail
complet (extension de `RecoveryCoordinator` à la QA, `posix_subprocess`
partagé Ralph/QA, `_resume_qa_only`) ; 1350 tests au SHA `23e68b7`.

### P19 — Gravity worker/backend — `APPROUVÉ` (GO humain 2026-09-28)

**Statut** : `APPROUVÉ` (GO humain, 2026-09-28) — reste minimal : pas de
framework plugin, pas de refactor provider général, pas de second
moteur d'exécution. Cette sous-section documente le contrat que
l'implémentation réelle (branche `feature/p19-gravity-worker-backend`)
suit. **P19 n'est en aucun cas une dépendance implicite d'AIDO Code
M3** : M3 est développé et exécutable avec le pool de workers actuel
(`config/workers.yaml`), sans Gravity — inversement, l'approbation de
P19 n'approuve rien de plus que ce que cette sous-section décrit.

**Vérification réelle `agy` (Phase A, 2026-09-28, binaire déjà installé
et authentifié sur la machine, aucun onboarding effectué ici)** :
binaire `/home/jarvis/.local/bin/agy`, version `1.2.12`. `agy --help`
confirme réellement `--mode` (valeurs `accept-edits`/`plan`),
`--dangerously-skip-permissions`, `-p`/`--print`/`--prompt` (prompt
non interactif, texte littéral, jamais un chemin de fichier),
`--output-format text/json/stream-json`, `--model`, `--effort`
(`low|medium|high|max`). Testé réellement dans un dépôt Git temporaire
jetable (hors des deux projets, supprimé après) :
`agy -p "<texte>" --mode=accept-edits` (**sans**
`--dangerously-skip-permissions`) crée/modifie bien un fichier de façon
purement non interactive, exit code `0`, aucun prompt bloquant —
**STANDARD ne nécessite donc pas de bypass total**, confirmé avant tout
code. `agy models` liste un catalogue de modèles réels et sélectionnables
(`claude-sonnet-4-6`, `claude-opus-4-6-thinking`, la famille
`gemini-3.x-flash-*`, `gpt-oss-120b-medium`, entre autres) ; `--model
claude-sonnet-4-6` vérifié fonctionnel (exit `0`), un id invalide
échoue proprement (`status":"ERROR"`, exit `1`, jamais un crash). `
--effort` existe mais est refusé par `agy` pour `claude-sonnet-4-6`
(« `--effort is not supported for model "claude-sonnet-4-6"` ») — donc
aucun `reasoning_effort` n'est configuré pour ce profil, honnêtement,
plutôt que fabriqué. `--output-format json` ne fournit ni `model` ni
`quota`/`rate_limit` dans sa réponse (`conversation_id`, `status`,
`response`, `duration_seconds`, `num_turns`, `usage{tokens...}`
seulement) — confirme qu'aucune télémétrie de quota n'est observable,
exactement le constat qui justifie `EXECUTION_PROBE_ONLY` pour
`MistralVibeAdapter`.

**Contrat de permission retenu (Phase A)** :

```
STANDARD:     agy -p "<prompt>" --mode=accept-edits
UNRESTRICTED: agy -p "<prompt>" --mode=accept-edits --dangerously-skip-permissions
```

jamais l'inverse, jamais `--dangerously-skip-permissions` hors du mode
`UNRESTRICTED` explicite.

**Objectif** : ajouter un worker `gravity`, exécutable via Ralph, en
réutilisant le mécanisme « custom backend » solo déjà employé par Vibe
(`orchestrator.vibe_ralph_bridge` — voir §13, note P13 et
`docs/VIBE_SPIKE.md`) — jamais un second moteur d'exécution. Un seul
worker au départ ; aucun doublon/fallback Gravity sans preuve de besoin
réelle (REUSE FIRST/YAGNI).

**Configuration cible** (`config/workers.yaml`, sur le modèle exact de
l'entrée `milo`/`juno` déjà existante pour Vibe) :

```yaml
- worker_id: gravity
  display_name: Gravity
  enabled: true   # décision humaine explicite ; gate séparément pour AIDO Code : P19-03
  provider: gravity
  backend: gravity
  priority: 101   # légèrement au-dessus des autres (100) : augmente
                   # réellement la bande passante DEV A sans toucher
                   # WorkerSelector/P17 — quota pressure -> priority ->
                   # worker_id reste l'unique règle de tri
  capabilities:
    - development
  default_profile_id: standard
  estimator_profile_id: standard
  profiles:
    standard:
      quality_tier: STANDARD
      model: claude-sonnet-4-6   # id réel, vérifié via `agy models`/
                                  # `agy --model claude-sonnet-4-6`
                                  # (Phase A) — jamais un sentinel
                                  # inventé puisqu'un id réel existe
      cost_rank: 20
```

Un seul profil, aucun `reasoning_effort` : `--effort` existe côté `agy`
mais `claude-sonnet-4-6` le refuse explicitement (vérifié en Phase A) —
absent plutôt que fabriqué.

**Commande utilisateur, syntaxe finale vérifiée (Phase A)** :

```
STANDARD:     agy -p "<prompt>" --mode=accept-edits
UNRESTRICTED: agy -p "<prompt>" --mode=accept-edits --dangerously-skip-permissions
```

**Permissions — réutilisation stricte de `ExecutionPermissionMode`
existant (§13, P14 « Mode de permission d'exécution des workers »),
jamais un bypass hardcodé indépendant de cette politique** :

- **`UNRESTRICTED`** : traduit vers
  `agy -p "<prompt>" --mode=accept-edits --dangerously-skip-permissions`
  — gatée sur ce mode explicite, jamais un défaut silencieux (même
  invariant que `vibe_ralph_bridge.permission_args` pour
  `unrestricted` → `--auto-approve`).
- **`STANDARD`** : traduit vers `agy -p "<prompt>" --mode=accept-edits`
  (sans `--dangerously-skip-permissions`) — **vérifié réellement non
  bloquant en Phase A** (exit `0`, fichier créé, zéro prompt), donc
  jamais un bypass. Si un futur `agy` cessait d'honorer ce comportement,
  le bridge fail-closerait (`UnsupportedPermissionModeError`, même type
  que `vibe_ralph_bridge.BridgeArgError`) plutôt que de dégrader
  silencieusement vers un bypass.

#### P19-01 — Gravity/agy real spike (`DONE`, Phase A, 2026-09-28)

Kind: research spike — résultats consignés directement ci-dessus (pas
de fichier `docs/GRAVITY_SPIKE.md` séparé : la vérification tient dans
ce paragraphe, REUSE FIRST/KISS — un document dédié n'apporterait rien
de plus).

Acceptance :

- `command -v agy` (présence réelle du binaire) vérifié.
- Version `agy` observée et consignée.
- `agy --help` lu intégralement et consigné — jamais deviné.
- Syntaxe exacte des flags demandés (`--mode=accept-edits`,
  `--dangerously-skip-permissions`) vérifiée réellement contre le CLI
  installé, jamais supposée depuis un nom qui semble correct.
- Comportement *unattended* réel observé (un run sans TTY/sans
  utilisateur pour répondre à un prompt réussit-il, échoue-t-il
  proprement, ou reste-t-il bloqué ?).
- Test contrôlé d'édition réelle dans un dépôt Git temporaire jetable
  (jamais sur ce dépôt ni un projet réel).
- Identifie précisément comment Ralph transmet le prompt/fichier à
  `agy` (argv, stdin, fichier — sur le modèle de la découverte réelle
  documentée par `vibe_ralph_bridge.py` : chemin embarqué en fin de
  phrase, jamais un simple argument brut).
- Aucune intégration permanente (`orchestrator/providers/`,
  `config/workers.yaml` avec `enabled: true`) avant cette preuve.

#### P19-02 — Gravity provider/backend integration

Dépend de : P19-01 (preuve réelle requise avant tout code).

Acceptance :

- Adaptateur provider `gravity`, sur le modèle minimal des adaptateurs
  CLI existants (`orchestrator/providers/adapter.py`,
  `ProviderAdapter`) — jamais une structure parallèle inventée.
- Disponibilité réelle uniquement, jamais un quota inventé : `agy
  --output-format json` n'expose (confirmé en Phase A) ni `model` ni
  télémétrie de quota/rate-limit — exactement le constat qui a produit
  `EXECUTION_PROBE_ONLY` pour `MistralVibeAdapter`
  (`orchestrator/providers/mistral_vibe_adapter.py`) ; l'adaptateur
  Gravity suit donc le même schéma `EXECUTION_PROBE_ONLY` (un appel
  minimal, borné, réel comme seul signal honnête, `quota_windows=()`)
  — jamais un chiffre de quota fabriqué.
- Backend Ralph « custom » réutilisant exactement la mécanique déjà
  employée par Vibe (`cli.backend: "custom"` du mode solo de Ralph,
  jamais un second point d'entrée d'exécution).
- Un bridge minimal (`gravity_ralph_bridge.py`, sur le modèle de
  `vibe_ralph_bridge.py`) — le protocole argv réel de Ralph livre le
  prompt en phrase avec un chemin de fichier embarqué en fin de phrase
  (constat déjà vérifié pour ce même mécanisme Ralph par le bridge
  Vibe, indépendant du CLI cible) ; le bridge Gravity lit ce fichier
  puis appelle `agy -p "<contenu>" ...` — `agy` lui-même n'accepte que
  du texte littéral via `-p`, jamais un chemin.
- Aucun second moteur d'exécution ; `RalphExecutionEngine` reste
  l'unique point d'exécution.
- Mode de permission respecté exactement comme spécifié ci-dessus
  (`UNRESTRICTED`/`STANDARD`/`UnsupportedPermissionModeError`).
- Réutilise le primitive `orchestrator.posix_subprocess.
  run_in_new_process_group` (P18-03) pour l'arrêt de groupe de
  processus et la gestion d'annulation — jamais une seconde
  implémentation de cleanup.
- Worker `gravity` ajouté au registry moteur (`config/workers.yaml`)
  avec `enabled: true` (décision humaine explicite) — ce registry n'est
  de toute façon jamais ce qu'AIDO Code lit (`docs/PROJECT_CONTRACT.md`
  §4) ; P19-03 gate séparément l'ajout de Gravity à l'override local
  d'AIDO Code lui-même.
- Capacité déclarée : `development` seulement, tant qu'aucune autre
  capacité réelle n'est prouvée par un run réel.

#### P19-03 — Real validation (`DONE`, 2026-09-28)

Dépend de : P19-02.

Acceptance :

- Run réel via Ralph + Gravity, sur un dépôt de test jetable.
- Modification contrôlée réellement produite et committée.
- Événement de terminaison Ralph reconnu (mécanisme
  `.ralph/events-*.jsonl` déjà partagé par tous les backends, jamais un
  second parseur d'événements).
- `ExecutionRecord` correctement peuplé (worker, provider, backend,
  modèle, statut, SHA avant/après).
- Commit réellement attribué à l'identité du worker Gravity (même
  invariant que P13.2 pour tout worker).
- Un DEV peut réussir de bout en bout avec Gravity.
- Nettoyage à l'interruption vérifié (P18-03 : arrêt de groupe de
  processus réel, `ExecutionRecord` finalisé `INTERRUPTED`, jamais
  `RUNNING` orphelin).
- `provider unavailable` propre et honnête si `agy` est absent ou non
  authentifié (jamais une exception non gérée, jamais un faux
  `available=True`).
- La suite de tests offline complète reste entièrement verte — aucun
  test n'appelle `agy` réellement.
- `enabled: true` n'est décidé qu'après cette preuve réelle complète —
  jamais avant, jamais par anticipation.

**Résultat réel (2026-09-28)**, `RalphExecutionEngine` réel (jamais de
subprocess_runner factice), worker `gravity`/`claude-sonnet-4-6`, dans
un dépôt Git jetable hors des deux projets :

- **Run nominal** : `ExecutionStatus.SUCCEEDED`, `provider=gravity`,
  `backend=gravity`, `model=claude-sonnet-4-6`. Fichier créé avec le
  contenu exact demandé. Commit réel produit
  (`git_sha_before` ≠ `git_sha_after`), auteur Git
  `Gravity <gravity_spike_01@workers.ai-dev-orchestrator.local>` —
  conforme à P13.2, jamais un nom de vendor. `.ralph/events-*.jsonl`
  contient bien l'événement `work.completed` réel émis par `agy` via
  `ralph emit` (mécanisme déjà partagé, aucun second parseur).
- **Interruption** : tâche annulée après 8s (`asyncio` `task.cancel()`)
  pendant un run réel plus long. `ExecutionRecord` correctement
  finalisé `INTERRUPTED` (jamais laissé `RUNNING`) — ce point précis de
  l'acceptance est bien vérifié. **Mais** : un descendant du process
  Ralph (`gravity_ralph_bridge.py`, puis `agy` lui-même) a survécu à
  l'annulation, observé réellement vivant (`ps`) plusieurs secondes
  après le retour de `engine.execute()`, PPID réattribué (reparenté),
  PGID distinct de celui du groupe lancé par
  `run_in_new_process_group`. **Ce n'est pas une régression P19** :
  c'est exactement la limite déjà documentée, non dissimulée, dans
  `posix_subprocess.py` depuis P18-03 (« a descendant that itself
  detaches into a new session/group (a double fork) is outside this
  group and outside this project's control ») — `ralph` (binaire
  externe) détache visiblement son propre enfant custom-backend dans un
  groupe/session distinct, exactement le cas déjà anticipé. Le même
  mécanisme partagé (`_SOLO_MODE_BACKENDS`) s'applique identiquement à
  Vibe ; rien n'indique que Vibe échapperait à la même limite si
  soumis au même test réel (jamais vérifié en conditions réelles
  avant aujourd'hui pour aucun backend solo-mode — la suite P18-03
  reste entièrement offline). Processus orphelins nettoyés
  manuellement après constat (`kill`), aucun run réel laissé actif.
  **Conséquence opérationnelle à connaître** : interrompre une tâche
  Gravity (ou Vibe) en cours peut laisser un appel `agy`/`vibe` réel
  continuer en arrière-plan jusqu'à sa propre fin naturelle — coût réel
  possible au-delà de l'annulation logique. Corriger cette limite
  (si jamais possible côté `ralph` lui-même) est hors du périmètre de
  P19 ; documenté ici pour que ce ne soit plus un angle mort.
- Suite de tests offline complète : verte (voir commit d'implémentation
  ci-dessus), aucun test n'appelle `agy` réellement.
- Décision : `enabled: true` maintenu (déjà positionné en P19-02) — le
  run nominal réel est un succès complet et sans réserve ; la réserve
  ci-dessus concerne une limite pré-existante et déjà acceptée de
  l'annulation, pas le fonctionnement de Gravity lui-même.

### P20 — Keep only validated providers and normalize Gravity workers — `DONE`

**Statut** : `DONE` (GO humain donné) — PR #32 mergée, SHA final `bac2216`,
1374 tests PASS (`pytest -q`).

**Résultat attendu** :
- DeepSeek/Kimi retirés (adapters, tests, workers `dana`/`kai`, factories
  runtime) ;
- 8 workers, 4 providers validés (anthropic, openai, mistral, gravity) ;
- Gravity : `gravity_primary` (Arthur, priorité 101) et `gravity_secondary`
  (Nora, priorité 91) — `gravity` est un provider/backend, jamais un
  `display_name` ; mêmes capacités (`development`), profil `standard`
  (`claude-sonnet-4-6`, `STANDARD`, `cost_rank: 20`, sans
  `reasoning_effort`) ; les deux partagent un seul `ProviderState` ;
- quota Gravity réel via `agy -p "/usage" --output-format json`
  (`num_turns: 0`, aucun tour modèle) : `remaining_fraction` ->
  `utilization = 1 - remaining_fraction`, `reset_time` -> `reset_at`,
  `id`/`window` -> `window_type` ; champ absent => `None`, JSON
  inattendu => échec propre ; aucun quota inventé ;
- aucun changement de `WorkerSelector` ni de `QuotaManager`.

### P21 — Live worker execution observability — `DONE` (2026-10-06)

**Décision produit** : après l'échec terminal de WI-M3-01 dans AIDO Code
(DEV A, Ralph `max_iterations` après 5 itérations, code 2, aucun événement
`work.completed`/`work.failed`, aucun commit), fournir les faits qui ont
manqué au diagnostic et le flux opérationnel nécessaire à une interface
`aido run` réellement live. Cet incident ne prouve pas une panne Gravity.
AIDO Code M3 reste partiel ; ses WorkItems terminaux ne sont pas rouverts.
P21 a été livré par le WorkItem Flow moteur (WI-P21-01/02/03/04), PR #36,
avec CI Python 3.10 et 3.12 vertes. Un nouveau jalon AIDO Code M3.1
pourra réutiliser le routeur M3-03 déjà livré et rendre le flux public ;
aucun code M3.1 n'a été écrit dans P21.

**Frontière et réutilisation** : `provider CLI → Ralph →
RalphExecutionEngine → MVPManager → OrchestratorEngine.run(on_event=...) →
frontend`. Le moteur reste seul propriétaire des observations. Le
frontend ne lit ni stdout Ralph, ni Git, ni SQLite pour reconstruire une
timeline ; il applique son sanitizer terminal existant aux textes reçus.
Il n'affiche aucun bloc de raisonnement privé : si un backend mélange
raisonnement et sortie opérationnelle sans séparation fiable, afficher
seulement les événements sûrs (transitions/heartbeat/diagnostic), et
signaler la limite d'observabilité de ce backend.
Le contrat est identique pour tous les providers/backends : une sortie
n'autorise aucune inférence sur les outils, commandes, tests ou commits
si leur CLI ne les expose pas réellement. Aucun raisonnement interne
(chain-of-thought) n'est demandé ou présenté comme un fait.

**Contrat de capture** : étendre le mécanisme POSIX partagé
`run_in_new_process_group` pour drainer stdout et stderr simultanément,
progressivement, par lignes ou chunks bornés, y compris le fragment
final sans newline. Préserver l'ordre **dans chaque stream**, sans
fabriquer un ordre global entre streams ; éviter les deadlocks et un
événement par caractère. La capture finale `ExecutionResult.stdout`/
`stderr` garde la borne et la sémantique existantes ; le flux live doit
également avoir une politique de taille/fréquence bornée, avec
troncature signalée, sans stockage de transcript ni dump d'environnement.
Timeout, `CancelledError`, arrêt et reap du groupe POSIX restent ceux de
P18-03 ; un descendant détaché dans une autre session demeure la limite
documentée, hors scope. `on_event=None` garde le comportement historique.

**Événements publics** : réutiliser l'unique DTO `EngineEvent` et le
callback P18. `execution.output` porte l'horodatage, les IDs projet/MVP/
WorkItem/exécution, la phase (`dev_a`, `dev_b`, `dev_fix`), le worker et
son provider/backend, ainsi que `stream` (`stdout`/`stderr`) et `text`
réels. `profile_id`/`model` restent les valeurs sélectionnées par le
moteur, jamais déduites du texte. Aucun parser par provider ni nom d'outil
fabriqué. Un éventuel `execution.heartbeat` signifie seulement « encore
en cours » et porte une durée écoulée réelle ; cadence raisonnable et
bornée, sans activité inventée. Les événements P18 DEV/QA/Git existants
continuent à décrire sélection, transition, verdict et SHA réels.

**Callback et gouvernance** : une erreur de présentation lors de la
livraison d'un chunk ou heartbeat ne tue jamais le worker ; le moteur
isole ce callback d'observation et garde une indication bornée de l'échec
de livraison. Le contrat P18 pour les callbacks de transition reste
explicite : leur exception se propage ; le futur frontend doit protéger
son propre renderer. Tester les deux cas et ne jamais convertir une
erreur d'affichage en verdict worker, QA ou Git. Aucun changement de
sélection/priorité des workers, de policy, de QA ou de workflow Git.

**Diagnostic final** : lorsqu'aucun événement métier terminal n'existe,
le verdict reste `FAILED`, jamais déduit du code de sortie. Exposer via
une surface publique typée (résultat final et, pour les seuls faits
durables disponibles, snapshot de statut) la phase, l'identité de la
dernière exécution, modèle, code de sortie, verdict métier
`absent|completed|failed`, raison Ralph seulement si effectivement
lisible dans `loop.terminate`, dernier extrait utile borné disponible
pendant le run, et prochaine action déterministe. Ne pas reconstituer
après redémarrage une sortie non persistée. Si le payload Ralph est
inconnu, afficher « raison inconnue », jamais attribuer la faute au
provider. Exemple fidèle à WI-M3-01 : « Ralph terminated with
max_iterations after 5 iterations; no work.completed/work.failed event;
exit_code=2. » La prochaine action proposée dans ce cas est d'inspecter
la sortie observée et de décider d'un nouveau WorkItem gouverné, jamais
de relancer automatiquement le WorkItem terminal. Le résultat `aido run`
sans affichage live doit recevoir ces faits par l'API moteur, jamais en
lisant ses stores internes.

**WorkItems livrés par le WorkItem Flow P21** :

1. **WI-P21-01 — Progressive subprocess output** : callback optionnel
   dans la primitive POSIX partagée et `RalphExecutionEngine`, capture
   stdout/stderr concurrente et bornée, fragment final, ordre par stream,
   sortie finale préservée, timeout/cancellation/process group identiques.
   Tests offline avec scripts locaux seulement, y compris callback absent
   et callback d'observation qui lève.
2. **WI-P21-02 — Public output events and failure diagnostics** (dépend
   de 01) : filage générique vers `EngineEvent` via MVPManager/Engine,
   identité/phase réelles, résultat final typé pour FAILED, verdict
   absent et `loop.terminate` avec code de sortie. Tests de propagation
   bout en bout, callback de présentation défaillant, sortie inconnue,
   et non-régression du verdict métier.
3. **WI-P21-03 — Heartbeat and acceptance** (dépend de 01 et 02) :
   heartbeat purement temporel si silence, tests offline du silence et
   de la reprise de sortie, suite moteur complète. Après streaming,
   spike manuel **jetable** Ralph+Gravity pour voir la sortie réellement
   exposée par Arthur ; aucune conclusion anticipée ni action sur le
   `WorkerSelector`. Si le heartbeat complexifie 01/02, le garder dans
   ce WorkItem séparé.

   *Livré (WI-P21-03)* : `execution.heartbeat` (payload
   `{"elapsed_seconds"}` seul, identité réelle de l'exécution), émis par
   `run_in_new_process_group(on_heartbeat=, heartbeat_interval=)` après
   15 s de silence stdout/stderr (puis un par intervalle silencieux ; toute
   sortie remet le compteur à zéro). Purement temporel : aucun message de
   progression, aucun verdict ; un callback défaillant est compté dans
   `output_delivery_failures`, jamais fatal.
   *Acceptation manuelle (à exécuter **après merge**, processus neuf, dépôt
   Gravity jetable, jamais dans pytest)* : lancer un run réel via
   `OrchestratorEngine.run(on_event=print)` sur un WorkItem jetable ;
   constater `execution.output` pendant l'exécution, `execution.heartbeat`
   pendant un silence prolongé, puis la reprise de sortie ; noter ce
   qu'Arthur expose réellement, sans conclusion anticipée.
4. **WI-P21-04 — Bound live output delivery** (dépend de 01, 02 et 03) :
   revue d'acceptance des WorkItems précédents : les chunks individuels
   sont bornés, mais un worker très bavard peut encore émettre un nombre
   illimité d'événements `execution.output`. Appliquer au flux public une
   limite simple de volume et de fréquence par exécution, avec un signal
   explicite de troncature. Continuer à drainer stdout/stderr et conserver
   les captures finales ainsi que le dernier extrait diagnostique ; ne
   jamais ralentir ou tuer le worker à cause de l'affichage. Tester hors
   ligne le débordement, le signal unique de troncature et l'absence de
   régression sur un flux normal. Aucun parser ou traitement propre à un
   provider.

**Gates P21** : tests offline stdout/stderr séparés et simultanés,
progression avant fin de processus, ordre intra-stream, dernière ligne,
captures et flux bornés, timeout, annulation et nettoyage du groupe,
callback absent/défaillant, absence de verdict métier, raison
`max_iterations` quand réellement présente, et `execution.output` livré
jusqu'à `OrchestratorEngine` ; régression QA/P18 vérifiée, puisque la
primitive POSIX est partagée. Zéro appel réel provider dans pytest. Spike
Gravity réel uniquement après livraison du streaming, en dépôt jetable.
**Acceptance réelle après merge** (processus Python neuf, dépôt Git
jetable, `OrchestratorEngine.run(on_event=...)`, Arthur/Gravity) : 62
`execution.output` sur stdout pendant DEV A, aucun sur stderr ; deux
`execution.heartbeat` pendant un silence, puis reprise de la sortie.
Le flux brut n'a pas fourni de preuve classable de commandes, outils ou
tests : ne pas en inventer dans l'interface. Ralph a émis cinq
`iteration.summary` puis `loop.terminate` avec `max_iterations` ; aucun
événement métier terminal, `exit_code=2`, aucun commit, dépôt jetable
propre et aucun processus Ralph/Gravity restant. `RunResult.diagnostics`
a rendu ces faits sans attribuer la cause à Gravity. Cette exécution
n'est pas une preuve de réussite fonctionnelle du worker Gravity ; elle
valide le transport live et le diagnostic P21 dans le cas observé.
Tests offline : 1413 PASS (un avertissement de collecte préexistant) ;
CI Python 3.10/3.12 PASS. Limite P18 inchangée : un descendant détaché
dans une autre session ne relève pas du nettoyage du groupe POSIX.
**Suite constatée** : AIDO Code M3.1 a livré ses trois nouveaux
WorkItems, mais son acceptance live reste partielle : une sortie brute
Claude a exposé un bloc structuré de raisonnement privé. Voir P21.1.

### P21.1 — Safe public execution output — `APPROUVÉ` (GO humain 2026-10-06)

**Défaut observé** : pendant le run gouverné AIDO Code M3.1, un bloc
Claude `thinking` présent dans une ligne JSON structurée a été affiché
par le frontend. Le contenu privé n'est reproduit dans aucun document.
La suite moteur de référence reste à 1413 PASS ; AIDO Code à 311 PASS.
P21 reste livré comme transport live, mais la sûreté de son contenu
public n'est pas prouvée. M3.1 reste `PARTIAL` côté produit.

**Chemin vérifié** : `ralph_execution_engine._build_ralph_args()` lance
`ralph run -a -q`. Pour Claude, Ralph 2.10.1 utilise `CliExecutor` et
`claude --print --verbose --output-format stream-json` ; sa branche
stdout recopie chaque ligne JSON brute même avec `-q`. Le moteur draine
ces lignes dans `posix_subprocess._drain()`, puis les transmet par
`ExecutionRequest.output_observer` à `MVPManager.observe_output()`, qui
les émet telles quelles en `EngineEvent(execution.output)`. Le rendu AIDO
Code ne classe pas le contenu. Sur échec, `RalphExecutionEngine` remplit
`ExecutionResult.last_output` depuis le stdout/stderr brut, et
`_build_failure_diagnostic()` choisit cet extrait avant le dernier texte
observé : la fuite peut donc réapparaître dans `FailureDiagnostic`.

**Décision d'architecture : C, filtre structuré minimal à la frontière
moteur/backend.** Le contrat est approuvé ; l'implémentation doit passer
par le WorkItem Flow. Ordre de réutilisation audité :

- **A indisponible dans le mode courant** : `-q` est déjà actif et la
  fuite est réelle. `claude --help` 2.1.291 n'offre pas de garantie
  documentée pour ôter uniquement les blocs privés du flux structuré.
  Changer l'effort ou demander du texte ne prouverait pas la séparation
  et risquerait de modifier la lecture des événements métier par Ralph.
- **B non substituable sans changer le contrôle** : Ralph possède un
  parseur Claude et un mode `--rpc` qui sépare certains événements.
  `--rpc` est incompatible avec `-a`, active un autre chemin PTY et un
  protocole bidirectionnel ; il n'est pas une option de sortie du
  `CliExecutor` actuellement utilisé. Les événements `.ralph` servent
  aux verdicts métier, pas à une chronologie opérationnelle sûre.
- **C retenu pour le contrat** : exploiter les lignes NDJSON déjà
  garanties par Claude, à l'emplacement moteur où le backend est connu.
  Recomposer les lignes coupées par la borne de chunks P21 avant de les
  analyser ; borner le tampon. N'émettre que les champs structurés
  explicitement classables et affichables. Un bloc privé, inconnu,
  incomplet ou malformé est supprimé du flux public sans interrompre le
  worker. Ne pas parser du texte libre avec une regex. Ne pas disperser
  la connaissance Claude dans `MVPManager`, `EngineEvent` ou AIDO Code.

**Invariant public** : `execution.output` désigne une observation sûre
pour affichage, jamais un octet brut du provider. Les captures brutes
peuvent rester internes au fonctionnement et à l'audit de Ralph ; ni
`FailureDiagnostic.last_output`, ni `summary`, ni les autres champs
publics dérivés de la sortie (dont les erreurs de livraison du callback)
ne doivent reprendre un bloc privé. Les raisons d'arrêt et codes
doivent rester des faits vérifiés. Un heartbeat suffit pendant une
suppression ; aucun nouvel événement `output_suppressed` n'est prévu.
La borne/troncature P21 s'applique au texte publiable après filtrage.

**WI-P21.1-01 — Safe observable-output boundary**. Dépendances : aucune.
Réutiliser les options amont si une variante réellement sûre est
démontrée avant le code ; sinon implémenter C au point de traduction
Claude existant. Garder stdout/stderr bruts internes et le transport
progressif. Filtrer aussi les diagnostics finaux et les erreurs de
callback destinées au public. Tests synthétiques offline avec un
marqueur fictif `PRIVATE_REASONING_SENTINEL` : bloc privé absent de
`execution.output` et de tous les diagnostics ; sortie opérationnelle
voisine conservée lorsqu'elle est séparée de façon fiable ; JSON
scindé entre chunks ou malformé supprimé sans fuite ; stdout/stderr,
heartbeat, troncature, captures finales, callbacks, timeout,
annulation et nettoyage P18/P21 préservés. Aucun vrai provider dans
pytest ; Codex/Vibe/Gravity inchangés sauf invariant public commun.

**WI-P21.1-02 — Real acceptance and regression**. Dépend de 01.
Suite moteur complète et CI Python 3.10/3.12, puis exécution jetable
Claude/Ralph dans un processus neuf. Constater séparément la présence
éventuelle de raisonnement à la frontière brute/interne, son absence
obligatoire dans événements et diagnostics publics, la sortie
opérationnelle sûre, les heartbeats, le verdict et le nettoyage.
Ne reproduire aucun contenu privé dans les preuves. Après livraison,
rejouer AIDO Code M3.1 depuis un runner neuf avant de le clore ; aucun
filtre provider dans son frontend.

**Next** : faire approuver ce contrat documentaire, puis seulement
exécuter WI-P21.1-01/02 par le WorkItem Flow. Ne pas commencer M4.

### Ordre approuvé

1. P12 (`DONE`) puis P1 (`DONE`) : cycle productisation/onboarding,
   terminé.
2. P4 (étude Mammouth) a été menée, puis close `RETIRÉ` : l'agrégateur a
   été jugé d'intérêt économique/architectural insuffisant face à
   l'intégration directe de providers supplémentaires (décision
   utilisateur, 2026-09-19).
3. P3 (`RETIRÉ`, 2026-09-28, P20) : DeepSeek + Kimi, intégrés puis retirés
   faute de preuve d'exécution réelle (voir sous-section P3 ci-dessus).
4. P13 (`DONE`, priorité 1, 2026-09-19) : découplage moteur, façade
   publique `OrchestratorEngine`, création préparatoire du projet
   `aido-code`. Voir sous-section P13 ci-dessus.
5. P14 (`APPROUVÉ — APRÈS P13`, 2026-09-19) : observabilité de
   consommation et efficacité économique. Aucun WorkItem d'implémentation
   créé à ce jour. Voir sous-section P14 ci-dessus.
6. P13.4 (`DONE`, 2026-09-23) : remédiation post-audit externe — voir
   sous-section P13.4 ci-dessus.
7. P15 (`APPROUVÉ POUR ÉTUDE`, 2026-09-23) et P16 (`APPROUVÉ POUR
   REVUE`, 2026-09-23) : ni l'un ni l'autre n'est implémenté ; aucun des
   deux ne bloque M2. Voir sous-sections P15/P16 ci-dessus.
8. P13.5 (`DONE`, 2026-09-24) : frontière moteur/librairie — injection du
   `WorkerRegistry`, `workers:` optionnel dans `aido.yaml`. Voir
   sous-section P13.5 ci-dessus.
9. P13.6 (`DONE`, 2026-09-24) : retrait de la commande produit `aido` —
   AIDO Code en devient l'unique propriétaire. Voir sous-section P13.6
   ci-dessus.
10. P13.7 (`DONE`, 2026-09-25) : pre-execution state safety — un
    WorkItem/son MVP ne sont plus marqués `RUNNING` avant que les
    prérequis pré-exécution aient réussi. Voir sous-section P13.7
    ci-dessus.
11. P17 (`DONE`) : routing quota-aware déterministe dans l'unique
    `WorkerSelector`, après disponibilité et gouvernance, sans probe
    additionnel ; indépendant de P14.
12. P18 (`DONE`, GO humain 2026-09-26) : événements live publics
    (`on_event`) + interruption/recovery propre (DEV A/DEV B/DEV FIX et
    QA), prérequis pour AIDO Code M3 ; P18-01/P18-02/P18-03 `DONE`. Voir
    sous-section P18 ci-dessus.
13. P19 (`APPROUVÉ`, GO humain 2026-09-28) : worker/backend Gravity
    (`agy`), réutilisant le mécanisme custom backend déjà employé par
    Vibe ; Phase A (spike réel) `DONE` — pas une dépendance de M3. Voir
    sous-section P19 ci-dessus.
14. P20 (`DONE`) : ne garder que les providers validés et normaliser
    les workers Gravity (Arthur/Nora). Voir sous-section P20 ci-dessus.
15. P21 (`DONE`, 2026-10-06) : observabilité live des sorties worker,
    diagnostic final et heartbeat via les événements publics moteur ;
    WorkItem Flow et acceptance Gravity jetable exécutés.

### Table des propositions

| ID | Proposition | Valeur / question à trancher | Statut |
|----|-------------|------------------------------|--------|
| P1 | CLI / productisation | Le projet doit-il exposer une CLI publique pour qu'un utilisateur n'ait plus besoin d'un harnais Python ? | **`DONE` — voir §3/§10, `README.md`** |
| P1.1 | Guided project bootstrap / onboarding | Créer un projet local complet et guider son premier usage avec le `aido init` existant | **`DONE` (2026-09-24), extension de P1 — voir §13 et `docs/PROJECT_CONFIG.md`** |
| P2 | Ollama / provider local | Un provider gratuit/local est-il assez utile pour justifier un adaptateur ? | À VOTER |
| P3 | Providers supplémentaires | Quels autres providers devraient rejoindre le pool ? Étendu par décision utilisateur explicite (2026-09-19) au-delà du cadre d'origine « coût marginal nul » : DeepSeek (facturé à la consommation) et Kimi (abonnement Kimi Code) | **`RETIRÉ` (2026-09-28, P20) : DeepSeek + Kimi, voir la sous-section P3 ci-dessus** |
| P4 | Étude build-vs-reuse Mammouth AI | Offre-t-il des capacités multi-provider utiles à réutiliser plutôt qu'à construire ? | **`RETIRÉ` (2026-09-19)** : étude menée, agrégateur jugé d'intérêt économique/architectural insuffisant face à l'intégration directe ; décision terminée, pas un report ; DeepSeek/Kimi (P3) intégrés directement sur cette base ; aucune dépendance gateway/agrégateur multi-modèles |
| P5 | Projets de validation externes progressifs | Continuer à valider sur des projets réels plus complexes ? | À VOTER |
| P6 | Workflow GitHub distant complet | Étendre la gouvernance Git locale actuelle à un vrai push/PR/statut CI distant ? | À VOTER |
| P7 | Orchestration multi-projets | Une instance d'orchestrateur gérant plusieurs projets isolés ? | À VOTER |
| P8 | Exécution parallèle | WorkItems/projets/workers en parallèle ? | À VOTER |
| P9 | QA avancée/externe | Candidats historiquement étudiés : BrowserStack, Momentic, TestSprite, Diffblue — adopter seulement quand un vrai projet/stack établit le besoin ? | À VOTER |
| P10 | Isolation d'exécution QA en lecture seule | Worktree/copie isolée vs. solution amont Ralph pour les commits de housekeeping ? | À VOTER |
| P11 | Productiser le cycle optionnel release/planning | `PlanningCoordinator` → `ApprovalCoordinator` → `RoadmapApplicationService` existent déjà (§10) — en faire un flux produit supporté de bout en bout ? | À VOTER |
| P12 | Format de configuration de projet public | Aucun format déclaratif stable n'existait pour onboarder un projet (harnais Python custom) | **`DONE` — voir §3/§10, `docs/PROJECT_CONFIG.md`** |
| P13 | Découplage moteur / externalisation AIDO Code | Le moteur headless doit-il être séparé d'une future interface terminal interactive (AIDO Code), pour rester réutilisable par un frontend externe ? | **`DONE`, priorité 1 (2026-09-19) — voir §3/§10, sous-section P13 ci-dessus** |
| P13.1 | Première exécution réelle du WorkItem Flow sur AIDO Code (M1) — défaut moteur QA découvert et corrigé | Le run réel de M1 (WI-01..WI-07) a-t-il fonctionné, et qu'a-t-il révélé sur le moteur lui-même ? | **`DONE` (2026-09-21) — M1 complété fonctionnellement ; défaut réel de preuve QA (`FAIL`->`PASS` sur SHA identique via dérive d'environnement, hors Git) découvert et corrigé (`VALIDATION_ENVIRONMENT_CHANGED`) ; attribution Git par worker ajoutée — voir sous-section P13.1 ci-dessus** |
| P13.2 | Attribution Git worker renforcée après un défaut réel découvert sur AIDO Code (M1.1) | Un commit fonctionnel réel d'un worker (M1.1, WI-M1.1-01) est resté attribué à l'identité ambiante du mainteneur au lieu du worker — l'injection d'environnement seule dans le subprocess `ralph` était-elle suffisante ? | **`DONE` (2026-09-22) — non, un `git commit` imbriqué peut ne pas hériter cet environnement ; config Git locale au workspace en défense indépendante + audit post-exécution fail-closed (`WorkerCommitIdentityMismatchError`) ajoutés — voir sous-section P13.2 ci-dessus** |
| P13.3 | Status opérationnel complet, quotas riches, registry standalone, GPT-6 | Le kata externe a révélé `STANDALONE_RUNTIME_PASS = FAIL` (aucun registry livré) ; `aido status` restait en retard sur AIDO Code (M1.1) ; `MVP status=running` avec 100% WorkItems `completed` est-il un bug ? | **`DONE` (2026-09-23) — `aido status`/`--probe` enrichis (workers/prénoms/quota par provider) ; registry par défaut packagé + `aido init` standalone ; modèles Codex GPT-6 validés réellement ; `MVP status=running` confirmé NON bug (ReleaseManager/P11 séparé, `À VOTER`) — voir sous-section P13.3 ci-dessus** |
| P14 | Observabilité de consommation et efficacité économique | Le moteur doit-il enregistrer, par exécution, les métriques réelles (tokens, durée, retries, coût observé/estimé) nécessaires pour identifier ensuite quelles phases/workers/providers sont les moins économiques ? | **APPROUVÉ — APRÈS P13** (2026-09-19). Aucun WorkItem d'implémentation créé à ce jour ; voir sous-section P14 ci-dessous pour le détail complet des critères |
| P13.4 | Remédiation post-audit externe (Mistral) | Un audit technique externe a trouvé 11 findings (1 HIGH, 4 MEDIUM, 6 LOW) sur le SHA `75b3ef5` — combien sont réels, et corrigés comment ? | **`DONE` (2026-09-23)** — 8/11 corrigés (dont le HIGH), 2 documentés/différés (YAGNI), 1 classé P16 (candidat DELETE). Rapport : `docs/reports/mistral-engine-audit-2026-09-23.md` ; voir sous-section P13.4 ci-dessus |
| P15 | Prompt optimization externe | Une capacité d'optimisation de prompts basée sur un dataset/métrique réels (candidat : Opik Optimizer) mérite-t-elle d'être étudiée, avant toute intégration ? | **APPROUVÉ POUR ÉTUDE** (2026-09-23). Pas d'intégration ; dépend de P14 (non implémenté) pour la télémétrie. Comparaison complète : `docs/ECOSYSTEM.md` ; voir sous-section P15 ci-dessus |
| P16 | Revue de simplification YAGNI/REUSE FIRST | Une revue structurée (DELETE → STDLIB → REUSE → PACKAGE → BUILD) doit-elle encadrer toute recommandation de simplification, y compris celles d'un audit externe ? | **APPROUVÉ POUR REVUE** (2026-09-23). Pas de refactor global autorisé par ce seul vote ; candidats déjà identifiés : `Project.current_mvp_id` (DELETE, analyse compatibilité requise), fingerprint d'environnement non-Python (BUILD rejeté, YAGNI) ; voir sous-section P16 ci-dessus |
| P17 | Quota-aware worker routing | Le `WorkerSelector` doit-il exploiter les `utilization` déjà sondées pour préférer un provider disponible nettement moins consommé ? | **`DONE`** — pression=max(utilization connue), bande de 10 points, inconnu neutre, gouvernance avant quota, aucun second probe ; indépendant de P14 |
| P18 | Live execution events and graceful interruption | `OrchestratorEngine` doit-il exposer des événements publics fins (DEV A/DEV B/DEV FIX/QA/Git) en temps réel, avec les métadonnées réellement décidées, et traiter explicitement une interruption pendant `run()` ? | **`DONE`** (GO humain 2026-09-26) — callback `on_event` optionnel ; **P18-01/P18-02/P18-03 `DONE`** (transport live + métadonnées fines DEV A/B/FIX/QA/Git, `EngineEvent` dans `orchestrator.engine_events`, `RunResult.events` inchangé, interruption/recovery DEV+QA) ; Adaptive Execution non branché ; recovery étendu (jamais dupliqué) ; voir sous-section P18 ci-dessus |
| P19 | Gravity worker/backend | Un worker Gravity (`agy`), exécutable via Ralph en réutilisant le mécanisme custom backend déjà employé par Vibe, mérite-t-il d'être intégré ? | **`APPROUVÉ`** (GO humain 2026-09-28) — Phase A (spike réel) `DONE` ; implémentation en cours (branche `feature/p19-gravity-worker-backend`) ; pas une dépendance de M3 ; voir sous-section P19 ci-dessus |
| P21 | Live worker execution observability | Comment rendre visibles les sorties réelles du worker et les échecs sans verdict métier dans `aido run`, sans scraping frontend ni retry ? | **`DONE` (2026-10-06)** — WorkItems P21-01/02/03/04 livrés, PR #36, CI Python 3.10/3.12 et acceptance Gravity jetable ; voir sous-section P21 ci-dessus |
| P21.1 | Safe public execution output | Comment garantir que sorties et diagnostics publics n'exposent aucun bloc de raisonnement privé ? | **`APPROUVÉ` (GO humain 2026-10-06)** — architecture C retenue après audit de Ralph/Claude installés ; deux WorkItems autorisés via le WorkItem Flow ; voir sous-section P21.1 ci-dessus |
| P13.5 | Frontière moteur/librairie : injection du `WorkerRegistry`, `workers:` optionnel | `aido.yaml` doit-il rester la source de configuration complète du pool de workers, ou le moteur doit-il accepter un `WorkerRegistry` construit/injecté par l'application appelante (AIDO Code) ? | **`DONE`** (2026-09-24) — `workers:` optionnel dans `ProjectConfig` ; `OrchestratorEngine`/`ProjectRuntime` acceptent `worker_registry=` ; `WorkerSelector` reste seul propriétaire de la sélection ; chemin legacy fichier intégralement conservé et testé ; voir sous-section P13.5 ci-dessus |
| P13.6 | Retrait de la commande produit `aido` (cutover AIDO Code) | AIDO Code ayant atteint son propre cutover produit, `ai-dev-orchestrator` doit-il cesser d'installer la commande `aido` ? | **`DONE`** (2026-09-24) — `[project.scripts]` retiré de `pyproject.toml` ; `orchestrator.cli`/`default_workers.yaml` conservés, legacy/internes, toujours réellement testés ; aucun binaire de compatibilité ajouté (YAGNI) ; voir sous-section P13.6 ci-dessus |
| P13.7 | Pre-execution state safety | Le cutover M8 d'AIDO Code a révélé un défaut réel (`WI-M8-01` resté `RUNNING` durablement, sans `ExecutionRecord`) — un WorkItem/son MVP doivent-ils n'être marqués `RUNNING` qu'une fois tous les prérequis pré-exécution (préparation Git notamment) réellement satisfaits ? | **`DONE`** (2026-09-25) — `mark_mvp_running`/`mark_work_item_running` déplacés après les prérequis dans `_execute_work_item` et `_resume_dev_b_wait` (deux sites réels) ; invariant de recovery existant inchangé ; 4 nouveaux tests, chacun vérifié rouge sans le correctif ; `WI-M8-01` non modifié rétroactivement (YAGNI) ; voir sous-section P13.7 ci-dessus |

Les propositions encore `À VOTER` restent non planifiées ; aucun ordre entre
elles n'est impliqué. La prochaine étape pour celles-ci, si l'utilisateur le
décide, est un vote explicite proposition par proposition, pas une
sélection automatique par cette session ni une future session. P12, P1,
P1.1, P13, P13.4, P13.5, P13.6 et P13.7 sont `DONE`. P3 est `RETIRÉ` (P20). P14 est `APPROUVÉ — APRÈS P13`, sans WorkItem
d'implémentation créé à ce jour. P15 est `APPROUVÉ POUR ÉTUDE`, P16
`APPROUVÉ POUR REVUE` — ni l'un ni l'autre n'est implémenté, ni ne bloque
M2. P4 est `RETIRÉ` : décision terminée, pas un report. Elle ne redevient
pas un prérequis implicite d'une future proposition sans un nouveau vote
explicite. P19 (worker Gravity) est `APPROUVÉ` (GO humain, 2026-09-28),
Phase A (spike réel) `DONE` — jamais une dépendance implicite d'AIDO
Code M3, qui reste exécutable avec le pool de workers actuel.
