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
- **P13.5 (frontière moteur/librairie : injection du `WorkerRegistry`,
  `workers:` optionnel) : `DONE`** (2026-09-24), voir §13. `aido.yaml` ne
  possède plus obligatoirement le pool de workers ; `OrchestratorEngine`/
  `ProjectRuntime` acceptent un `WorkerRegistry` injecté par l'appelant,
  chemin legacy fichier intégralement conservé.
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
- CLI publique `aido` (P1) — `aido init/validate/run/status`, plus besoin
  de harnais Python pour l'usage normal. `run` est aussi la reprise (pas
  de commande `resume` séparée). Voir §10 pour le détail complet.
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
- P1.1 — bootstrap projet guidé dans le `aido init` existant, Git local et
  reprise manuelle en cas d’échec, compatibilité préservée (2026-09-24).
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
`config/workers.yaml`, parsing réel, DeepSeek/Kimi désactivés, mapping
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
6. P13.4 (`DONE`, 2026-09-23) : remédiation post-audit externe — voir
   sous-section P13.4 ci-dessus.
7. P15 (`APPROUVÉ POUR ÉTUDE`, 2026-09-23) et P16 (`APPROUVÉ POUR
   REVUE`, 2026-09-23) : ni l'un ni l'autre n'est implémenté ; aucun des
   deux ne bloque M2. Voir sous-sections P15/P16 ci-dessus.
8. P13.5 (`DONE`, 2026-09-24) : frontière moteur/librairie — injection du
   `WorkerRegistry`, `workers:` optionnel dans `aido.yaml`. Voir
   sous-section P13.5 ci-dessus.

### Table des propositions

