# ADAPTIVE_EXECUTION.md — Adaptive Worker/Profile selection architecture

**Note historique :** ce document a été écrit comme un document d'étude
*avant* l'implémentation des Slices 15-18 (Phase 1) — il fixait alors
l'architecture cible et les décisions tranchées pour que l'implémentation
future n'ait plus à ré-ouvrir ces questions. Cette architecture a depuis
été **implémentée intégralement** (Slices 15-19). Certaines sections
conservent volontairement le raisonnement/le formalisme *tel qu'il a été
pensé avant construction* (utile pour comprendre le "pourquoi") ; les
annotations « Fait (Slice N) » / « Résolu par Slice N » insérées au fil de
l'implémentation reflètent l'état final réellement construit et font
autorité sur le texte d'étude environnant en cas de doute. Voir
`ROADMAP.md`, section « Découpage incrémental », pour le détail
slice-par-slice, et `ROADMAP.md`, section « Chemin nominal actuel », pour
la note de statut courante ci-dessous.

**CURRENT STATUS (2026-09-17)** :
- Slices 15-19 implémentées (Worker Registry, Execution Profiles, quality
  tiers, complexity pre-flight, sélection adaptative development/rework/
  review/planning) — voir statuts individuels dans `ROADMAP.md`.
- La machinerie adaptative reste disponible et n'est pas retirée.
- `LEAN_FEATURE_FLOW` est désormais le workflow `DEFAULT` ; son chemin
  nominal n'invoque **aucune** estimation de complexité/sélection adaptative.
- Cette machinerie est conservée telle quelle mais **non enrichie** sans
  besoin réel démontré (KISS/YAGNI).
