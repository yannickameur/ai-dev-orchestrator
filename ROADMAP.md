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
- Suite de tests offline : **1157 PASS** (1135 avant, 22 pour la façade
  moteur `OrchestratorEngine` ; voir §13, sous-section P13, et
  docs/status.md pour le détail).
- **P12 (format de configuration de projet public + mode de permission
  d'exécution des workers) : `DONE`**, voir §10 et `docs/PROJECT_CONFIG.md`.
  **P1 (CLI publique `aido`) : `DONE`** (`aido init/validate/run/status`,
  voir §10). Cycle productisation/onboarding terminé.
- **P3 (DeepSeek + Kimi comme providers de premier niveau) :
  `IMPLEMENTED` (2026-09-19), validation réelle `PENDING`**, voir §7 et
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

## 3. Ce qui existe aujourd'hui

- Orchestration durable Project / MVP / WorkItem (`ProjectStateStore`,
  SQLite, survit à un redémarrage).
- `WorkerRegistry` — pool de workers déclaratif (`config/workers.yaml`),
  aucun worker codé en dur.
- `WorkerSelector` — capability > gouvernance > disponibilité provider >
  priorité ; jamais le coût comme critère de sélection du worker
  lui-même.
- Gestion de quota consciente du provider (`QuotaManager` +
  `ProviderAdapter` par provider, re-probe réel, jamais un reset simulé).
- Adaptateur Claude Code (Anthropic).
- Adaptateur Codex CLI (OpenAI).
- Adaptateur Mistral/Vibe — honnêtement limité à `EXECUTION_PROBE_ONLY`
  (aucune fenêtre de quota observable pour ce provider ; jamais fabriquée).
- Adaptateurs DeepSeek et Kimi — jamais un second client HTTP indépendant :
  les deux réutilisent l'adaptateur Claude Code existant (même binaire
  `claude`, redirigé via `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` vers leur
  point de terminaison compatible Anthropic ; voir
  `orchestrator.providers.deepseek_adapter`/`kimi_adapter`). Désactivés par
  défaut dans `config/workers.yaml` (`dana`/`kai`) : clé API requise, aucune
  preuve d'exécution réelle encore obtenue. Voir §7.
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
- `ProjectConfig` (P12) — format de configuration public `aido.yaml`
  schema v1 : identité de projet, référence (jamais copie) au pool de
  workers, mode de permission d'exécution project-controlled, politique
  Git, MVP/WorkItems, commandes QA déterministes. `ExecutionPermissionMode`
  (`standard`/`unrestricted`), traduit en flags CLI réels et vérifiés
  exclusivement à la frontière `RalphExecutionEngine` (`docs/PROJECT_CONFIG.md`)
  — Vibe n'est plus jamais unconditionnellement `--auto-approve`.
- CLI publique `aido` (P1) — `aido init/validate/run/status`, plus besoin
  de harnais Python pour l'usage normal. `run` est aussi la reprise (pas
  de commande `resume` séparée). Voir §10 pour le détail complet.
- Façade moteur publique `orchestrator.engine.OrchestratorEngine` (P13) —
  `.open()`/`.validate()`/`.status()`/`.workers()`/`.probe_workers()`/
  `.run()`/`.close()`, masquant `ProjectRuntime`/`MVPManager`/
  `WorkerSelector`/`QuotaManager`/`ProviderAdapter`/`GitGovernanceService`/
  `InternalQAEngine`/toute Store derrière des snapshots typés,
  sérialisables, jamais un objet interne. Le CLI `aido` existant reste
  intact et n'est pas migré vers cette façade par P13. Voir §10.

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
| `bob` | anthropic | claude_code | 90 | (identiques à alice) | ✅ VALIDATED |
| `victor` | openai | codex | 100 | development, release_planning, roadmap_synthesis, complexity_estimation, qa_testing | ✅ VALIDATED |
| `oscar` | openai | codex | 90 | (identiques à victor) | ✅ VALIDATED |
| `milo` | mistral | vibe | 60 | development uniquement | ✅ VALIDATED |
| `juno` | mistral | vibe | 60 | development uniquement | ✅ VALIDATED |
| `dana` | deepseek | claude_code | 60 | development uniquement | `IMPLEMENTED — REAL VALIDATION PENDING` (`enabled: false`) |
| `kai` | kimi | claude_code | 60 | development uniquement | `IMPLEMENTED — REAL VALIDATION PENDING` (`enabled: false`) |