| ID | Proposition | Valeur / question à trancher | Statut |
|----|-------------|------------------------------|--------|
| P1 | CLI / productisation | Le projet doit-il exposer une CLI publique pour qu'un utilisateur n'ait plus besoin d'un harnais Python ? | **`DONE` — voir §3/§10, `README.md`** |
| P1.1 | Guided project bootstrap / onboarding | Créer un projet local complet et guider son premier usage avec le `aido init` existant | **`DONE` (2026-09-24), extension de P1 — voir §13 et `docs/PROJECT_CONFIG.md`** |
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
| P13.1 | Première exécution réelle du WorkItem Flow sur AIDO Code (M1) — défaut moteur QA découvert et corrigé | Le run réel de M1 (WI-01..WI-07) a-t-il fonctionné, et qu'a-t-il révélé sur le moteur lui-même ? | **`DONE` (2026-09-21) — M1 complété fonctionnellement ; défaut réel de preuve QA (`FAIL`->`PASS` sur SHA identique via dérive d'environnement, hors Git) découvert et corrigé (`VALIDATION_ENVIRONMENT_CHANGED`) ; attribution Git par worker ajoutée — voir sous-section P13.1 ci-dessus** |
| P13.2 | Attribution Git worker renforcée après un défaut réel découvert sur AIDO Code (M1.1) | Un commit fonctionnel réel d'un worker (M1.1, WI-M1.1-01) est resté attribué à l'identité ambiante du mainteneur au lieu du worker — l'injection d'environnement seule dans le subprocess `ralph` était-elle suffisante ? | **`DONE` (2026-09-22) — non, un `git commit` imbriqué peut ne pas hériter cet environnement ; config Git locale au workspace en défense indépendante + audit post-exécution fail-closed (`WorkerCommitIdentityMismatchError`) ajoutés — voir sous-section P13.2 ci-dessus** |
| P13.3 | Status opérationnel complet, quotas riches, registry standalone, GPT-6 | Le kata externe a révélé `STANDALONE_RUNTIME_PASS = FAIL` (aucun registry livré) ; `aido status` restait en retard sur AIDO Code (M1.1) ; `MVP status=running` avec 100% WorkItems `completed` est-il un bug ? | **`DONE` (2026-09-23) — `aido status`/`--probe` enrichis (workers/prénoms/quota par provider) ; registry par défaut packagé + `aido init` standalone ; modèles Codex GPT-6 validés réellement ; `MVP status=running` confirmé NON bug (ReleaseManager/P11 séparé, `À VOTER`) — voir sous-section P13.3 ci-dessus** |
| P14 | Observabilité de consommation et efficacité économique | Le moteur doit-il enregistrer, par exécution, les métriques réelles (tokens, durée, retries, coût observé/estimé) nécessaires pour identifier ensuite quelles phases/workers/providers sont les moins économiques ? | **APPROUVÉ — APRÈS P13** (2026-09-19). Aucun WorkItem d'implémentation créé à ce jour ; voir sous-section P14 ci-dessous pour le détail complet des critères |
| P13.4 | Remédiation post-audit externe (Mistral) | Un audit technique externe a trouvé 11 findings (1 HIGH, 4 MEDIUM, 6 LOW) sur le SHA `75b3ef5` — combien sont réels, et corrigés comment ? | **`DONE` (2026-09-23)** — 8/11 corrigés (dont le HIGH), 2 documentés/différés (YAGNI), 1 classé P16 (candidat DELETE). Rapport : `docs/reports/mistral-engine-audit-2026-09-23.md` ; voir sous-section P13.4 ci-dessus |
| P15 | Prompt optimization externe | Une capacité d'optimisation de prompts basée sur un dataset/métrique réels (candidat : Opik Optimizer) mérite-t-elle d'être étudiée, avant toute intégration ? | **APPROUVÉ POUR ÉTUDE** (2026-09-23). Pas d'intégration ; dépend de P14 (non implémenté) pour la télémétrie. Comparaison complète : `docs/ECOSYSTEM.md` ; voir sous-section P15 ci-dessus |
| P16 | Revue de simplification YAGNI/REUSE FIRST | Une revue structurée (DELETE → STDLIB → REUSE → PACKAGE → BUILD) doit-elle encadrer toute recommandation de simplification, y compris celles d'un audit externe ? | **APPROUVÉ POUR REVUE** (2026-09-23). Pas de refactor global autorisé par ce seul vote ; candidats déjà identifiés : `Project.current_mvp_id` (DELETE, analyse compatibilité requise), fingerprint d'environnement non-Python (BUILD rejeté, YAGNI) ; voir sous-section P16 ci-dessus |
| P13.5 | Frontière moteur/librairie : injection du `WorkerRegistry`, `workers:` optionnel | `aido.yaml` doit-il rester la source de configuration complète du pool de workers, ou le moteur doit-il accepter un `WorkerRegistry` construit/injecté par l'application appelante (AIDO Code) ? | **`DONE`** (2026-09-24) — `workers:` optionnel dans `ProjectConfig` ; `OrchestratorEngine`/`ProjectRuntime` acceptent `worker_registry=` ; `WorkerSelector` reste seul propriétaire de la sélection ; chemin legacy fichier intégralement conservé et testé ; voir sous-section P13.5 ci-dessus |

Les propositions encore `À VOTER` restent non planifiées ; aucun ordre entre
elles n'est impliqué. La prochaine étape pour celles-ci, si l'utilisateur le
décide, est un vote explicite proposition par proposition, pas une
sélection automatique par cette session ni une future session. P12, P1,
P1.1, P13, P13.4 et P13.5 sont `DONE`. P3 est `IMPLEMENTED`, validation
réelle `PENDING`. P14 est `APPROUVÉ — APRÈS P13`, sans WorkItem
d'implémentation créé à ce jour. P15 est `APPROUVÉ POUR ÉTUDE`, P16
`APPROUVÉ POUR REVUE` — ni l'un ni l'autre n'est implémenté, ni ne bloque
M2. P4 est `RETIRÉ` : décision terminée, pas un report. Elle ne redevient
pas un prérequis implicite d'une future proposition sans un nouveau vote
explicite.