- `GOVERNED_FULL` (qui utilisait pleinement cette machinerie pour le
  développement/rework/review) a été **retiré** avant la première release
  publique (2026-09-18, voir l'entrée datée dans `ROADMAP.md`) —
  `WorkflowMode` lui-même n'existe plus. Seule la partie
  développement/rework de cette machinerie adaptative reste effectivement
  invocable par du code produit (`MVPManager._select_dev_worker`,
  toujours optionnelle, jamais câblée par défaut) ; la sélection
  adaptative de reviewer a été retirée avec le reste de l'ancien
  pipeline.

Contexte (au moment de l'étude) : depuis Slice 14, la boucle Release ->
ActivityReport -> planning -> RoadmapProposal -> approbation ->
`RoadmapApplicationService` -> MVP N+1 est fonctionnelle de bout en bout.
L'adaptive execution **améliore** comment les Workers sont choisis/
configurés à l'intérieur de cette boucle — elle ne la remplace pas et ne
remet en cause aucune des Slices 0-14.

## 1. Ce que Ralph fournit déjà (findings)

Source : `docs/SPIKE_RALPH.md` (spike expérimental déjà exécuté et validé,
Ralph 2.10.1), complété par l'inspection de `~/projects/ralph-spike`
(`ralph.yml`, `ralph-codex.yml`, `hats-backend-spike.yml`) et de l'aide CLI
locale (`ralph --help`, `ralph hats --help`, `ralph tutorial --no-input`)
pour cette session. Aucun nouveau worker IA n'a été relancé : les preuves
citées ci-dessous viennent du spike déjà réalisé (tests réels avec Claude
Haiku et Codex/gpt-5.6-terra) ou de commandes non destructives/statiques
(`--help`, `hats show`, lecture de fichiers).

Confirmé aussi par l'inspection de `src/orchestrator/ralph_execution_engine.py` :
chaque `Execution` génère un `hats.yml`/`config.yml` **temporaire**
(`tempfile.mkdtemp`) avec **un seul hat** dont `backend.type`/`args` sont
dérivés de `Worker.backend`/`.model`/`.reasoning_effort` — jamais de
mutation d'un `ralph.yml` permanent. C'est déjà le patron « une Execution =
un hat = une configuration jetable », qui reste la base de toute
l'architecture adaptative ci-dessous.

## 2. Tableau NATIVE / PARTIAL / ORCHESTRATOR

| # | Question | Classification | Preuve |
|---|---|---|---|
| 1 | Définir plusieurs hats/workers | **NATIVE_RALPH** | `hats:` YAML, `ralph hats list/show/graph/validate` ; workflows `code-assist` (4 hats) et `review` (3 hats) opérationnels (SPIKE_RALPH.md §Workflows). |
| 2 | Configurer par hat : backend/model/reasoning_effort | **NATIVE_RALPH** | Testé réellement : `backend: {type: codex, model: gpt-5.6-terra, reasoning_effort: low}` — la session Codex réelle a confirmé ces valeurs (SPIKE_RALPH.md §"Configuration model + reasoning_effort par hat"). |
| 3 | Changer ces paramètres entre deux exécutions | **NATIVE_RALPH** | Déjà notre patron actuel : `RalphExecutionEngine._render_hats_config` régénère un hat/backend différent à chaque `Execution` (fichier temporaire, jamais un fichier partagé muté). |
| 4 | Ralph choisit lui-même le modèle selon la complexité | **ORCHESTRATOR_RESPONSIBILITY** | Aucune preuve d'un tel mécanisme dans le spike ni dans l'aide CLI ; la config hat est déclarative et statique par run. |
| 5 | Ralph choisit lui-même `reasoning_effort` selon la complexité | **ORCHESTRATOR_RESPONSIBILITY** | Idem — champ déclaratif statique, jamais dérivé dynamiquement par Ralph. |
| 6 | Ralph sélectionne un provider selon son quota | **ORCHESTRATOR_RESPONSIBILITY** | Limitation CRITIQUE observée : un hat en quota épuisé termine silencieusement (0 token/0 turn), sans fallback intelligent — Ralph continue simplement l'itération suivante (SPIKE_RALPH.md §"CRITICAL — Limitation détectée"). |
| 7 | Plusieurs profils possibles pour un même agent logique | **PARTIAL_RALPH** | Un hat = une config figée. Plusieurs "profils" ne peuvent exister qu'en déclarant plusieurs hats/exécutions distincts (ou en régénérant la config, comme aujourd'hui) — pas de notion native de "profil sélectionnable" pour un même hat. |
| 8 | Exécuter un hat estimator avant un hat developer | **NATIVE_RALPH** (mécanisme) / usage actuel **PARTIAL** | Le chaînage événementiel hat->hat est le cœur validé de `code-assist` (Planner→Builder→Critic→Finalizer). Techniquement faisable en un seul `ralph run`. Mais notre architecture actuelle (Slice 6/9/12) exécute **un hat par `Execution`** (planner, puis synthesizer = deux appels `RalphExecutionEngine.execute()` séparés, jamais un seul run multi-hats) — pour rester cohérent avec ce patron déjà établi, l'estimator sera lui aussi une `Execution` séparée, séquencée par l'orchestrateur, pas un second hat dans le même run Ralph. |
| 9 | Exploiter un event/payload structuré d'un hat pour paramétrer l'exécution suivante | **ORCHESTRATOR_RESPONSIBILITY** (le transport est NATIVE_RALPH) | Ralph transporte et journalise des payloads structurés (`ralph emit "topic" '<json>'`, `.ralph/events.jsonl`) — déjà exploité tel quel par `planning.py`/`review.py`. Mais c'est l'orchestrateur qui lit ce payload et construit la *prochaine* Execution (nouveau hat/config) — Ralph ne fait jamais lui-même le lien entre le contenu d'un payload et le choix du backend/model d'un hat suivant. |
| 10 | Générer une configuration runtime temporaire sans modifier la config permanente | **NATIVE_RALPH** | Déjà notre implémentation : `tempfile.mkdtemp(prefix="ralph-exec-...")`, jamais d'écriture dans un `ralph.yml` utilisateur. |
| 11 | Exposer les événements nécessaires pour que l'orchestrateur fasse le routing | **NATIVE_RALPH** | `ralph emit`, `.ralph/events.jsonl`, événements applicatifs dédiés (jamais `task.start`/`task.resume`, réservés) — déjà la base de `RalphExecutionEngine`/`planning.py`/`review.py`. |
| 12 | Gérer nativement coût/quota/disponibilité provider | **ORCHESTRATOR_RESPONSIBILITY** (confirmé) | Télémétrie Ralph "PARTIALLY RELIABLE", y compris en cas de succès pour Codex (`input_tokens=0` alors que la session réelle a bien tourné) — jamais une source canonique de quota (SPIKE_RALPH.md §Télémétrie). C'est pour cela que `QuotaManager`/`WorkerSelector`/`wait.py` existent déjà. |
| 13 | Concept analogue à nos quality tiers | **ORCHESTRATOR_RESPONSIBILITY** | Ralph a des « quality gates » (tests/lint/typecheck à satisfaire avant qu'un hat déclare "done") — un concept différent (barrière de sortie, pas classification amont de complexité). Aucun équivalent à un tiering de complexité pour choisir un modèle. |
| 14 | Choisir dynamiquement un backend/modèle entre plusieurs candidats | **ORCHESTRATOR_RESPONSIBILITY** | Assignation statique par hat uniquement ; « pas de fallback intelligent intégré » observé explicitement (SPIKE_RALPH.md). |

## 3. Frontière Ralph/orchestrateur (confirmée)

La frontière hypothétique du prompt est confirmée par l'étude, avec une
précision sur le point 8/9 (granularité "une Execution = un hat") :

```
AI DEV ORCHESTRATOR                          RALPH
  décide :                                     exécute :
  - worker logique                    ->       - hat (config jetable,
  - capability                                    1 par Execution)
  - quality tier minimum               ->      - backend
  - ExecutionProfile (model +          ->      - model / reasoning
    reasoning_effort)                            (args CLI du backend)
  - disponibilité/quota (QuotaManager)          - workflow (si multi-hats
  - policy coût                                  natif requis plus tard)
                                                - runtime (subprocess,
                                                  timeout, events)
```

Rien dans l'étude ne justifie de déléguer une partie de cette frontière à
Ralph : les trois briques différenciantes de ce projet (quota d'abonnement
multi-fenêtres, garantie de politique author≠reviewer, et maintenant
sélection adaptative de tier/profil) restent, comme documenté depuis la
Phase 0.5, hors du périmètre que Ralph couvre nativement.

## 4. Worker Registry

`Worker` reste l'**agent logique** (identité stable pour la gouvernance :
author≠reviewer, exclusions, priorité). Un nouveau champ optionnel
`enabled: bool = True` permet de désactiver un worker sans le retirer de la
config. Les modèles/efforts fixes actuels (`Worker.model`,
`Worker.reasoning_effort`) restent valides pour un worker à profil unique —
aucune rupture.

Chargement externe (Slice 15) : `config/workers.yaml`, répondant enfin à
l'AC-2 de `MVP_SPEC.yaml` (au moment de l'étude, aucun `WorkerRegistry`,
aucun loader YAML, aucun `config/` n'existait encore dans `src/` — cette
lacune a depuis été résolue par Slice 15, voir
`src/orchestrator/worker_registry.py`). Aucun nom de modèle n'est câblé en
dur dans le moteur ; aucun secret dans ce fichier (clés d'API/auth restent
gérées par l'environnement/les CLIs Claude Code et Codex eux-mêmes, jamais
dans `workers.yaml`).