8 workers déclarés, **5 providers de premier niveau** (anthropic, openai,
mistral, deepseek, kimi), tous résolus par la même table explicite,
`orchestrator.project_runtime._PROVIDER_ADAPTER_FACTORIES`, jamais une
hiérarchie métier codée en dur entre eux). 6 workers **activés** par
défaut, 3 providers activés par défaut ; chaque provider activé a au moins
2 workers indépendants (`DEV_B.worker_id != DEV_A.worker_id` reste toujours
satisfiable sans dépendre de l'autre provider).

Mistral/Vibe : capacité volontairement limitée à `development` (le spike
n'a produit de preuve d'exécution réelle que pour ce type de travail —
voir `docs/VIBE_SPIKE.md`) ; son signal de disponibilité est
`EXECUTION_PROBE_ONLY` (pas de fenêtre de quota observable), toujours
rapporté honnêtement comme `unknown`, jamais fabriqué en pourcentage.

**DeepSeek et Kimi (2026-09-19)** : `IMPLEMENTED — REAL VALIDATION
PENDING`. Intégrés à la même profondeur architecturale que les trois
providers existants (même contrat `ProviderAdapter`/`ProviderState`, même
table de résolution), mais `dana`/`kai` restent `enabled: false` dans le
fichier livré, pour deux raisons distinctes :
- Les deux nécessitent une vraie clé API (`DEEPSEEK_API_KEY`/`KIMI_API_KEY`,
  jamais committée, sans valeur par défaut), contrairement à Claude
  Code/Codex/Vibe dont l'authentification reste entièrement au CLI, déjà
  connecté en dehors de ce projet. Une clé absente ne doit pas empêcher les
  trois autres providers de fonctionner : voir
  `orchestrator.providers.deepseek_adapter`/`kimi_adapter` et
  `ProjectRuntime.ProviderConfigurationError` (erreur contrôlée, pas de
  traceback brute).
- Aucun pilote réel n'a encore prouvé leur fonctionnement (pas de spike
  équivalent à `docs/VIBE_SPIKE.md`). `enabled: true` est un pas distinct,
  après configuration d'une vraie clé et une exécution réelle validée
  (critères listés en §13), jamais une bascule automatique.

DeepSeek est facturé à la consommation (PAYG), ce qui s'écarte à la lettre
du cadre d'origine « coût marginal nul » de la proposition P3 (§13) ;
Kimi passe par **Kimi Code** (abonnement/quota) plutôt que par un accès
PAYG générique. Les deux ont été retenus sur décision utilisateur
explicite malgré cet écart de cadrage.

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
  Python) ; seul `aido run` provoque un appel provider réel ou une
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
- **DeepSeek / Kimi** — `IMPLEMENTED — REAL VALIDATION PENDING`.
  Architecture/tests offline complets (§7, §13) ; aucune exécution réelle
  n'a eu lieu, ni clé API (`DEEPSEEK_API_KEY`/`KIMI_API_KEY`) configurée
  sur une machine de ce projet, ni spike équivalent à
  `docs/VIBE_SPIKE.md`. `dana`/`kai` restent `enabled: false` jusqu'à
  cette validation.
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
- P4 (étude Mammouth) menée puis close `RETIRÉ` : agrégateur jugé d'intérêt
  économique/architectural insuffisant face à l'intégration directe de
  providers (décision utilisateur, 2026-09-19).