```yaml
workers:
  - worker_id: victor
    display_name: Victor
    enabled: true
    provider: openai
    backend: codex
    capabilities: [development, code_review, release_planning, roadmap_synthesis, complexity_estimation]
    priority: 100
    profiles:
      economy:  { quality_tier: 1, model: gpt-5.6-mini,  reasoning_effort: low }
      standard: { quality_tier: 2, model: gpt-5.6-terra, reasoning_effort: medium }
      deep:     { quality_tier: 3, model: gpt-5.6-terra, reasoning_effort: high }
```

## 5. Execution Profiles

```
Worker            = agent logique/exécuteur (identité de gouvernance)
ExecutionProfile   = configuration concrète possible (quality_tier, model, reasoning_effort)
Execution          = snapshot réel utilisé (déjà existant, ExecutionRecord)
```

`ExecutionRecord` (Slice 5) **ne change pas de forme** : il continue de
capturer `worker_id`, `provider`, `backend`, `model`, `reasoning_effort`,
`role`. Un `ExecutionProfile` n'est qu'une entrée de configuration nommée ;
une fois sélectionné, il se traduit exactement dans les mêmes champs
`model`/`reasoning_effort` déjà présents. L'invariant de gouvernance réel
(inchangé depuis, voir `WorkerSelectionPolicy`) porte sur `worker_id` —
jamais sur `model`/`provider` : un reviewer ne peut jamais être le même
`worker_id` que l'auteur (obligatoire), un provider distinct est préféré
mais jamais requis par défaut (`require_distinct_provider_for_review=False`).

## 6. Quality tiers

Abstraction provider-agnostique, 4 niveaux (noms indicatifs) :

| Tier | Nom | Exemples |
|---|---|---|
| 1 | SIMPLE | changement mécanique, petit périmètre local, faible risque |
| 2 | STANDARD | développement normal, raisonnement modéré |
| 3 | COMPLEX | multi-module, architecture, persistence/concurrency, risque de régression élevé |
| 4 | CRITICAL | exceptionnel, très forte complexité/risque |

Principe : `tâche -> minimum_quality_tier -> profils dont quality_tier >=
minimum`. Un tier minimum n'est **jamais** silencieusement dégradé faute de
quota (voir §11).

## 7. Complexity pre-flight

```
WorkItem / action
    -> complexity estimator (Execution Ralph dédiée, capability
       complexity_estimation — jamais un nouveau mécanisme d'exécution)
    -> event structuré : execution.profile_recommended
    -> minimum_quality_tier + recommended_reasoning + reasons
    -> sélection finale Worker + ExecutionProfile (par l'orchestrateur)
```

Le pre-flight n'analyse que des faits déjà disponibles — jamais de
développement réel : objectif, acceptance criteria, dernier handoff,
contexte code pertinent, review findings éventuels, risques, portée du
changement. Contrat de sortie (mêmes contraintes fail-closed que
`planning._parse_planner_payload` : payload invalide/absent -> jamais
traité comme un succès) :

```json
{
  "complexity": "COMPLEX",
  "minimum_quality_tier": 3,
  "recommended_reasoning": "high",
  "reasons": ["cross-module change", "persistent state semantics", "high regression risk"]
}
```

L'estimator **recommande un niveau**, jamais un modèle concret
(`"utilise model XYZ"` est explicitement hors contrat) — voir §9.

## 8. Architecture estimator retenue

Comparaison des 4 options :

| Option | Coût | Circularité | Quota | Indépendance | Stabilité | Verdict |
|---|---|---|---|---|---|---|
| A. Estimator global léger (1 worker dédié, petit modèle) | faible | aucune | isolé (capability propre, quota séparé du developer) | oui | bonne (prompt étroit, tâche fermée) | **Retenu** |
| B. Estimator par provider | moyen | aucune | dupliqué (double surface de quota) | oui | bonne mais complexité inutile | rejeté (sur-ingénierie sans bénéfice observé) |
| C. Worker dédié avec capability `complexity_estimation` | faible | aucune | isolé | oui | bonne | équivalent à A — c'est la même chose formulée via le Worker Registry (Slice 15) |
| D. Le futur développeur s'auto-estime en profil économique | faible en apparence | **oui** (le développeur juge son propre périmètre avant de le traiter — proche du problème author=reviewer) | partagé avec le développement (concurrence de quota) | **non** | risque de sous-estimation systématique (biais d'auto-évaluation) | rejeté |

**Recommandation : Option A/C** (elles convergent) — un Worker dédié,
sélectionné via `WorkerSelector` par la capability `complexity_estimation`
exactement comme `release_planning`/`roadmap_synthesis` (Slice 12),
jamais un mécanisme parallèle. Une capability distincte permet d'ajouter un
provider supplémentaire (ex. Mistral) plus tard sans toucher au moteur :
il suffit qu'un nouveau worker déclare cette capability.

## 9. Séquence de sélection retenue

Comparaison des 3 options du prompt :

- **Option A** (`WorkerSelector` choisit un worker, qui s'auto-estime,
  *puis* choisit son profil) : circulaire — le `WorkerSelector` doit déjà
  savoir quel tier viser *avant* de sélectionner qui va exécuter le
  travail réel ; mélange le rôle estimator et le rôle développeur.
- **Option B** (estimation abstraite d'abord, puis `WorkerSelector`
  choisit worker+profile) : proche de la cible mais sous-spécifie *qui*
  produit l'estimation.
- **Option C** (sélection d'un estimator -> estimation -> sélection finale
  worker+profile) : explicite sur les deux sélections distinctes, chacune
  gouvernée par `WorkerSelector` avec sa propre capability requise.

**Confirmé : Option C.** C'est la seule qui n'introduit aucune circularité
(l'estimator n'est jamais le même rôle/Execution que le futur développeur),
reste compatible avec quota/coût (deux sélections indépendantes, chacune
peut échouer/attendre séparément via les mécanismes Slice 4/11 existants),
préserve l'indépendance reviewer (l'estimation d'un rôle n'influence jamais
le choix d'indépendance provider d'un autre rôle), s'intègre sans
modification au wait/recovery existant (une Execution d'estimation
interrompue suit exactement le même chemin `RECOVERY_REQUIRED`/`WAITING`
qu'une Execution de développement), et reste auditable (`ExecutionRecord`
existant capture l'estimation comme n'importe quelle autre Execution).