- P3 — DeepSeek + Kimi intégrés comme providers de premier niveau, sur
  cette base, en réutilisant l'adaptateur Claude Code existant
  (redirection `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`) ; `dana`/`kai`
  désactivés par défaut faute de clé/preuve d'exécution réelle
  (implémentation `DONE`, validation réelle `PENDING`, 2026-09-19).
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

### P3 — DeepSeek + Kimi — implémentation `DONE`, validation réelle `PENDING` (2026-09-19)

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
`.probe_workers()` ne mute jamais `os.environ` globalement ; les
credentials DeepSeek/Kimi ne sont lus que si le provider est réellement
requis (inchangé, §7) ; aucun secret n'entre dans `aido.yaml`/
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

### Ordre approuvé

1. P12 (`DONE`) puis P1 (`DONE`) : cycle productisation/onboarding,
   terminé.
2. P4 (étude Mammouth) a été menée, puis close `RETIRÉ` : l'agrégateur a
   été jugé d'intérêt économique/architectural insuffisant face à
   l'intégration directe de providers supplémentaires (décision
   utilisateur, 2026-09-19).
3. P3 (implémentation `DONE`, validation réelle `PENDING`, 2026-09-19) :
   DeepSeek + Kimi intégrés comme providers de premier niveau, sur la base
   de la conclusion P4 (voir sous-section P3 ci-dessus et §7).
4. P13 (`DONE`, priorité 1, 2026-09-19) : découplage moteur, façade
   publique `OrchestratorEngine`, création préparatoire du projet
   `aido-code`. Voir sous-section P13 ci-dessus.
5. P14 (`APPROUVÉ — APRÈS P13`, 2026-09-19) : observabilité de
   consommation et efficacité économique. Aucun WorkItem d'implémentation
   créé à ce jour. Voir sous-section P14 ci-dessus.

### Table des propositions

| ID | Proposition | Valeur / question à trancher | Statut |
|----|-------------|------------------------------|--------|
| P1 | CLI / productisation | Le projet doit-il exposer une CLI publique pour qu'un utilisateur n'ait plus besoin d'un harnais Python ? | **`DONE` — voir §3/§10, `README.md`** |
| P2 | Ollama / provider local | Un provider gratuit/local est-il assez utile pour justifier un adaptateur ? | À VOTER |
| P3 | Providers supplémentaires | Quels autres providers devraient rejoindre le pool ? Étendu par décision utilisateur explicite (2026-09-19) au-delà du cadre d'origine « coût marginal nul » : DeepSeek (facturé à la consommation) et Kimi (abonnement Kimi Code) | **Implémentation `DONE`, validation réelle `PENDING` (2026-09-19) : DeepSeek + Kimi, voir §7 et la sous-section P3 ci-dessus** |
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
| P14 | Observabilité de consommation et efficacité économique | Le moteur doit-il enregistrer, par exécution, les métriques réelles (tokens, durée, retries, coût observé/estimé) nécessaires pour identifier ensuite quelles phases/workers/providers sont les moins économiques ? | **APPROUVÉ — APRÈS P13** (2026-09-19). Aucun WorkItem d'implémentation créé à ce jour ; voir sous-section P14 ci-dessous pour le détail complet des critères |

Les propositions encore `À VOTER` restent non planifiées ; aucun ordre entre
elles n'est impliqué. La prochaine étape pour celles-ci, si l'utilisateur le
décide, est un vote explicite proposition par proposition, pas une
sélection automatique par cette session ni une future session. P12, P1 et
P13 sont `DONE`. P3 est `IMPLEMENTED`, validation réelle `PENDING`. P14
est `APPROUVÉ — APRÈS P13`, sans WorkItem d'implémentation créé à ce
jour. P4 est `RETIRÉ` : décision terminée, pas un report. Elle ne
redevient pas un prérequis implicite d'une future proposition sans un
nouveau vote explicite.