Séquence complète retenue :

```
WorkItem éligible
  -> WorkerSelector.select(capability=complexity_estimation)
  -> Execution estimator (RalphExecutionEngine, comme un planner)
  -> execution.profile_recommended { tier, reasoning, reasons }
  -> orchestrateur : profil le moins coûteux avec quality_tier >= minimum,
     parmi les workers enabled, capability requise, gouvernance ok,
     quota disponible
  -> WorkerSelector.select(capability=development, ...) contraint au tier
  -> Execution développement (comme aujourd'hui)
```

## 10. Qui décide ?

Principe non négociable, déjà énoncé dans le prompt et confirmé par
l'étude Ralph (aucun mécanisme Ralph ne doit jamais lancer un modèle non
configuré) : **l'estimator recommande, l'orchestrateur décide.** Avant de
lancer quoi que ce soit, l'orchestrateur vérifie : worker `enabled`,
capability requise, profil existant satisfaisant le tier, quota
disponible (`QuotaManager`, inchangé), policy (gouvernance/exclusions,
`WorkerSelector` inchangé), coût/priorité, indépendance reviewer
(`WorkerSelectionPolicy`, inchangé), disponibilité provider. Un événement
`execution.profile_recommended` invalide ou absent ne débloque jamais
l'exécution d'un profil arbitraire — comportement fail-closed identique à
`planning.py`/`review.py`.

## 11. Quota / coût

Ordre conceptuel retenu (identique à celui du prompt, confirmé cohérent
avec l'ordre de sélection déjà en vigueur dans `WorkerSelector` — capacité
> gouvernance > disponibilité > coût) :

```
1. capability requise
2. quality tier minimum
3. governance/policies (author != reviewer, exclusions)
4. disponibilité quota (QuotaManager, WorkerSelector existants)
5. coût/priorité
6. continuité du worker si pertinente (ex. rework sur le même work item)
```

Règle explicite : un tier minimum de 3 ne devient jamais silencieusement un
tier 1 parce que le tier 3 n'a plus de quota — l'orchestrateur cherche un
autre worker/profil au tier >= 3 ; si aucun n'est disponible, le WorkItem
suit le mécanisme `WAITING`/quota-recovery **déjà existant** (Slice 11,
`WaitCoordinator`), jamais un nouveau mécanisme d'attente dupliqué.

## 12. Development vs review (et autres rôles)

L'estimation est **role-specific** : development, review, planning et
roadmap synthesis d'un même WorkItem/release peuvent recevoir des tiers
différents (ex. development tier 2, review du même changement tier 3).
Aucun tier n'est automatiquement recopié d'un rôle à l'autre — chaque rôle
déclenche sa propre Execution d'estimation quand l'adaptive execution est
activée pour ce rôle (Slices 17/18 séparées précisément pour cette raison).

## 13. Fingerprint / cache

**Recommandé**, mais non implémenté dans cette étude. Fingerprint SHA-256
déterministe sur : `role` + `WorkItem` (titre, objectif, acceptance
criteria) + dernier `HandoffRecord` + `git_sha` courant + review findings
éventuels (si le rôle est review/rework) + toute autre entrée jugée
pertinente au moment de l'implémentation. Objectif : éviter un pre-flight
identique (même coût, même decision) quand rien de pertinent n'a changé
depuis la dernière recommandation pour ce couple (role, WorkItem).
Invalidation : dès que l'un des éléments d'entrée change (nouveau handoff,
nouveau SHA, nouvelles review findings) — jamais réutilisée au-delà, et
jamais comme substitut à une vérification de disponibilité/quota (le
fingerprint ne cache que la *recommandation de tier*, jamais la sélection
finale worker/quota qui reste toujours réévaluée à chaque tentative).

## 14. Impacts composants

| Composant | Impact |
|---|---|
| `Worker`/`ExecutionProfile` | Modification nécessaire (mineure, additive) : `Worker.enabled: bool = True` (Slice 15) ; `ExecutionProfile.cost_rank: int = 0` (Slice 17) — préférence économique **configurée**, jamais un prix réel, jamais inférée du modèle ; défaut `0` pour rester rétrocompatible (bascule alors sur la proximité de tier comme départage, déjà un choix par défaut raisonnable). |
| `WorkerSelector`/`WorkerSelectionRequest` | **Fait (Slice 17), extension minimale confirmée** : `WorkerSelectionRequest.minimum_quality_tier: QualityTier \| None` — un worker n'est candidat que s'il a au moins un profil `quality_tier >= minimum_quality_tier` ; ce filtre s'applique **avant** toute diagnose de quota (dans `_filter_by_capabilities_and_governance`), ce qui distingue gratuitement "aucun worker capable" (diagnostics vides, jamais transformé en `WAITING`) de "un worker capable existe mais son provider est en quota" (diagnostics normaux, `WAITING` inchangé). L'ordre de sélection existant (capacité > gouvernance > disponibilité provider > priorité) reste inchangé ; le choix du *profil* concret (cost_rank, proximité de tier, reasoning hint) reste entièrement dans `orchestrator.adaptive_execution`, jamais dans `WorkerSelector`. |
| `RalphExecutionEngine` | Aucune modification structurelle : `Worker.model`/`.reasoning_effort` restent les seuls champs consommés par `_build_backend_args`/`_render_hats_config` ; un `ExecutionProfile` résolu se traduit dans ces mêmes champs avant d'atteindre ce module. Invariant à préserver : ce module ne doit jamais apprendre la notion de "tier". |
| `MVPManager` | **Fait (Slice 17 pour development/rework, étendu Slice 19 pour review)** : paramètre optionnel `adaptive_execution_selector` (même patron que `quality_gate_runner`/`review_store`/`wait_store`) ; comportement Slice 7-14 inchangé si absent. Câblé via deux helpers privés symétriques — `_select_dev_worker` (DEVELOPMENT/REWORK, Slice 17) et `_select_reviewer_worker` (review, Slice 19). Les trois chemins qui résolvent un reviewer (frais via `_execute_work_item`, reprise `WaitPhase.REVIEW` via `_resume_review_wait`, reprise `RECOVERY_REQUIRED` via `_try_resume_recovery_required`) convergent déjà tous vers `_run_review`/`_run_review_from_handoff` — un seul point de câblage a donc suffi pour rendre les trois adaptatifs d'un coup, jamais `Worker.profile()` par défaut "parce que c'est une reprise". Le cache/fingerprint Slice 16 réutilise naturellement la recommandation existante quand rien n'a changé, en recalcule une nouvelle sinon. Aucune duplication de la logique wait/recovery existante : une erreur de pre-flight/sélection non diagnosable comme quota se propage simplement (fail-closed), une erreur diagnosable comme quota suit le `WAITING` existant sans changement. |
| Review orchestration (`review.py`) | **Non modifié.** `ReviewPolicy`/l'indépendance author≠reviewer restent la seule source de vérité, jamais contournées par un choix de profil — `AdaptiveExecutionSelector.select()` (Slice 19) forwarde `author_worker_id` à `WorkerSelector` exactement comme la sélection non adaptative le faisait déjà ; `ReviewRecord.__post_init__` (reviewer≠author) reste le filet de sécurité structurel final. |
| Planning orchestration (`planning.py`) | **Fait (Slice 19)** : deux dépendances optionnelles sur `PlanningCoordinator` (`execution_recommendation_service`, `adaptive_execution_decision_store`) ; comportement inchangé si absentes. `_select_planner`/`_select_synthesizer` gagnent `minimum_quality_tier` ; `AdaptiveExecutionSelector` n'est délibérément PAS composée telle quelle ici (elle ne fait qu'un seul appel `WorkerSelector.select()`, incompatible avec la boucle de diversité existante de `_select_planner`) — les trois primitives sous-jacentes (`ExecutionRecommendationService.estimate()`, le filtre `minimum_quality_tier`, `resolve_profile()`) sont composées directement, une seule `AdaptiveExecutionDecision` persistée par worker réellement sélectionné. Invariant préservé : `PlanningPolicy.planner_count`/indépendance des planners/diversité inchangés. |
| `ExecutionRecord` | **Aucune modification** — capture déjà `worker_id`/`provider`/`backend`/`model`/`reasoning_effort`/`role`. |
| `QuotaManager` | **Aucune modification** — reste l'unique source de vérité de disponibilité, jamais dupliquée par l'adaptive execution. |
| `WaitCoordinator` | **Aucune modification structurelle** — une Execution d'estimation ou de développement contrainte par tier qui échoue faute de quota suit le mécanisme `WAITING` existant tel quel. |
| `RecoveryCoordinator` | **Aucune modification** — une Execution d'estimation orpheline/interrompue est un `ExecutionRecord` comme un autre, déjà couvert. |
| `RoadmapApplicationService` | **Aucune modification, invariant fort à préserver** : ce service ne doit jamais connaître les noms de modèles, tiers, reasoning, ni Claude/Codex — il continue de déléguer entièrement au moteur d'orchestration (`MVPManager.run_next_work_item`, déjà le cas depuis Slice 14) sans rien savoir de comment ce WorkItem sera exécuté. |

## 15. Configuration : workers vs policies

Séparation retenue (aucun fichier de config n'existe encore dans ce dépôt —
vérifié : ni `config/`, ni chargeur YAML dans `src/`) :

- `config/workers.yaml` — agents et profils disponibles (Worker Registry,
  §4).
- `config/orchestrator.yaml` — règles d'utilisation (policies) :

```yaml
review:
  require_distinct_worker: true
  prefer_distinct_provider: true
planning:
  planner_count: 2
adaptive_execution:
  enabled: true
  use_preflight: true
approval:
  timeout_minutes: 20
  auto_approve_on_timeout: true
```

Format : **YAML**, cohérent avec les dépendances actuelles — `pyproject.toml`
ne déclare aucune dépendance de parsing aujourd'hui (`dependencies = []`),
donc un lecteur YAML (`PyYAML`) serait la première dépendance runtime du
projet. Alternative stdlib pure : `tomllib` (Python 3.11+, lecture seule) ou
JSON — les deux sont plus verbeux/moins lisibles à la main pour des humains
éditant `workers.yaml`/`orchestrator.yaml`, et l'exemple conceptuel du
prompt lui-même est écrit en YAML. **Décision : YAML**, avec `PyYAML` comme
nouvelle dépendance minimale explicite (à ajouter dans `pyproject.toml` au
moment de l'implémentation, Slice 15) — pas de solution stdlib pure
satisfaisante pour de l'écriture humaine confortable.

## 16. Capabilities

Recensées aujourd'hui dans `src/orchestrator/` : `"reviewer"`
(`mvp_manager.REVIEW_CAPABILITY`), `"release_planning"`/`"roadmap_synthesis"`
(`planning.py`), et des chaînes arbitraires sur `WorkItem.required_capabilities`
(ex. `"developer"` dans les tests — jamais une constante figée aujourd'hui,
c'est la proposition/le créateur du WorkItem qui la choisit librement).

Liste minimale cible (Slice 15+, à ne pas multiplier au-delà du besoin
observé) :

- `development`
- `code_review`
- `release_planning`
- `roadmap_synthesis`
- `complexity_estimation`

Note : harmoniser `"reviewer"` -> `code_review` et une convention stable
pour le développement (`"developer"` vs `development`) est un renommage de
constantes existantes, donc un changement fonctionnel — hors périmètre de
cette session documentaire, à traiter explicitement en Slice 15 si retenu.

## 17. Décisions fermes

- Worker = agent logique ; ExecutionProfile = configuration concrète ;
  Execution/ExecutionRecord inchangés dans leur forme.
- Ralph ne fait et ne fera jamais le choix adaptatif modèle/tier/quota —
  confirmé par preuves (§2), pas supposé.
- Séquence de sélection = Option C (estimator sélectionné et exécuté
  indépendamment, puis sélection finale worker+profile par
  l'orchestrateur).
- Estimator = un Worker dédié via capability `complexity_estimation`,
  jamais l'auto-estimation par le futur développeur.
- Un tier minimum n'est jamais dégradé silencieusement faute de quota.
- `RoadmapApplicationService` ne doit jamais connaître modèles/tiers/
  reasoning/providers.
- Découpage retenu : Slices 15 (Registry+Profiles) -> 16 (estimator) -> 17
  (intégration development) -> 18 (intégration review/planning) -> 19
  (Git/PR/merge governance, décalée).
- Configuration : `config/workers.yaml` + `config/orchestrator.yaml`,
  format YAML, `PyYAML` comme nouvelle dépendance minimale.

**Précision issue de l'implémentation réelle de Slice 15** (petit écart de
détail avec le texte ci-dessus, jamais avec les décisions fermes) :
`default_profile_id` n'est **jamais optionnel de fait** — auto-résolu
seulement quand un Worker ne déclare qu'un seul profil (aucune ambiguïté
possible), et strictement requis explicitement dès qu'il en déclare
plusieurs (jamais deviné parmi plusieurs candidats). Un
`Worker.with_single_profile(...)` (classmethod) a été ajouté comme
constructeur de convenance pour le cas mono-profil — utile en pratique
pour la migration des fixtures de test existantes, et pour tout worker
qui n'a réellement qu'une seule configuration possible.

## 18. Questions réellement ouvertes

Aucune question de ce type restante à ce stade (Slice 19) — voir les deux
entrées « Résolu par Slice 18.5 »/« Résolu par Slice 19 » ci-dessous.

**Résolu par Slice 19** (déplacé hors des questions ouvertes) :
- Étendre l'adaptive selection à la reprise **review** : fait —
  `_select_reviewer_worker` (voir §14), câblé une seule fois dans
  `_run_review`/`_run_review_from_handoff`, déjà partagés par les trois
  chemins (frais, reprise WAITING, reprise RECOVERY).
- Forme de l'extension pour porter `author_worker_id` à travers la couche
  adaptative : un paramètre optionnel `author_worker_id: str | None = None`
  sur `AdaptiveExecutionSelector.select()`, forwardé tel quel à
  `WorkerSelectionRequest.author_worker_id` — rétrocompatible (défaut
  `None`, le chemin development ne le passe jamais), et c'est la manière
  la plus petite de réactiver la politique cross-provider déjà existante
  sans la dupliquer.
- Planning/synthesis : les deux confirmées être de vraies exécutions LLM
  (`release_planning`/`roadmap_synthesis`, déjà réelles depuis Slice 12,
  jamais une synthèse déterministe dans ce dépôt) — CAS B partout.
  `AdaptiveExecutionSelector` volontairement non réutilisée telle quelle
  pour le planner (sa boucle de diversité n'est pas un simple `select()`
  unique) ; ses trois primitives sont composées directement dans
  `planning.py` à la place (voir §14).

**Résolu par Slice 18.5** (déplacé hors des questions ouvertes) :
- Renommage des capabilities existantes (`"reviewer"` -> `code_review`) :
  fait — `code_review` est désormais l'unique capability canonique pour
  le rôle review (`MVPManager.REVIEW_CAPABILITY`), `"reviewer"` reste
  uniquement le nom du rôle logique (`REVIEWER_ROLE`), jamais les deux
  comme synonymes.

**Résolu par Slice 17** (déplacé hors des questions ouvertes) :
- Forme de l'extension de `WorkerSelectionRequest` : un champ
  `minimum_quality_tier: QualityTier | None`, filtré avant toute diagnose
  de quota (voir §14) — pas de requête séparée, c'est la plus petite
  évolution qui préserve l'ordre de sélection existant.
- `cost_rank` : ajouté sur `ExecutionProfile` (int, défaut 0, validé
  non-négatif dans `WorkerRegistry`) — préférence économique configurée,
  jamais un prix réel.

**Résolu par Slice 16** (déplacé hors des questions ouvertes) :
- Fingerprint : implémenté (`compute_task_fingerprint`,
  `src/orchestrator/complexity_estimation.py`) — sha256 canonique sur
  `role`/`project_id`/`mvp_id`/`work_item_id`/`objective`/
  `acceptance_criteria`/`git_sha`/le contenu pertinent du dernier
  `HandoffRecord`/les `ReviewFinding` pertinents, plus un
  `contract_version` explicite pour invalider tout fingerprint passé si ce
  contrat change un jour.
- Persistence : une seule table (`ExecutionRecommendationStore`/
  `ExecutionRecommendation`) s'est avérée suffisante — pas
  d'`ExecutionProfileDecision` séparée, exactement comme anticipé ; cette
  seconde table reste pertinente pour Slice 17 (la décision finale
  worker+profile, distincte de la recommandation).
- Validation `estimator_profile_id` (Slice 15 avait délibérément laissé la
  question ouverte) : tranchée en faveur d'un contrôle **au niveau du
  service** (`EstimatorProfileNotConfiguredError` dans
  `ExecutionRecommendationService.estimate`), jamais dans
  `WorkerRegistry` — la sémantique du nom de capability
  `complexity_estimation` appartient au module qui la consomme, jamais à
  `WorkerRegistry`, qui reste entièrement générique (aucune capability
  n'y est jamais nommée), symétriquement à `WorkerSelector` qui ne nomme
  jamais un provider concret.
- Sandbox/permissions Codex sous adaptive execution : héritage direct du
  `NOT VALIDATED` déjà noté dans `docs/SPIKE_RALPH.md` — toujours non
  bloquant, toujours à re-tester avant toute hypothèse de sécurité.
