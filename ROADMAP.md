# ROADMAP — AI Dev Orchestrator

Ce document est la **source de vérité** du projet. Il doit permettre à n'importe
quel agent (Claude, Codex, autre) de reprendre le travail avec un minimum de
contexte, sans dépendre de la mémoire d'une conversation précédente.

Ne pas se fier à un historique de conversation : se fier à ce fichier, à
`MVP_SPEC.yaml`, à `docs/ECOSYSTEM.md`, et à l'état du dépôt (code, tests,
`git log`).

## Vision

Un orchestrateur léger, piloté par MVP, qui réutilise des outils existants
(Claude Code, Codex CLI, Ollama, Git, GitHub) au lieu de les recréer. Il
découpe un MVP en tâches, choisit le meilleur worker disponible pour chaque
tâche (coût, quota, spécialisation), impose une revue indépendante du code,
et persiste son état pour reprendre après interruption.

## Vision cible du produit

Principe central (ajouté après Slice 6, affiné à chaque itération) :

```
UN PROJET
+ UNE ROADMAP VERSIONNÉE
+ UN ÉTAT D'EXÉCUTION PERSISTANT
+ DES AGENTS IA INTERCHANGEABLES
```

L'orchestrateur doit être capable de prendre un projet logiciel et de
piloter progressivement sa roadmap jusqu'aux releases successives, avec des
agents IA (Claude, Codex, puis d'autres) traités comme des workers
interchangeables — jamais comme des rôles figés.

Cycle cible (vision long terme ; chaque étape correspond à une ou
plusieurs slices ci-dessous, certaines encore non construites) :

```
ROADMAP
  ↓
MVP courant
  ↓
WorkItems
  ↓
sélection du worker IA (WorkerSelector)
  ↓
exécution via Ralph (RalphExecutionEngine)
  ↓
handoff durable
  ↓
tests
  ↓
code review indépendante (author != reviewer)
  ↓
corrections éventuelles
  ↓
quality/release gate
  ↓
release
  ↓
rapport d'activité
  ↓
analyse indépendante de plusieurs agents IA
  ↓
synthèse de la suite (diff de roadmap proposé)
  ↓
notification utilisateur
  ↓
fenêtre de veto de 20 minutes
  ↓
si aucune réponse : approbation automatique
  ↓
mise à jour roadmap
  ↓
MVP suivant
  ↓
cycle suivant
```

Ce cycle est la cible produit. Il ne décrit pas l'état actuel du code : voir
« État actuel » plus bas pour ce qui est réellement construit, et le
« Découpage incrémental » (sous Phase 1) pour l'ordre de construction retenu.

**Statut : OPTIONAL, jamais auto-déclenché (2026-09-17).** Les briques de
ce cycle long terme (`PlanningCoordinator`, `ApprovalCoordinator`,
`ReleaseManager`) sont construites et testées, mais `MVPManager` ne les
référence nulle part dans son constructeur ni dans `run_next_work_item` —
rien ne les enchaîne automatiquement après la complétion d'un WorkItem ou
d'un MVP, `LEAN_FEATURE_FLOW` en particulier n'en déclenche aucune. Ce sont
des capacités disponibles, à invoquer explicitement par un appelant, pas
un pipeline par défaut. Elles restent `OPTIONAL` — ni dépréciées, ni
supprimées — pour un usage futur (release/synthèse multi-agent réelle),
sans capacité nouvelle prévue tant qu'un besoin concret ne le justifie
(KISS/YAGNI).

**Important — politique d'approbation optimiste (20 minutes)** : cette
politique de silence = approbation ne concerne QUE la gouvernance
roadmap/MVP (passage automatique au MVP suivant si personne ne répond à une
proposition de synthèse). Elle ne signifie EN AUCUN CAS que l'absence de
réponse autorise une action destructive, financière ou sensible ailleurs
dans le système. Elle devra rester configurable (délai, activation).
Non implémentée avant Slice 13 — voir plus bas.

### Chemin nominal actuel d'un WorkItem — `LEAN_FEATURE_FLOW` (DEFAULT)

Ceci est le chemin réellement exécuté aujourd'hui pour un WorkItem, tel
qu'implémenté par `MVPManager` (`workflow_mode` par défaut
`WorkflowMode.LEAN_FEATURE_FLOW`, décision produit du 2026-09-16) — à ne
pas confondre avec le cycle MVP/release long terme ci-dessus :

```
ROADMAP / WorkItem
      ↓
WorkerSelector (capability > gouvernance > disponibilité provider > priorité)
      ↓
DEV A
      ↓
DEV B — corrective review (2e développeur indépendant,
        pas en lecture seule : il corrige et committe directement)
      ↓
QA déterministe, unique, en lecture seule (QAPhase.FINAL_VERIFICATION
réutilisée telle quelle — pas de QA Test Authoring séparée, pas de
Final QA supplémentaire, pas d'estimation adaptative obligatoire)
      ↓
   PASS ────────────────────────────────► merge (gouverné, SHA-pinné)
      │                                       ↓
   FAIL                                      tag (`feature/<work-item-id>/done`)
      ↓                                       ↓
   DEV FIX → QA (jusqu'à 3 tentatives QA      DONE
   au total, `QAPolicy.max_qa_cycles`)
      ↓ (3e FAIL)
   HUMAN_REVIEW_REQUIRED
   (`BLOCKED` + TODO déterministe ajouté au ROADMAP.md du projet cible,
   commité sur sa branche de travail ; les autres WorkItems indépendants
   continuent normalement)
```

Invariants de gouvernance de ce chemin (`WorkerSelectionPolicy`, inchangés
depuis leur introduction — voir `src/orchestrator/worker_selector.py`) :

- **`DEV_B.worker_id != DEV_A.worker_id` — REQUIRED**, non désactivable
  (`require_distinct_worker_for_review=True`, épinglé).
- **`DEV_B.provider != DEV_A.provider` — PREFERRED, jamais REQUIRED par
  défaut** (`prefer_distinct_provider_for_review=True`,
  `require_distinct_provider_for_review=False`) : un repli sur un second
  worker du même provider reste toujours valide plutôt que de bloquer.
- QA (chemin Lean) n'utilise aucun `WorkerSelector`, ne sélectionne et
  n'exécute aucun agent/worker IA : elle réutilise directement la
  validation déterministe existante (`QAEngine.run`,
  `QAPhase.FINAL_VERIFICATION`), sur le SHA exact du WorkItem, working
  tree clean, en lecture seule — c'est la dernière preuve exécutable avant
  merge, jamais une troisième opinion LLM. Jamais concernée par la
  disponibilité d'un provider.

**Nominal LLM executions: DEV A + DEV B only.** Le chemin heureux consomme
exactement 2 exécutions LLM (DEV A, DEV B) — la QA n'en consomme aucune.
`LLM IS NOT ORACLE` : le second regard IA est DEV B (corrective review,
write-capable) ; la QA finale apporte une preuve exécutable déterministe,
pas une troisième opinion LLM systématique.

**Invariant de continuité (2026-09-17)** :

> *Provider exhaustion must never produce WAITING while another compatible
> worker on an AVAILABLE provider exists.*
>
> Un quota provider épuisé ne doit jamais provoquer `WAITING` si un autre
> worker compatible sur un provider disponible peut poursuivre. `WAITING`
> n'intervient que si aucun worker éligible n'existe actuellement et qu'au
> moins un provider candidat est diagnosticable comme `quota_exhausted`
> avec un `reset_at` connu (`WaitCoordinator.record_wait`) ; `BLOCKED`
> intervient quand aucune capacité structurellement compatible n'existe
> (jamais un `WAITING` fabriqué sans deadline plausible).

**Worker pool — recommandation actuelle** : au moins **2 workers
indépendants par provider participant** (`config/workers.yaml`), pour que
l'exclusion `DEV B != DEV A` puisse toujours se satisfaire sans dépendre
de la disponibilité de l'*autre* provider. Aujourd'hui : `alice`/`bob`
(anthropic), `victor`/`oscar` (openai) — mêmes capabilities/profils que
leur worker primaire, priorité inférieure (90 vs 100) pour que la
sélection sans auteur préfère naturellement le worker primaire quand tous
les providers sont disponibles. Important : **plusieurs workers d'un même
provider ne créent pas plusieurs quotas provider** — `QuotaManager`
continue d'interroger un seul état par nom de provider ; ces workers
représentent uniquement plusieurs identités d'exécution indépendantes
partageant le même quota sous-jacent.

**Adaptive execution** (Slices 15-19, `docs/ADAPTIVE_EXECUTION.md`) :
`WorkerSelector`/`AdaptiveExecutionSelector`/`resolve_profile()`/
l'estimation de complexité restent disponibles et inchangés, mais
`LEAN_FEATURE_FLOW` ne dépend d'aucun d'eux sur son chemin nominal —
aucune nouvelle sophistication adaptative n'est à ajouter sans besoin réel
prouvé (voir « KISS/YAGNI » ci-dessous).

**`GOVERNED_FULL`** (l'ancien pipeline : estimation adaptative avant
chaque phase, QA Test Authoring isolée, Review isolée en lecture seule,
Final QA séparée) : **`DEPRECATED` / `REMOVAL_CANDIDATE`**. Reste
sélectionnable explicitement (`workflow_mode=WorkflowMode.GOVERNED_FULL`),
sa suite de tests reste verte, seules des régressions critiques y seront
corrigées ; aucune nouvelle capacité n'y est ajoutée. Suppression évaluée
plus tard, après suffisamment de recul sur `LEAN_FEATURE_FLOW`.

## Responsabilités (ne pas confondre)

Chaque composant a une responsabilité unique et ne doit jamais empiéter sur
celle d'un autre :

- **`ROADMAP.md`** = direction fonctionnelle du projet (MVPs, objectifs,
  décisions structurantes). Jamais un journal d'exécution machine.
- **Project/MVP persistent state** (Slice 7+) = état de l'orchestration
  haut niveau (MVP courant, WorkItems, statuts). Jamais une seconde file de
  tâches fine — Ralph garde sa propre orchestration interne une fois un
  WorkItem délégué.
- **`ExecutionStore`** (Slice 5) = vérité des exécutions machine
  (identité, statut, exit_code, SHA git, timestamps). Ne devient jamais un
  god object : un `HandoffStore`/`ProjectStateStore` séparé porte les
  responsabilités qui ne sont pas de l'audit d'exécution brut.
- **Handoff** (Slice 7) = contexte durable nécessaire pour reprendre le
  travail sans dépendre de la mémoire conversationnelle d'un LLM
  précédent. Construit à partir de faits persistants (ExecutionRecord,
  events métier, SHA git), jamais seulement d'un résumé libre du LLM.
- **Activity Report** (Slice 10) = historique intelligible de ce qu'a fait
  l'orchestrateur (MVP/WorkItem, workers, executions, tests, reviews,
  décisions, changements de roadmap). Aucun secret stocké.
- **Ralph** (`RalphExecutionEngine`, Slice 6) = exécution fine des
  workers/workflows. Jamais réimplémenté, jamais doublé par un second
  scheduler.
- **`WorkerSelector`** (Slice 4) = choix du worker (capability >
  gouvernance > disponibilité provider > priorité). Jamais dupliqué
  ailleurs ; `cost_rank` n'intervient jamais ici — il appartient à la
  sélection de l'`ExecutionProfile` dans `orchestrator.adaptive_execution`.
- **`QuotaManager`** (Slice 3) = disponibilité provider (cache/fraîcheur
  au-dessus des `ProviderAdapter`). Jamais interrogé directement par une
  couche d'orchestration haut niveau — toujours via `WorkerSelector`.

## Principes directeurs (valables à toutes les phases)

1. **Ne pas recréer** Codex, Claude Code, Ollama, Git ou GitHub : les piloter
   en subprocess / CLI / API, jamais réimplémenter leur logique.
2. **Local et gratuit d'abord**, puis quotas d'abonnement, puis API payantes
   en dernier recours.
3. **Aucun état important en mémoire seule** : tout ce qui doit survivre à un
   redémarrage vit en SQLite.
4. **Un agent qui a écrit du code ne se relit jamais lui-même** : la
   contrainte Developer ≠ Reviewer doit être vérifiable à partir de l'historique
   des exécutions, pas d'un mécanisme ad hoc.
5. **Configuration déclarative (YAML), état observé en base (SQLite)** : ne
   jamais mélanger les deux.
6. **Étendre sans réécrire** : chaque phase ajoute un adaptateur ou un module,
   sans modifier le cœur (moteur, modèles de données, routage).
7. **Une exécution interrompue n'est jamais rejouée aveuglément** : persister
   l'état en SQLite ne suffit pas à garantir une reprise sûre — un worker
   externe peut avoir produit des effets avant l'interruption. Toute reprise
   passe par une détection explicite de l'incident et une étape de
   réconciliation, même minimale.
8. **Le moteur ne décide jamais de stratégie Git** : toute décision de
   branche/merge/tag appartient exclusivement à `GitGovernanceService`/
   `LocalGitWorkspace`, jamais à `RalphExecutionEngine` ni aux workers.
   Exception assumée et documentée (AC-12 de `MVP_SPEC.yaml`) :
   `RalphExecutionEngine` peut effectuer une lecture Git minimale et
   strictement en lecture seule (`git rev-parse HEAD`) pour capturer un
   fait d'audit SHA — jamais une opération d'écriture, jamais un choix de
   stratégie. Une abstraction `Workspace` générique et interchangeable
   n'est pas actuellement implémentée ; la stratégie concrète (branche
   locale, GitHub, worktree, PR) reste néanmoins centralisée dans une
   couche séparée du moteur.
9. **REUSE FIRST** : avant l'implémentation d'une nouvelle capability,
   rechercher et documenter les implémentations existantes. Préférer
   reuse > adaptation > développement spécifique lorsque la qualité, la
   licence et le coût d'intégration le permettent. Ne pas recréer un
   orchestrateur déjà disponible sans raison. Voir `docs/ECOSYSTEM.md`.
10. **KISS / YAGNI** : préférer le plus petit changement qui satisfait le
    besoin prouvé. Ne pas ajouter une abstraction, un store, un routeur,
    un agent ou une phase tant qu'un besoin concret ne le justifie (voir
    la décision produit `LEAN_FEATURE_FLOW`, section « Chemin nominal
    actuel » ci-dessus, et l'arbitrage worker↔provider dans « État
    actuel »).

## Phases

### Phase 0 — Spécification (DONE)

Objectif : aligner le besoin, l'architecture et les critères d'acceptation
avant d'écrire du code.

Livrables :
- `ROADMAP.md` (ce fichier)
- `MVP_SPEC.yaml` (critères d'acceptation mesurables du MVP 0.1)
- Arborescence cible validée
- Décisions d'architecture et risques documentés

Sortie de phase : validation explicite de l'utilisateur.

### Phase 0 — Ecosystem Study / Build-vs-Reuse Gate

Objectif : appliquer le principe REUSE FIRST — apprendre des projets open
source comparables, identifier ce qui peut être réutilisé/adapté tel quel, et
ne développer nous-mêmes que ce qui manque réellement. Répondre à la question
« qu'avons-nous réellement besoin d'implémenter ? » avant d'écrire du code.

Cette phase porte le même numéro que la phase de spécification initiale
(les deux font partie du travail de préparation avant tout code) ; elle est
**bloquante** : la Phase 1 ne doit pas commencer tant qu'elle n'est pas
validée par l'utilisateur.

Livrables :
- `docs/ECOSYSTEM.md` — étude de 12 projets open source comparables (famille
  Ralph, OpenHands, LiteLLM, LangGraph, Microsoft Agent Framework, PR-Agent,
  MetaGPT/ChatDev, SWE-agent/mini-swe-agent, Aider), avec pour chacun :
  licence, maturité, fonctionnalités pertinentes, ce qui est réutilisable
  précisément, ce qui n'est qu'une source d'inspiration, ce qui manque, coût
  d'intégration, risques de dépendance, et une décision provisoire
  (`USE`/`ADAPT`/`INSPIRE`/`WATCH`/`REJECT`)
- Matrice **BUILD / REUSE / ADAPT / DEFER** par composant prévu de notre
  architecture (MVP Manager, Task model, Task scheduler, SQLite persistence,
  Worker registry, Worker adapters, WorkerSelector, QuotaManager,
  recovery/reconciliation, Workspace abstraction, Git/GitHub, PR workflow,
  Reviewer, Quality Gates) — voir `docs/ECOSYSTEM.md`
- Décision explicite sur `mikeyobrien/ralph-orchestrator` (le concurrent
  conceptuel le plus proche) : continuer notre projet indépendant en
  reprenant certains de ses patterns de conception, sans le forker ni
  construire dessus (voir `docs/ECOSYSTEM.md`, section 1, « Build vs Reuse »)

Conclusion de la gate (établie par lecture de code, à confirmer
expérimentalement — voir Phase 0.5 ci-dessous) : aucun projet étudié ne
couvre notre différenciateur central (QuotaManager multi-fenêtres pour
quotas d'abonnement CLI, **garantie de politique** — pas seulement
configurabilité — Developer.model != Reviewer.model, intégration GitHub PR
via `gh`/API) — le périmètre du MVP 0.1 reste inchangé. Un ajustement concret
est retenu pour la Phase 2 : réutiliser le CLI `gh` (déjà installé/
authentifié) pour piloter GitHub plutôt que d'écrire un client API GitHub
maison — confirmé après vérification directe du code source de Ralph
Orchestrator, qui n'a lui-même aucune intégration `gh`/API GitHub malgré un
workflow de « remote review » opérationnel (voir `docs/ECOSYSTEM.md`,
section 1).

Sortie de phase : validation explicite de l'utilisateur.

### Phase 0.5 — Reuse Spike (DONE)

Objectif : valider par expérimentation réelle les briques REUSE candidate,
particulièrement Ralph Orchestrator, avant de démarrer Phase 1.

Résultats documentés dans `docs/SPIKE_RALPH.md`.

#### Conclusions principales

- **Ralph 2.10.1 : REUSE comme moteur d'exécution/workflow**
  - Boucle d'exécution complète (itération, timeouts, metrics, handoff)
  - Workflows pré-construits : `builtin:code-assist` (Planner→Builder→Critic→Finalizer)
  - Preset de review : `builtin:review` (Reviewer→Analyzer→Closer)
  - Backends multiples et sélection par hat
  - **⚠️ Limitation critique** : pas de fallback intelligent si un hat échoue

- **Télémétrie des quotas : REUSE providers natifs**
  - Claude Code : `stream-json` retourne directement les fenêtres de quota
  - Codex : app-server expose `account/rateLimits/read` structuré
  - Multi-fenêtres validées (5h + 7j pour Claude, 5h + 7j pour Codex)

- **AI Dev Orchestrator = couche de gouvernance + sélection**
  - Ne pas reconstruire l'event loop, les hats, la revue, le TDD cycle
  - Construire : Provider Adapters légers, QuotaManager multi-fenêtres,
    Worker Selector (capability > governance > quota > cost),
    Author ≠ Reviewer enforcement, fallback explicite

- **Matrice revisitée** (voir `docs/SPIKE_RALPH.md`, tableau final)
  - Task scheduler → `REUSE` Ralph complet
  - Planner/Builder/Critic/Finalizer → `REUSE` code-assist
  - Review workflow → `REUSE` review preset
  - Quotas Claude/Codex → `REUSE` APIs natives
  - QuotaWindow multi-fenêtres → `BUILD` (Ralph ne le gère pas)
  - Author ≠ Reviewer → `BUILD` (gouvernance, Ralph est logique)
  - Worker selection → `BUILD` (WorkerSelector, ordre strict)
  - Provider adapters → `BUILD` légers (normalisation)

Sortie de phase : validée, prête pour Phase 1.

### Phase 1 — MVP 0.1 : gouvernance + sélection + Ralph integration

**Architecture** : AI Dev Orchestrator est une couche mince de gouvernance
et de sélection au-dessus de Ralph Orchestrator (moteur d'exécution réutilisé).

Objectif : mettre en place les briques **différenciantes** — gestion des
quotas d'abonnement CLI, sélection intelligente des workers, gouvernance
stricte (Author ≠ Reviewer enforcement) — sans reconstruire l'event loop,
les hats, la revue ou le cycle TDD que Ralph fournit déjà.

**Composants à construire (REUSE FIRST appliqué), ordre imposé** :

**1. Contrats normalisés — ✅ DONE (Slice 0)**
- **ProviderState, ProviderAvailability, QuotaWindow, ResetCredit** (data classes
  immuables, `src/orchestrator/providers/contracts.py`)
  - `observed_at` timezone-aware obligatoire à chaque niveau pertinent
  - `quota_windows` : collection, jamais un `reset_at` unique au niveau `ProviderState`
  - `utilization` : `None` (inconnu) distinct de `0.0` (mesuré), jamais coercé
  - `ResetCredit.auto_consume` figé à `False` (invariant de type, pas de `consume()`)
  - Tests : `tests/providers/test_contracts.py` (offline, indépendants de tout provider)

**2. Interface ProviderAdapter — ✅ DONE (Slice 0)**
- Signature minimale : `probe() → ProviderState` (`src/orchestrator/providers/adapter.py`)
- Garanties : jamais d'exécution (pas `codex exec` dans probe) — ne doit
  **jamais** devenir un moteur d'exécution, l'exécution appartient à
  RalphExecutionEngine (étape 9)
- Tests : `tests/providers/test_adapter.py`

**3. ClaudeCodeAdapter — ✅ DONE (Slice 1)**
- Parse stream-json des quotas natifs (`src/orchestrator/providers/claude_code_adapter.py`)
- Retourne ProviderState normalisé (fenêtres `five_hour`/`seven_day`, jamais
  de `reset_at` unique)
- Sous-processus borné : timeout, exit code non-zéro, JSONL invalide/
  incomplet → erreurs normalisées (`ClaudeProbeTimeout`, `ClaudeProcessError`,
  `ClaudeStreamParseError`), jamais silencieux
- Règle documentée pour plusieurs `rate_limit_event` dans un même flux : le
  dernier observé fait autorité
- Fixture réelle capturée une seule fois (`tests/providers/fixtures/
  claude_stream_allowed.jsonl`, nettoyée), tests 100% offline sinon
- Tests : `tests/providers/test_claude_code_adapter.py`

**4. CodexAdapter — ✅ DONE (Slice 2)**
- Échange JSON-RPC borné sur `codex app-server` (`src/orchestrator/
  providers/codex_adapter.py`) : `initialize` → `initialized` →
  `account/rateLimits/read` uniquement, jamais `account/usage/read` ni
  `account/rateLimitResetCredit/consume` — probe strictement read-only
- Retourne ProviderState normalisé (fenêtres `primary_5h`/`secondary_7d`,
  jamais de `reset_at` unique) ; `ordinaryUsageAllowed` est le signal
  canonique d'availability (`true`→AVAILABLE, `false`→QUOTA_EXHAUSTED,
  absent/incohérent→UNKNOWN)
- Reset credits normalisés descriptivement, `auto_consume` toujours `False`,
  aucune méthode de consommation exposée par l'adapter
- Échange borné par un timeout global unique ; sous-processus terminé
  proprement dans tous les cas (succès, erreur, timeout) ; erreurs
  normalisées (`CodexProbeTimeout`, `CodexProcessError`, `CodexProtocolError`)
- Fixture réelle capturée une seule fois via une lecture strictement
  read-only (`tests/providers/fixtures/codex_rate_limits_allowed.json`,
  nettoyée), tests 100% offline sinon
- Tests : `tests/providers/test_codex_adapter.py`

**5. Fixtures capturées + tests offline**
- Fixtures issues des captures du spike (stream-json Claude, app-server Codex)
- Valident les contrats et adapters sans appel réseau réel avant intégration

**6. QuotaManager — ✅ DONE (Slice 3)**
- Couche de cache/fraîcheur autour des `ProviderAdapter` existants
  (`src/orchestrator/quota_manager.py`) — ne parle jamais directement à
  Claude/Codex, uniquement à `ProviderAdapter.probe()`
- `QuotaPolicy(state_ttl: timedelta)` injectable, validée (TTL positif
  obligatoire) ; clock injectable et vérifiée timezone-aware à chaque appel
- `get(provider)` : sert le cache si frais, probe sinon ; **si le probe
  échoue, propage toujours `ProviderProbeError`** — que le cache soit
  absent ou expiré, jamais de repli silencieux sur un ancien état, même
  inchangé/non réétiqueté (STALE != USABLE FOR ROUTING ; vérifié dans le
  code réel et par `tests/test_quota_manager.py::
  test_expired_cache_with_failing_probe_raises_and_never_returns_stale_available`)
- `refresh(provider)` : probe toujours forcé, ne substitue jamais
  silencieusement un ancien état — échoue explicitement si le probe échoue
- `refresh_all()` : fan-out concurrent de `refresh()` sur tous les
  providers connus, sans logique de sélection
- Probes concurrents dédupliqués par provider (single-flight `asyncio.Task`)
- `quota_windows`/`reset_credits` transmis tels quels (jamais réduits à un
  `reset_at` unique, jamais consommés) ; erreurs domaine minimales
  (`UnknownProviderError`, `ProviderProbeError`)
- Ne classe pas WAITING_RESET/INTERRUPTED/RECOVERY_REQUIRED (états futurs
  de l'orchestrateur/exécution, hors périmètre de cette couche)
- Tests : `tests/test_quota_manager.py` (100% offline, fake adapters)

**7. WorkerSelector — ✅ DONE (Slice 4)**
- `Worker` typé (`src/orchestrator/worker_selector.py`) : `worker_id`
  (identité technique stable), `display_name` (jamais utilisé pour une
  décision de gouvernance), `provider`, `backend`, `model`,
  `reasoning_effort` optionnel, `capabilities: frozenset[str]` (rôle =
  capability, ex. `"developer"`/`"reviewer"`), `priority: int`
- Aucun provider n'est intrinsèquement author-only/reviewer-only ; Claude
  et Codex ne sont jamais nommés dans la logique du sélecteur (vérifié par
  test) — seule la config des workers les nomme
- Ordre de filtrage : capabilities requises → exclusions de gouvernance
  (author != reviewer + exclusions explicites) → provider `AVAILABLE` via
  `QuotaManager.get()` (fail-closed : tout sauf `available=True` exclut,
  y compris une erreur de probe) → priorité (tie-break lexical sur
  `worker_id`, documenté et déterministe)
- `WorkerSelectionPolicy` : `require_distinct_worker_for_review` figé à
  `True` (invariant, jamais désactivable) ; `prefer_distinct_provider_for_review`
  (soft, repli same-provider si nécessaire) ; `require_distinct_provider_for_review`
  (hard, lève `ReviewIndependenceError` plutôt que de replier)
- Ne lit jamais `quota_windows`/`utilization`/reset credits pour classer ou
  départager des workers ; ne consomme jamais de reset credit
- Un seul probe `QuotaManager.get()` par provider distinct réellement
  nécessaire à la sélection (déduplication en amont, pas de duplication de
  la logique single-flight du `QuotaManager`)
- Erreurs domaine : `UnknownWorkerError`, `NoEligibleWorkerError`,
  `ReviewIndependenceError`
- Hors périmètre (volontairement) : exécution, retry/fallback post-échec,
  WAITING_RESET/RECOVERY_REQUIRED — appartiennent à RalphExecutionEngine
- Tests : `tests/test_worker_selector.py` (100% offline, fake adapters)

**8. Persistence / execution audit** (SQLite) — volet Execution = Slice 5
- **Volet `Execution` (audit) — ✅ DONE** (`src/orchestrator/execution_store.py`)
  - `ExecutionRecord` : identité complète et immuable une fois créée
    (`execution_id` distinct de `task_id`/`worker_id`, snapshot
    provider/backend/model/`reasoning_effort`/role/`started_at`) + champs
    d'audit évolutifs (`finished_at`, `status`, `exit_code`,
    `provider_session_id`, `ralph_loop_id`, `git_sha_before/after`)
  - `ExecutionStatus` minimal : `RUNNING` (seul état non terminal),
    `SUCCEEDED`, `FAILED`, `INTERRUPTED`, `RECOVERY_REQUIRED` — pas de
    `WAITING_RESET` (état d'orchestration futur, hors périmètre)
  - `exit_code` = donnée d'audit technique uniquement, jamais convertie
    automatiquement en verdict métier (`SUCCEEDED`/`FAILED` toujours
    explicite via `mark_succeeded()`/`mark_failed()`)
  - Transitions validées, minimales : `RUNNING` → un des 4 états
    terminaux ; tout état terminal est bloqué à toute transition
    ultérieure (`InvalidTransitionError`)
  - `ExecutionStore` (sqlite3 stdlib, synchrone) : `create()`, `get()`,
    `list_running()`, `mark_succeeded/failed/interrupted/recovery_required()`
    — écriture transactionnelle, persistance après redémarrage du process,
    erreurs explicites sur données corrompues (`CorruptExecutionRecordError`)
  - Ne lance aucun worker/Ralph/Git ; ne fait aucun retry/fallback ; fournit
    uniquement les primitives de détection (`list_running()`) et de
    marquage explicite (`mark_interrupted`/`mark_recovery_required`) qu'une
    future réconciliation utilisera
  - Tests : `tests/test_execution_store.py` (100% offline, sqlite réel sur
    fichier temporaire)
- **Volets restants (non traités par cette slice)** : `MVP` (périmètre,
  critères d'acceptation), `Task` (statut PENDING/IN_PROGRESS/DONE/FAILED/
  RECOVERY_REQUIRED), `Worker` persistant (le `Worker` de
  `worker_selector.py` est actuellement déclaratif en mémoire, pas encore
  stocké), `QuotaWindow` persistant, et la réconciliation automatique au
  démarrage (actuellement : primitives disponibles, pas de logique de
  démarrage qui les invoque)

**9. RalphExecutionEngine (wrapper) — ✅ DONE (Slice 6)**
- `RalphExecutionEngine(execution_store, ralph_binary="ralph", clock=...)`
  (`src/orchestrator/ralph_execution_engine.py`) : `async execute(request:
  ExecutionRequest) -> ExecutionResult`
- Génère un `ralph.yml`/`hats.yml`/`PROMPT.md` temporaires, propres à
  l'exécution (jamais la config Ralph permanente de l'utilisateur), un seul
  hat déclaratif traduit depuis `Worker.backend`/`model`/`reasoning_effort`
  (jamais `provider=="anthropic"`/`"openai"` — traduction par table
  explicite `backend -> type Ralph` + args CLI)
- Lance `ralph run -a -q -c ... -H ... -P ...` en subprocess borné
  (timeout global, cwd=workspace, args en liste, jamais `shell=True`) ;
  n'appelle jamais `claude`/`codex` directement
- `ExecutionRecord` créé `RUNNING` avant le lancement du subprocess ;
  finalisé dans tous les cas (succès, échec métier, JSONL invalide, backend
  non supporté, binaire introuvable) — jamais laissé `RUNNING` dans le
  store
- **EXIT CODE != VERDICT MÉTIER** (validé par le spike : `LOOP_COMPLETE` +
  `reason=max_iterations` + exit_code=2) : le verdict vient exclusivement
  des `success_topics`/`failure_topics` observés dans les events Ralph ;
  `exit_code` reste une donnée d'audit, jamais transformée en verdict —
  aucun événement métier fiable ⇒ `FAILED` (fail-closed, jamais
  `SUCCEEDED`)
- `task.start`/`task.resume` rejetés explicitement comme events custom
  (réservés au coordinateur Ralph) via `ReservedEventTopicError`
- `ralph_loop_id` récupéré depuis `.ralph/current-loop-id`/
  `current-events` du workspace (source déterministe), jamais par parsing
  fragile de stdout
- Git : lecture seule (`git rev-parse HEAD` avant/après), aucune mutation
  (pas de checkout/branch/commit/merge) ; non-Git ⇒ `None`
- Aucun retry/fallback ; timeout ⇒ `INTERRUPTED` (retour normal, pas
  d'exception) ; recovery d'anciennes exécutions `RUNNING` reste une passe
  de réconciliation future au-dessus de `ExecutionStore.list_running()`
  (non construite ici)
- `ExecutionStore` (Slice 5) étendu a minima : `ralph_loop_id`/
  `provider_session_id` désormais réglables aux transitions (connus
  seulement après coup), même pattern que `git_sha_after`
- Fixtures réelles réutilisées depuis le spike Ralph (`ralph-spike`,
  jamais une dépendance runtime) : `tests/fixtures/ralph_events/` — aucune
  invocation Claude/Codex répétée pour ces cas
- Un seul smoke test réel exécuté (Claude Haiku, `work.completed`) pour
  valider l'intégration bout en bout ; a révélé et corrigé deux bugs réels
  (`--no-tui` incompatible avec `--autonomous` ; `description` de hat
  requise par Ralph)
- Tests : `tests/test_ralph_execution_engine.py` (offline, sauf `git`
  local pour le test de capture de SHA)

### Découpage incrémental (Slice 7 et suivantes)

Ce découpage remplace/précise l'ancien item générique « 10. MVPManager » et
le fourre-tout « Compléments MVP 0.1 » ci-dessous à la lumière de la
« Vision cible du produit » (voir plus haut). Il reste **indicatif** dans
son ordre exact au-delà de Slice 7 — l'architecture existante peut justifier
un léger réordonnancement — mais chaque slice doit rester petite,
testable offline, et documenter explicitement ses dépendances.

**Slice 7 — Project/MVP orchestration core + durable handoff**
- `Project`, `MVP`, `WorkItem` : types haut niveau (voir section dédiée
  ci-dessous pour le détail retenu)
- `MVPManager` : sélectionne les `WorkItem` éligibles (dépendances
  satisfaites), délègue au `WorkerSelector` existant pour le choix du
  worker, délègue au `RalphExecutionEngine` existant pour l'exécution —
  n'implémente ni quota, ni provider, ni priorité, ni capability matching
  (déjà dans `WorkerSelector`), et ne crée pas de seconde file de tâches
  fine (Ralph garde son orchestration interne une fois le WorkItem délégué)
- Exécution séquentielle déterministe (pas de parallélisme dans cette
  slice)
- `HandoffRecord` durable, persistant, relisible après redémarrage,
  indépendant du contexte conversationnel d'un worker — construit à partir
  de faits (`ExecutionRecord`, events métier, SHA git), pas seulement d'un
  résumé libre
- Persistence dédiée (store(s) séparé(s) d'`ExecutionStore`, sqlite3
  stdlib), reprise après redémarrage sans retry aveugle
- **Reporté** (hors périmètre explicite de cette slice) : lancement de
  tests projet, code review, release gate, activity report complet,
  `WAITING_RESET`, reprise automatique après quota, multi-agent planning,
  notification, fenêtre 20 min, GitHub PR/merge
- Dépend de : Slice 4 (WorkerSelector), Slice 5 (ExecutionStore), Slice 6
  (RalphExecutionEngine) — toutes DONE

**Slice 8 — Project validation commands + tests / quality gates — ✅ DONE**
- `ValidationCommand` (config persistée par projet, `argv` structuré,
  jamais de chaîne shell libre, `shell=False`) : `validation_id`, `kind`
  (`UNIT_TEST`/`INTEGRATION_TEST`/`LINT`/`TYPECHECK`/`BUILD`/`SMOKE`/
  `CUSTOM`), `timeout_seconds`, `required` — voir `src/orchestrator/
  validation.py`
- Configuration explicite uniquement (pas de discovery `pyproject.toml`/
  `package.json`/`Makefile` dans cette slice — délibérément différé)
- `ValidationStatus` : `PASSED`/`FAILED`/`ERROR`/`TIMEOUT`/`SKIPPED` —
  jamais confondus (`FAILED` = exit non-zéro, `ERROR` = binaire introuvable/
  échec de lancement, `TIMEOUT` = dépassement borné)
- `QualityGateRunner` : exécute uniquement les commandes configurées
  (jamais inventées par un worker), persiste chaque `ValidationResult` via
  `ValidationStore` (sqlite3, store dédié, `ExecutionStore`/
  `ProjectStateStore` non transformés en god objects)
- Fail-closed strict : une validation `required` sans résultat `PASSED`
  enregistré échoue le gate — y compris l'**absence totale** de résultat
  pour une validation `required` configurée (jamais interprétée comme
  succès) ; une validation optionnelle peut échouer sans faire échouer le
  gate global
- `validation_run_id` distinct de `execution_id`/`work_item_id`/`mvp_id` ;
  un même WorkItem peut être gaté plusieurs fois, dernier résultat
  retrouvable (`latest_gate_result_for_work_item`)
- Git : lecture seule (`git rev-parse HEAD`, une fois par run, capturé
  comme fait d'audit) — aucune mutation
- Intégration `MVPManager` minimale et opt-in : `quality_gate_runner`
  optionnel au constructeur ; sans lui (ou sans commande configurée), le
  comportement Slice 7 est inchangé (succès exécution ⇒ `COMPLETED`) ; avec
  lui, `COMPLETED` exige exécution réussie **et** gate `passed` — un gate
  échoué finalise en `FAILED` (jamais `BLOCKED`, réservé aux problèmes de
  graphe de dépendances), aucun dépendant lancé, aucun retry
- Handoff enrichi d'un résumé structuré et compact du gate
  (`quality_gate=PASSED (id=status, ...)`), jamais de milliers de lignes de
  stdout — le détail complet reste dans `ValidationStore`
- Aucun code review IA (Slice 9), aucun release gate complet (plus tard)
- Tests : `tests/test_validation.py` (offline, commandes locales
  contrôlées type `python -c ...`) + tests d'intégration dans
  `tests/test_mvp_manager.py`
- Dépend de : Slice 7 (WorkItem/MVP existent déjà)

**Slice 9 — Independent author/reviewer orchestration — ✅ DONE**
- `ReviewRecord`/`ReviewFinding`/`ReviewStatus`/`ReviewPolicy`/`ReviewStore`
  (`src/orchestrator/review.py`, sqlite3 stdlib, store dédié)
- Invariant absolu vérifié au niveau du contrat lui-même :
  `reviewer_worker_id != author_worker_id` (`ReviewRecord.__post_init__`
  lève si violé) ; préférence/obligation `provider` différent **jamais
  dupliquée** — entièrement déléguée à la `WorkerSelectionPolicy` déjà
  configurée sur l'instance `WorkerSelector` injectée (Slice 4)
- L'« author » d'une review = le worker de la **dernière** exécution de
  développement soumise à revue (pas nécessairement le développeur
  initial du WorkItem) — dérivé localement à chaque cycle, jamais d'un
  historique reconstruit
- Review exécutée via `RalphExecutionEngine` (jamais `claude`/`codex`/
  `ralph` en direct) ; verdict canonique = events métier
  `review.approved`/`review.rejected`, jamais interprétation libre de
  stdout ; `exit_code` reste un fait d'audit uniquement
- `REJECTED` distingué explicitement d'un défaut d'event fiable (`ERROR`) :
  `RalphExecutionEngine` collapse les deux en `FAILED`, mais
  `MVPManager._determine_review_verdict` ré-inspecte les events pour ne
  jamais confondre un vrai rejet motivé (findings) avec une absence de
  signal
- Findings parsés depuis le payload de `review.rejected` selon un contrat
  explicite : tableau JSON d'objets → findings structurés ; texte brut
  (pattern validé par le spike) → un finding unique enveloppant le texte
- Nouveaux statuts `WorkItemStatus.REVIEWING`/`NEEDS_REWORK` (transitions
  validées : `RUNNING→REVIEWING→{COMPLETED,NEEDS_REWORK,BLOCKED}`,
  `NEEDS_REWORK→RUNNING`) ; `NEEDS_REWORK` directement éligible pour
  `MVPManager` (pas de re-vérification de dépendances, déjà établies)
- Boucle bornée par `ReviewPolicy.max_review_cycles` (défaut 3) ; limite
  atteinte ⇒ `BLOCKED` avec raison explicite, jamais de retry technique
  automatique (rework métier motivé par des findings ≠ retry technique
  aveugle d'une exécution crashée — distinction documentée)
- Reviewer indisponible (aucun worker éligible indépendant) ⇒ jamais de
  fallback silencieux vers le même provider/worker ; `ReviewRecord` `ERROR`
  persisté, WorkItem `BLOCKED` explicite, fail-closed (pas encore
  `WAITING_RESET`, différé à Slice 11)
- `COMPLETED` exige désormais : succès dev **et** gate `PASSED` (si
  configuré) **et** review `APPROVED` (si configuré) — intégration
  opt-in via `quality_gate_runner`/`review_store` optionnels sur
  `MVPManager`, comportement Slice 7/8 préservé si absents
- Handoff enrichi : `decisions` (résumé d'approbation) ou `open_issues`
  (résumé structuré des findings) selon le verdict — jamais de dépendance
  au contexte conversationnel du worker précédent
- Recoupe et précise l'ancienne Phase 3 (« Séparation Developer/Reviewer »
  ci-dessous), dont le contenu reste valable comme contexte
- Tests : `tests/test_review.py` + intégration dans
  `tests/test_mvp_manager.py` (100% offline)
- Dépend de : Slice 7, Slice 8 (verdict de review = une validation parmi
  d'autres)

**Slice 10 — Release gate + activity report — ✅ DONE**
- `ReleaseGateStatus`/`ReleaseCheck`/`ReleaseGateResult`/`ReleaseRecord`/
  `ReleaseStore` (`src/orchestrator/release.py`, sqlite3 stdlib, store
  dédié) — fail-closed strict : `PASSED` uniquement si chaque check
  confirme positivement sa condition ; `ERROR` (évaluation non fiable,
  ex. données corrompues) jamais confondu avec `FAILED` (checks évalués,
  au moins un non satisfait), ni l'un ni l'autre jamais traité comme
  `PASSED`
- 4 checks explicites et non dupliqués : `all-work-items-completed`
  (couvre PLANNED/READY/RUNNING/REVIEWING/NEEDS_REWORK/FAILED/BLOCKED en
  un seul check — tous doivent être `COMPLETED`), `quality-gates-satisfied`
  (dérivé de la config `ValidationStore` réellement persistée — pas
  d'heuristique), `reviews-approved` (actif seulement si le paramètre
  explicite `review_required=True` est passé — jamais déduit
  arbitrairement d'une présence/absence de données, cf. note "reviews
  requises" de la tâche Slice 10), `no-dangling-running-executions`
- `ReleaseManager` (`src/orchestrator/release_manager.py`) : service
  séparé de `MVPManager` (évite le god object), lit uniquement
  `ProjectStateStore`/`ExecutionStore`/`ValidationStore`/`ReviewStore`/
  `HandoffStore` — ne lance et ne sélectionne jamais de worker
- Cycle MVP étendu : `RUNNING → VALIDATING → RELEASED` ; un gate échoué
  laisse le MVP en `VALIDATING` (état correctable, jamais un cul-de-sac) ;
  `mark_mvp_validating`/`mark_mvp_released` ajoutés à `ProjectStateStore`
- `ActivityReport`/`ActivityReportStore` (`src/orchestrator/
  activity_report.py`) : snapshot factuel durable généré **uniquement**
  après un gate `PASSED`, agrégé depuis les stores existants (jamais de
  scraping stdout/logs, jamais rédigé par un LLM) — WorkItems, executions
  (worker/provider/model/reasoning_effort/SHA/session ids), validations,
  reviews (+ findings), handoffs, incidents, synthèse factuelle
  (compteurs, workers/providers utilisés, durée)
- `render_markdown()` : rendu Markdown déterministe pur, aucun appel LLM
- Petites extensions minimales et justifiées des stores existants (pas de
  god object) : `ExecutionStore.list_for_task()`,
  `ValidationStore.list_results_for_work_item()` +  `git_sha` exposé sur
  `ValidationResult`
- Git : lecture seule (`git rev-parse HEAD` une fois par tentative de
  release) ; aucune mutation
- Plusieurs tentatives de release conservées par MVP (jamais écrasées),
  restart supporté (release/report relisibles)
- Tests : `tests/test_release.py`, `tests/test_activity_report.py`,
  `tests/test_release_manager.py` (100% offline)
- Dépend de : Slice 7, Slice 8, Slice 9

**Slice 11 — Quota waiting / interruption / durable resume — ✅ DONE**
- `WorkItemStatus.WAITING` introduit comme état d'**orchestration**
  générique (jamais dans `ProviderAvailability`/`ExecutionStatus`
  existants, qui restent des contrats provider/exécution purs) — la
  raison précise (phase interrompue : `development`/`rework`/`review`,
  deadline, providers concernés) vit dans un `WaitRecord` séparé, jamais
  dans le WorkItem lui-même
- `orchestrator/wait.py` (nouveau, sqlite3 stdlib, store dédié) :
  `WaitPhase`/`WaitReason`/`WaitStatus`/`WaitRecord`/`WaitStore`
  (`create`/`get`/`list_pending`/`list_due(now)`/`next_due_at`/`resolve`,
  historique jamais supprimé) + `WaitCoordinator` (bookkeeping pur :
  n'appelle jamais WorkerSelector/QuotaManager lui-même, ne fait
  qu'interpréter les diagnostics qu'on lui passe)
- `WorkerSelector` enrichi *a minima* : `ProviderSelectionDiagnostic`
  (provider/available/reason/reset_at) attaché à `NoEligibleWorkerError`/
  `ReviewIndependenceError` sur échec de sélection uniquement — jamais
  utilisé par WorkerSelector pour classer/sélectionner, jamais de logique
  de scheduler ajoutée là ; 100% rétro-compatible (paramètre keyword-only
  par défaut `()`)
- `eligible_at` calculé strictement depuis les `QuotaWindow.reset_at`
  réels des candidats diagnostiqués `quota_exhausted` (le plus proche) —
  jamais inventé ; une `ProviderProbeError` ou une raison `unknown` ne
  produit jamais de `WaitRecord` (l'exception propage, ou le
  comportement Slice 9 (`BLOCKED`) est préservé tel quel)
- `MVPManager` (opt-in via nouveau paramètre optionnel `wait_store`,
  comportement Slice 7/8/9/10 inchangé si absent) : sélection dev/reviewer
  échouée + reset fiable connu ⇒ `WorkItem` → `WAITING` + `WaitRecord`
  persisté ; sinon comportement préexistant strictement préservé
- Reprise pull-based, jamais de polling : `run_next_work_item` interroge
  `WaitCoordinator.find_due` en tout premier ; re-probe WorkerSelector
  au moment de la reprise (un reset théorique n'est jamais une preuve de
  disponibilité) ; toujours une **nouvelle** `execution_id` (jamais de
  relance aveugle d'une exécution/interruption passée) ; le worker
  repris peut différer du précédent (provider différent inclus)
- Contexte de reprise reconstruit depuis les faits durables
  (`HandoffStore`, jamais depuis le worker interrompu) : reprise
  développement/rework réinjecte le dernier handoff dans les
  instructions ; reprise review reconstruit un `ExecutionResult` minimal
  (execution_id/worker_id/git_sha) à partir du dernier `HandoffRecord`
  pour relancer `_run_review` sans dépendre du worker précédent
- Toujours re-probe avant de reprendre : si le reset échoit mais qu'aucun
  worker n'est encore éligible, le wait est **requeue** avec un nouveau
  `eligible_at` fiable si connu, sinon abandon explicite vers `BLOCKED`
  (jamais de reprise aveugle sur un simple reset théorique)
- Dépendants d'un WorkItem `WAITING` jamais lancés (`refresh_readiness`
  ne les promeut que sur dépendance `COMPLETED`, inchangé)
- S'applique symétriquement à la review : un reviewer indisponible par
  quota fait aussi passer le WorkItem en `WAITING` (phase `review`) — le
  WorkItem n'est jamais `COMPLETED` faute de reviewer disponible ; aucune
  entrée `ReviewRecord` fantôme n'est créée (ne consomme jamais un cycle
  de rework borné pour une tentative qui n'a pas eu lieu)
- Garanties invariantes vérifiées par les tests : aucune consommation de
  reset credit Codex (`ResetCredit.auto_consume` toujours `False`, aucune
  méthode `consume()` nulle part), aucun polling agressif, aucun sleep
  réel, aucun appel direct Claude/Codex/Ralph (uniquement via
  WorkerSelector/RalphExecutionEngine existants)
- **Recovery execution-level (RUNNING/INTERRUPTED orphelines) — fermé
  dans cette même slice** : `WorkItemStatus.RECOVERY_REQUIRED` introduit,
  distinct de `WAITING` (aucune deadline à attendre, immédiatement
  re-orchestrable — voir docstring de `WorkItemStatus`). Nouveau
  `orchestrator/recovery.py` : `RecoveryCoordinator`, pur bookkeeping
  (aucune sélection de worker, aucun lancement d'exécution, aucune
  logique WorkerSelector/QuotaManager dupliquée) :
  - `reconcile_mvp`/`reconcile_work_item` : au tout début de chaque
    `run_next_work_item`, détecte un WorkItem `RUNNING`/`REVIEWING` dont
    la dernière exécution correspondante (rôle developer/reviewer) est
    soit encore `RUNNING` (orpheline — le process qui la tenait a disparu,
    son sort réel est inconnu ⇒ `ExecutionStore.mark_recovery_required`,
    jamais réutilisée ni remise `RUNNING`), soit déjà `INTERRUPTED` (déjà
    un état terminal légitime, laissé strictement inchangé — seul le
    WorkItem est reconcilié) ; toute autre exécution (déjà `SUCCEEDED`/
    `FAILED`/`RECOVERY_REQUIRED`) est un no-op
  - `ensure_recovery_handoff` : garantit un `HandoffRecord` durable pour
    l'exécution interrompue (idempotent par `execution_id`) — construit
    uniquement depuis les faits persistants (`ExecutionRecord`, dernier
    handoff pour le quality gate déjà connu, dernières findings de review
    si pertinent) ; jamais d'appel au worker interrompu, jamais de résumé
    LLM ; `open_issues="execution interrupted / recovery required"`,
    `next_action="continue work from persisted state"`
  - Une exécution `INTERRUPTED` détectée **dans le même appel** (timeout
    géré par `RalphExecutionEngine` lui-même, dev ou review) est traitée
    identiquement : `RECOVERY_REQUIRED` (jamais `FAILED`, qui interdirait
    toute continuation normale ; jamais de retry silencieux) — côté
    review, l'honnête `ReviewRecord(status=INTERRUPTED)` reste enregistré
    (ce n'est pas un verdict fantôme), mais ne consomme plus à tort un
    cycle de rework borné
  - Reprise immédiate (pas de deadline, contrairement à `WAITING`) :
    `MVPManager._try_resume_recovery_required` relit le rôle de la
    dernière exécution reconciliée (jamais deviné) pour savoir si la
    reprise est développement/rework (nouvelle sélection `WorkerSelector`
    + nouvelle `execution_id`, worker potentiellement différent — ex.
    Claude → Codex) ou review (reconstruction minimale d'un
    `ExecutionResult` depuis le dernier handoff **de l'auteur**
    spécifiquement — jamais le handoff de reprise le plus récent, qui
    porterait l'identité du reviewer interrompu et pourrait sinon rendre
    reviewer == author) via `_run_review`, reviewer toujours indépendant
    du dernier auteur
  - Quality gate jamais rejoué inutilement : une reprise review réutilise
    le résultat de gate déjà connu (`gate_summary_override`, aucun
    nouveau code produit) ; une reprise développement le fait rejouer
    normalement (nouvelle exécution potentiellement porteuse de nouveau
    code), sans système de cache dédié
  - Dépendants d'un WorkItem `RECOVERY_REQUIRED` jamais lancés (même
    mécanisme `refresh_readiness` que `WAITING`)
  - Opt-in via nouveau paramètre optionnel `execution_store` sur
    `MVPManager` — comportement Slice 7-11 (wait) strictement inchangé si
    absent
- Recoupe et précise l'ancienne Phase 4 (« Suivi des quotas et resets »)
- Tests : `tests/test_wait.py`, `tests/test_recovery.py` (nouveaux),
  extensions de `tests/test_project_state.py`,
  `tests/test_worker_selector.py`, `tests/test_mvp_manager.py` (100%
  offline, horloges injectées, aucun sleep réel)
- Dépend de : Slice 7 (état projet dans lequel s'inscrit une attente),
  Slice 9 (sélection reviewer)

**Slice 12 — Multi-agent release planning + roadmap synthesis — ✅ DONE**
- Nouveau `orchestrator/planning.py` : `PlanningSnapshot` (contexte
  factuel durable — contenu + `roadmap_hash` SHA-256 de `ROADMAP.md`,
  `ActivityReport` de la release chargé depuis `ActivityReportStore`
  (jamais régénéré), résumé structuré du project state et open
  issues/blockers dérivés des `incidents` du report), `PlanningSession`
  (historique jamais écrasé, plusieurs tentatives par release possibles),
  `PlannerProposal` (VALID/INVALID), `RoadmapProposal`,
  `RoadmapChange`/`RoadmapChangeType` (KEEP/ADD/MOVE/DROP),
  `ProposedWorkItem`, `Disagreement` — le tout persisté par un
  `PlanningStore` dédié (sqlite3 stdlib), jamais un god object sur
  `ProjectStateStore`
- **Indépendance structurelle** : `_build_planner_instructions(snapshot)`
  n'a par signature aucun paramètre par lequel une proposition sœur
  pourrait fuiter — chaque planner ne voit que le `PlanningSnapshot`
  commun ; la confrontation des propositions n'a lieu que dans les
  instructions du synthesizer, qui reçoit toutes les `PlannerProposal`
  VALID triées par `worker_id` (jamais par ordre d'exécution — testé
  explicitement : même synthèse quel que soit l'ordre réel des planners)
- `PlanningPolicy(planner_count=2, prefer_distinct_providers=True,
  require_distinct_workers=True, prefer_distinct_synthesizer_worker=True)`
  — `require_distinct_workers` épinglé `True` (même motif que
  `WorkerSelectionPolicy.require_distinct_worker_for_review`) ; ajouter
  un 3ᵉ planner (futur Mistral) ne change pas l'algorithme
- Sélection **exclusivement** via `WorkerSelector` existant (capabilities
  `release_planning`/`roadmap_synthesis`, jamais de nom de
  worker/provider en dur) : boucle bornée d'exclusion progressive
  (`excluded_worker_ids`) pour garantir des `worker_id` distincts et
  préférer des providers distincts quand c'est possible, sans jamais
  dupliquer la logique de capabilities/quota de `WorkerSelector`
- Un seul planner éligible alors que `planner_count=2` ⇒ fail-closed
  explicite : `PlanningSessionFailedError`, session `FAILED` persistée,
  aucune tentative de synthèse avec une seule proposition déguisée en
  analyse multi-agent
- Exécution **uniquement** via `RalphExecutionEngine` (jamais Claude/Codex
  direct, jamais de subprocess Ralph direct), rôles dédiés
  `planner`/`synthesizer`, événements métier dédiés
  `planning.proposed`/`planning.failed`/`synthesis.proposed`/
  `synthesis.failed` (jamais `task.start`/`task.resume`) ; instructions
  interdisant explicitement toute modification de fichiers, commit, push
  ou changement réel de roadmap
- Contrat de sortie strict : payload JSON structuré et validé
  (`_parse_planner_payload`/`_parse_synthesizer_payload`, fail-closed —
  jamais de fallback texte libre comme `review.parse_findings`) ; absence
  d'événement terminal fiable ou payload invalide ⇒ `PlannerProposal`
  `INVALID` (jamais silencieusement ignorée, jamais traitée comme un
  succès)
- Échec du synthesizer : `PlannerProposal` existantes jamais modifiées,
  aucune `RoadmapProposal`, session `FAILED` — aucun retry silencieux
- Restart : session/snapshot/proposals/synthèse relisibles ;
  `run_planners` ne relance jamais un worker qui a déjà une
  `PlannerProposal` (VALID ou INVALID) pour la session — resumable par
  construction, sans nouvelle boucle WAITING/RECOVERY dédiée (scope
  volontairement restreint, cf. tâche Slice 12)
- `render_markdown()` : rendu Markdown déterministe pur du diff de
  roadmap proposé (KEEP/ADD/MOVE/DROP + MVP proposé + risques +
  agreements/disagreements), aucun appel LLM
- Aucune mutation de `ROADMAP.md`, aucune création de MVP réel dans
  `ProjectStateStore`, aucune notification, aucune fenêtre d'approbation
  — tout cela reste Slice 13
- Tests : `tests/test_planning.py` (100% offline, fake WorkerSelector/
  RalphExecutionEngine)
- Dépend de : Slice 10 (il faut un rapport d'activité et un release gate
  pour avoir une release « réussie » à analyser)

**Slice 13 — Notification + optimistic approval window (20 min) — ✅ DONE**
- Pas de human gate bloquant entre deux releases : proposition persistée
  → notification envoyée → délai de 20 minutes → `APPROVE` (immédiat) /
  `REJECT` (n'enchaîne pas le MVP suivant) / `MODIFY` (nouvelle
  proposition) / silence → **approbation automatique à l'échéance**
- Règle survit au redémarrage du process (deadline persistée, jamais en
  mémoire seule)
- Cette politique ne concerne QUE la gouvernance roadmap/MVP — silence ne
  vaut jamais autorisation pour une action destructive, financière ou
  sensible ailleurs. Policy configurable (délai, activation)
- Dépend de : Slice 12 (il faut une proposition de synthèse à approuver)
- Voir `src/orchestrator/approval.py` (nouveau : `ApprovalWindow`,
  `ApprovalStore`, `ApprovalCoordinator`, `ApprovalPolicy`,
  `ApprovalStatus`) et `tests/test_approval.py`. Layered sur
  `RoadmapProposal` (Slice 12) par référence (`roadmap_proposal_id`),
  exactement comme `WaitRecord` (Slice 11) est layered sur
  `ProviderAvailability` — `planning.RoadmapProposalStatus` reste
  volontairement figé à sa seule valeur `PROPOSED`, jamais modifié par
  cette slice
- `ApprovalCoordinator.open_window(...)` : idempotent par
  `roadmap_proposal_id` (un redémarrage qui rejoue l'ouverture ne crée
  jamais une deadline ni une notification en double) ; deadline calculée
  une fois à la création à partir de la policy en vigueur à cet instant
  (`ApprovalWindow.auto_approval_enabled` figé pour toujours, jamais
  recalculé depuis une policy relue plus tard — un changement de policy
  n'affecte jamais rétroactivement une fenêtre déjà ouverte)
- Statuts : `AWAITING_APPROVAL`/`APPROVED`/`REJECTED`/`MODIFY_REQUESTED`/
  `AUTO_APPROVED`, transition unique (jamais de re-décision une fois
  terminal) ; `APPROVE`/`REJECT`/`MODIFY` sont des décisions immédiates
  (jamais besoin d'attendre la deadline) ; seul `AUTO_APPROVED` est
  contraint à `now >= deadline_at` (`require_due`, fail-closed —
  `ApprovalNotYetDueError` sinon, jamais de fenêtre auto-approuvée par
  anticipation)
- `ApprovalCoordinator.resolve_due()` : purement événementiel/temporel,
  aucun thread, aucun sleep — même posture que `WaitCoordinator` (Slice
  11) ; un appelant l'invoque à sa propre cadence. Notification livrée via
  un `NotificationSink` (callable) injectable, `None` par défaut (no-op) ;
  un échec du notifieur est capturé et audité
  (`ApprovalWindow.notification_error`) mais ne bloque jamais la création
  de la fenêtre ni sa deadline
- Reporté (hors périmètre explicite de cette slice, comme annoncé par le
  découpage Slice 12) : aucune mutation de `ROADMAP.md`, aucune création
  de MVP réel dans `ProjectStateStore`, et `MODIFY_REQUESTED` ne déclenche
  aucune nouvelle session de planning automatiquement — une slice future
  agit sur la décision persistée ici (« mise à jour roadmap » / « MVP
  suivant » du cycle cible)
- Tests : `tests/test_approval.py` (100% offline, sqlite3 réel sous
  `tmp_path`, horloge/`id_factory` injectables, aucun réseau/subprocess)

**Slice 14 — Apply decided RoadmapProposal + create/start next MVP — ✅ DONE**
- Ferme la boucle autonome entre deux releases : une décision terminale
  `APPROVED`/`AUTO_APPROVED` (Slice 13) sur une `RoadmapProposal` (Slice
  12) est appliquée — jamais `AWAITING_APPROVAL`/`REJECTED`/
  `MODIFY_REQUESTED`, ni l'absence de toute fenêtre d'approbation, qui
  laissent l'opération sans aucune trace (aucune ligne créée, aucun
  fichier touché, aucun MVP)
- Voir `src/orchestrator/roadmap_application.py` (nouveau :
  `RoadmapApplication`, `RoadmapApplicationStore`,
  `RoadmapApplicationService`, `RoadmapApplicationStatus`) et
  `tests/test_roadmap_application.py`
- Garde de cohérence roadmap_hash : avant toute application, le hash
  sha256 courant de `ROADMAP.md` est recalculé et comparé à celui capturé
  dans le `PlanningSnapshot` source ; une divergence n'applique jamais la
  proposition — un `RoadmapApplication` `CONFLICT` explicite
  (« STALE_PROPOSAL ») est persisté et une `RoadmapConflictError` est
  levée, sans aucune mutation
- Application déterministe, sans LLM : le diff KEEP/ADD/MOVE/DROP et le
  MVP proposé sont transformés en `ROADMAP.md`/MVP/WorkItems réels par une
  transformation pure — aucun worker, aucun appel
  Claude/Codex/Ralph/`RalphExecutionEngine`/`WorkerSelector` dans ce
  module ; le seul appel à un worker reste l'invocation optionnelle et
  déjà existante de `MVPManager.run_next_work_item(...)` pour démarrer le
  premier WorkItem du nouveau MVP — jamais une réimplémentation de
  `MVPManager`
- `ROADMAP.md` reste un document humain : tout le contenu en dehors de
  deux marqueurs dédiés (`<!-- orchestrator:roadmap-applications:begin
  -->`/`...:end -->`, ajoutés une seule fois en fin de fichier) est
  préservé verbatim pour toujours ; seule la section gérée entre ces
  marqueurs est régénérée intégralement à chaque application, à partir de
  `RoadmapApplicationStore` (jamais par découpage/relecture incrémentale
  de texte) — ce qui la rend déterministe indépendamment de tout état
  antérieur du fichier
- Idempotence et redémarrage : `target_mvp_id` et le mapping titre
  proposé -> `work_item_id` réel sont décidés une seule fois, avant toute
  mutation, et persistés dans la ligne `RoadmapApplication` elle-même — un
  deuxième appel pour la même proposition déjà `APPLIED` renvoie le
  résultat existant sans réappliquer ; un `APPLYING` orphelin après crash
  n'est JAMAIS rejoué aveuglément par `apply_decided_proposal` (qui lève
  `RoadmapApplicationInProgressError`) — seule
  `RoadmapApplicationService.reconcile(...)` le résout explicitement, en
  comparant le hash courant du fichier à `roadmap_hash_before`/
  `roadmap_hash_after` déjà connus, puis en (re)créant MVP/WorkItems de
  façon idempotente (capture des `Duplicate*Error` déjà exposées par
  `ProjectStateStore`)
- Écriture atomique de `ROADMAP.md` via `tempfile.mkstemp` (même
  répertoire) puis `os.replace` — jamais d'écriture progressive, jamais de
  fichier partiellement écrit observable
- Validation des dépendances du MVP proposé avant toute mutation : titres
  dupliqués, référence de dépendance inconnue, ou cycle -> échec fermé
  (`RoadmapApplicationFailedError`, aucune mutation), jamais de roadmap
  partiellement incohérente
- Aucune opération Git runtime (`add`/`commit`/`push`/merge) — cette slice
  ne modifie que le fichier de travail ; gouvernance Git/PR/merge reste
  Slice 20
- Tests : `tests/test_roadmap_application.py` (100% offline, sqlite3 réel
  + vrai fichier `ROADMAP.md` sous `tmp_path`, horloge/`id_factory`
  injectables, `MVPManager` simulé par un stub — aucun réseau/subprocess/
  LLM)
- Dépend de : Slice 12 (RoadmapProposal) et Slice 13 (décision
  APPROVED/AUTO_APPROVED)

> **Adaptive execution (Slices 15-18)** — étudiée en détail dans
> `docs/ADAPTIVE_EXECUTION.md` (findings Ralph, frontière Ralph/
> orchestrateur, Worker Registry, Execution Profiles, quality tiers,
> complexity pre-flight, séquence de sélection retenue). Le texte qui suit
> décrit le plan de découpage tel qu'établi **avant** l'implémentation ;
> les slices ont depuis été implémentées une par une, dans l'ordre — voir
> les statuts individuels ci-dessous (chacun fait autorité, pas ce
> paragraphe d'introduction).
>
> Justification du découpage en 4 slices plutôt qu'une seule grosse
> slice « adaptive execution » : chacune touche un sous-ensemble distinct
> et indépendamment testable du système (config/chargement, puis
> estimation pure, puis câblage development, puis câblage review/planning)
> — un découpage plus fin réduit le risque (chaque slice reste petite,
> offline, review-able seule) au prix de quelques dépendances séquentielles
> explicites ; fusionner 17/18 aurait mélangé deux frontières de risque
> différentes (WorkerSelector/MVPManager d'un côté, review/planning de
> l'autre) dans un seul diff.

**Slice 15 — Configurable Worker Registry + Execution Profiles — ✅ DONE**
- `config/workers.yaml` (déclaratif, pas de secret) charge des `Worker`
  enrichis (`enabled`, `profiles` — chacun avec `quality_tier`/`model`/
  `reasoning_effort`) via un nouveau `WorkerRegistry.load(...)` (nouveau
  module `src/orchestrator/worker_registry.py`) — répond enfin à AC-2 de
  `MVP_SPEC.yaml`, jamais satisfait jusqu'ici. `PyYAML` ajoutée comme
  dépendance déclarée (`pyproject.toml`) — première dépendance runtime du
  projet
- `Worker` (`src/orchestrator/worker_selector.py`) est désormais l'agent
  logique seul : `model`/`reasoning_effort` fixes ont disparu du niveau
  Worker et vivent sur le nouveau type `ExecutionProfile`
  (`profile_id`/`quality_tier`/`model`/`reasoning_effort`), un ou
  plusieurs par Worker (`Worker.profiles`). Nouveau type `QualityTier`
  (`IntEnum` SIMPLE/STANDARD/COMPLEX/CRITICAL) — voir
  `docs/ADAPTIVE_EXECUTION.md` sections 4-6
- `default_profile_id` auto-résolu quand un seul profil existe
  (non ambigu), strictement requis explicitement sinon (jamais deviné) ;
  `Worker.profile(profile_id=None)` résout le profil concret.
  `Worker.with_single_profile(...)` (classmethod) couvre le cas mono-profil
  d'un mot — utilisé pour migrer les fixtures de test existantes
- Contrairement à l'hypothèse initiale de cette slice, ce n'était PAS un
  pur ajout : `Worker.model`/`.reasoning_effort` disparaissant, tout code
  qui les lisait devait changer. Adaptation minimale et mécanique
  seulement : `ExecutionRequest` (`ralph_execution_engine.py`) porte
  désormais explicitement `model`/`reasoning_effort` (résolus par
  l'appelant via `worker.profile()`, jamais par ce moteur) ;
  `_build_backend_args`/`_ralph_backend_type` prennent des primitives
  (`backend`, `model`, `reasoning_effort`) au lieu d'un `Worker` entier ;
  `mvp_manager.py`/`planning.py` résolvent `worker.profile()` avant de
  construire leur `ExecutionRequest`/`PlannerProposal` — aucune logique de
  sélection de profil (Slice 17) introduite, juste la lecture du profil
  par défaut existant. `WorkerSelector` ignore désormais tout worker
  `enabled=False` (une ligne ajoutée à son filtre existant) ;
  `RalphExecutionEngine`/`ExecutionRecord`/`MVPManager` restent sinon
  inchangés dans leur logique propre
- Ne développe aucun estimator, aucun routage adaptatif par tier, aucune
  sélection de profil autre que le défaut explicite (Slices 16/17)
- Tests : `tests/test_worker_registry.py` (nouveau, chargement/validation
  fail-closed) + mises à jour de `tests/test_worker_selector.py`
  (`ExecutionProfile`/`QualityTier`/`enabled`), `tests/test_planning.py`,
  `tests/test_mvp_manager.py`, `tests/test_ralph_execution_engine.py`
  (migration mécanique vers `Worker.with_single_profile`, aucun
  changement de comportement testé)
- Dépend de : Slice 4 (`WorkerSelector`/`Worker` existants)

**Slice 16 — Complexity pre-flight + persistent execution recommendations — ✅ DONE**
- Nouveau `src/orchestrator/complexity_estimation.py` :
  `ComplexityEstimationRequest`, `ExecutionRecommendation`,
  `ExecutionRecommendationStore` (sqlite3), `ExecutionRecommendationService`
- Séquence Option C : `WorkerSelector.select(capability=
  complexity_estimation)` choisit l'estimator (jamais un worker/provider
  codé en dur) -> profil résolu via `Worker.estimator_profile_id` (jamais
  `Worker.profile()`'s default, qui appartient au développeur final) ->
  `RalphExecutionEngine` (exactement comme un planner/synthesizer, Slice
  12 — jamais un nouveau mécanisme d'exécution) -> événement structuré
  unique `execution.profile_recommended` (`minimum_quality_tier`,
  `recommended_reasoning` optionnel, `reasons`) -> `ExecutionRecommendation`
  persistée. Pas de champ `complexity` séparé : `QualityTier` (SIMPLE/
  STANDARD/COMPLEX/CRITICAL) est déjà cette classification, en ajouter un
  second aurait dupliqué la même information sous un autre nom
- Contrat de sortie strict, fail-closed comme `planning.py` : aucun champ
  `model`/`provider`/`worker_id` accepté dans le payload (même envoyé par
  erreur par l'estimator, il est ignoré) — l'estimator recommande un
  niveau, ne choisit jamais un worker/modèle ; tier inconnu, payload
  invalide, ou absence d'événement métier fiable -> aucune
  `ExecutionRecommendation` produite, jamais un repli silencieux sur
  `STANDARD`
- Estimation **role-specific** : `role` fait partie de la requête, du
  fingerprint et de la persistence — development/review/planning/roadmap
  synthesis du même WorkItem peuvent recevoir des tiers différents,
  jamais un tier recopié automatiquement d'un rôle à l'autre
- Fingerprint déterministe **implémenté** (et non plus seulement étudié) :
  sha256 canonique sur role/project/mvp/work_item/objective/acceptance
  criteria/git_sha/dernier handoff pertinent/review findings pertinents,
  plus un `contract_version` explicite. `estimate(request,
  force_refresh=False)` réutilise la dernière recommandation pour un
  fingerprint identique sans jamais relancer Ralph ;
  `force_refresh=True` en persiste une nouvelle sans jamais écraser
  l'ancienne (même posture que `PlanningSession`/`RoadmapApplication` :
  chaque tentative est conservée)
- `estimator_profile_id` manquant sur le worker sélectionné : détecté et
  refusé (`EstimatorProfileNotConfiguredError`) **au niveau du service**,
  jamais dans `WorkerRegistry` (qui reste générique, ne connaît aucun nom
  de capability — voir `docs/ADAPTIVE_EXECUTION.md` §18)
- N'intègre encore rien dans `WorkerSelector`/`MVPManager` : aucune
  sélection finale de worker/profile de développement ou de review,
  aucun changement du cycle `MVPManager` existant — service autonome,
  testable isolément
- Tests : `tests/test_complexity_estimation.py` (100% offline, fake
  WorkerSelector/RalphExecutionEngine, aucun réseau/subprocess/LLM réel,
  aucun reset credit)
- Dépend de : Slice 15 (les tiers/profils doivent exister pour qu'une
  recommandation ait un sens)

**Slice 17 — Adaptive Worker/Profile Selection for development/rework — ✅ DONE**
- Nouveau `src/orchestrator/adaptive_execution.py` : `AdaptiveExecutionSelector`,
  `AdaptiveExecutionDecision`, `AdaptiveExecutionDecisionStore`,
  `resolve_profile()`. Compose `ExecutionRecommendationService` (Slice 16)
  + `WorkerSelector` (Slice 4, étendu) — ne réimplémente ni l'un ni l'autre
- `WorkerSelectionRequest.minimum_quality_tier: QualityTier | None` (Slice
  4, extension minimale) : un worker n'est candidat que s'il a au moins un
  profil `quality_tier >= minimum_quality_tier`, filtré **avant** toute
  diagnose de quota — l'ordre existant capability > gouvernance > quota/
  disponibilité > coût/priorité reste inchangé ; le choix du *profil*
  concret (cost_rank, proximité de tier, reasoning hint) reste
  entièrement dans `adaptive_execution.py`, jamais dans `WorkerSelector`
- `ExecutionProfile.cost_rank: int = 0` (préférence économique
  **configurée**, jamais un prix réel), validé non-négatif dans
  `WorkerRegistry`, ajouté à `config/workers.yaml`
- `resolve_profile()` : jamais de profil sous `minimum_quality_tier` ;
  parmi les profils capables, préfère un `reasoning_effort` correspondant
  exactement au hint (sans jamais dégrader le tier pour l'obtenir), puis
  `cost_rank` minimal, puis tier le plus proche du minimum, puis
  `profile_id` — déterministe
- Distinction WAITING vs incapacité structurelle obtenue **sans code
  supplémentaire** : un worker sans profil capable est éliminé avant
  toute diagnose de quota (`ProviderSelectionDiagnostic` jamais généré
  pour lui) ⇒ `NoEligibleWorkerError(diagnostics=())` ⇒
  `WaitCoordinator.record_wait` (Slice 11, inchangé) retourne `None` ⇒
  l'exception se propage (fail-closed, jamais de `WAITING` fabriqué) ;
  un worker capable mais en quota épuisé reste diagnostiqué normalement
  ⇒ `WAITING` avec deadline exactement comme avant
- `AdaptiveExecutionDecision` persistée (insert-only, `AdaptiveExecutionDecisionStore`,
  sqlite3) : snapshot concret (worker/provider/backend/profile/model/
  reasoning_effort/rationale), traçable à la `recommendation_id`, jamais
  affecté par un `config/workers.yaml` modifié après coup
- `MVPManager` : nouveau paramètre optionnel `adaptive_execution_selector`
  (même patron opt-in que `quality_gate_runner`/`review_store`/
  `wait_store`) ; câblé via un helper privé partagé (`_select_dev_worker`)
  sur DEVELOPMENT **et** REWORK, y compris leurs chemins de reprise
  (`_try_resume_due_wait`, `_try_resume_recovery_required`) — une reprise
  reconstruit le pre-flight depuis les faits persistants courants et
  n'utilise **jamais** `Worker.profile()` par défaut "parce que c'est une
  reprise" (cela violerait l'invariant no-downgrade) ; le cache/fingerprint
  Slice 16 réutilise naturellement la recommandation existante si rien de
  pertinent n'a changé, en recalcule une nouvelle sinon. À l'époque de
  cette Slice 17, la reprise **review** restait non adaptative — étendue
  depuis par la Slice 19 (voir son entrée ci-dessous). Absent (défaut) :
  comportement Slice 7-16 inchangé à l'identique
- Fail-closed de bout en bout : aucune exception de pre-flight/sélection
  n'est jamais capturée pour retomber sur un profil par défaut — seule
  `NoEligibleWorkerError` diagnosticable comme quota déclenche `WAITING`
  (mécanisme existant, jamais dupliqué)
- Review, release planning, roadmap synthesis **non touchés par cette
  Slice 17** — rendus adaptatifs depuis par la Slice 19
- Tests : `tests/test_adaptive_execution.py` (26, unitaires) + nouvelles
  classes `TestAdaptiveDevelopmentSelection`/`TestReviewSelectionIsNotAdaptive`/
  `TestAdaptiveWaitResume`/`TestAdaptiveRecoveryResume`/
  `TestAdaptiveReviewResumeUnaffected` dans `tests/test_mvp_manager.py`
  (intégration, y compris les deux chemins de reprise) — 100% offline
- Dépend de : Slice 16 (recommandation) et Slice 15 (profils)

**Slice 18 — Realization reports + real cross-worker cold-resume acceptance — ✅ DONE**
- Nouveau `src/orchestrator/realization_report.py` : `RealizationReport`,
  `RealizationReportStore` (sqlite3, insert-only), `RealizationReportService`
  — un snapshot détaillé, déterministe, d'un WorkItem (objectif, timeline
  chronologique, executions, recommendations/adaptive decisions, handoffs,
  quality gates, reviews, waits, incidents), agrégé depuis les stores déjà
  existants (`ProjectStateStore`/`ExecutionStore`/`HandoffStore`/
  `ValidationStore`/`ReviewStore`/`WaitStore`/`ExecutionRecommendationStore`/
  `AdaptiveExecutionDecisionStore`) — jamais LLM-généré, jamais une
  duplication d'`ActivityReport` (qui reste la consolidation release/MVP)
- `RealizationReport.render_html()` : rendu HTML autonome (aucun CDN/JS
  externe, CSS inline, tout le texte HTML-escaped, déterministe), résumé
  humain construit par règles explicites (jamais un LLM) ; `write_html()`
  écrit atomiquement (tempfile + `os.replace`) ; `compute_html_hash()`
  pour vérifier la stabilité du rendu
- Insert-only comme le reste du projet : une nouvelle génération produit
  toujours un nouveau `report_id`, un rapport intermédiaire
  (`HANDOFF_READY`) n'est jamais écrasé par le rapport final
  (`COMPLETED`/`BLOCKED`/`FAILED`)
- `HandoffRecord` reste l'unique artefact machine de passation ; le HTML
  n'est jamais reparsé par aucun composant — contexte supplémentaire pour
  un futur worker, jamais une source d'état
- Validation d'acceptation Slice 17 : `tests/integration/test_cross_worker_resume_e2e.py`
  (offline, déjà PASS) puis `scripts/smoke_cross_worker_real.py` — un
  smoke manuel réel (jamais lancé par `pytest`), deux vrais workers de
  `config/workers.yaml` (un backend `claude_code`, un backend `codex`),
  vrai `ExecutionRecommendationService`/`AdaptiveExecutionSelector`/
  `RalphExecutionEngine`, contrôleur qui lance Phase A et Phase B comme
  deux process OS réellement distincts (aucun objet Python partagé entre
  les deux), vérification de disponibilité provider en lecture seule
  avant tout smoke (jamais de reset credit consommé ; `BLOCKED` proprement
  si un provider est indisponible), copie jetable de
  `~/projects/ralph-spike` sous `/tmp` (original jamais modifié, vérifié
  avant/après), et une copie HTML sanitizée versionnée dans
  `docs/reports/` comme preuve historique
- Dépend de : Slice 17 (adaptive development/rework) et Slice 16
  (`ExecutionRecommendationService`)
- **Smoke réel exécuté et PASS (2026-09-13)** : deux vrais workers de
  `config/workers.yaml` — `alice` (anthropic/claude_code, modèle `haiku`,
  profil `economy`, tier `SIMPLE`) puis `victor` (openai/codex, modèle
  `gpt-5.6-terra`, `reasoning_effort=low`, profil `economy`, tier
  `SIMPLE`) — deux process OS réellement distincts (PIDs différents),
  `execution_id` distincts, chaîne git SHA réelle (avant A -> après A ->
  après B), handoff réellement transmis (le `next_action` de Worker A
  apparaît dans le prompt de Worker B), quality gate réel PASSED,
  `~/projects/ralph-spike` original inchangé (vérifié avant/après). Aucun
  bug Slice 17/18 découvert. Preuve : `docs/reports/real-cross-worker-resume-2026-09-13.html`
  (copie sanitizée, chemins temporaires neutralisés, aucun secret)

**Slice 18.5 — Stabilisation pré-Slice 19 — ✅ DONE**
- Corrige un mismatch réel découvert lors de l'audit OmniRoute (2026-09-13) :
  `config/workers.yaml` déclare la capability `code_review` pour `alice`/
  `victor`, mais `MVPManager.REVIEW_CAPABILITY` valait encore `"reviewer"`
  — aucun worker réel n'aurait donc jamais été éligible pour une review
  (`NoEligibleWorkerError` systématique, masqué jusqu'ici car les tests
  offline utilisaient des fixtures où le nom de capability du faux worker
  et de la fausse requête coïncidaient toujours). Convention retenue :
  `code_review` est la capability canonique (elle décrit une capacité),
  `"reviewer"` reste le nom du rôle logique (`REVIEWER_ROLE`, inchangé) —
  jamais les deux comme synonymes. Nouveaux tests de régression contre le
  vrai `config/workers.yaml` (`tests/test_worker_registry.py`,
  `TestShippedExampleConfigReviewCapability`) : un reviewer est
  réellement sélectionnable, author≠reviewer reste appliqué, préférence
  cross-provider conservée, aucun fallback sur un worker sans
  `code_review`.
- Audit factuel du transport `reasoning_effort` pour le backend
  `claude_code` (signalé par l'audit Codex) : `claude --help` confirme un
  vrai flag natif `--effort <low|medium|high|xhigh|max>` sur ce système
  (distinct du style `-c key=value` de codex). `_build_backend_args`
  transmettait déjà `model_reasoning_effort` pour `codex` mais ne
  transmettait rien pour `claude_code` — corrigé pour transmettre
  `--effort <valeur>` quand un profil le définit, via le même mécanisme
  hat `backend.args` déjà validé réellement pour codex
  (`docs/SPIKE_RALPH.md`). Aucun flag inventé. Aujourd'hui aucun profil
  Claude de `config/workers.yaml` ne définit `reasoning_effort` (donc
  aucun changement de comportement observable tant qu'aucun profil ne
  l'utilise) ; test de régression dédié prouvant que la config générée
  contient bien `--effort` quand la valeur est renseignée
  (`tests/test_ralph_execution_engine.py::test_reasoning_effort_is_transmitted_for_claude_via_effort_flag`),
  et le test existant `test_absence_of_reasoning_effort_is_supported_for_claude`
  reste vert pour le cas `None`.
- Ne rend pas la review adaptative (reste Slice 19) ; ne modifie ni les
  objectifs ni la portée de Slice 19/20.
- 772 tests offline PASS (766 + 6 nouveaux).

**Slice 19 — Adaptive review/planning integration — ✅ DONE**
- Étend le pre-flight/la sélection adaptative aux rôles review, release
  planning et roadmap synthesis — chacun avec son propre tier, jamais le
  tier development réutilisé tel quel (fingerprint Slice 16 indépendant
  par `role`)
- **Review** : `MVPManager._run_review` (déjà l'unique méthode partagée
  par le chemin frais, la reprise `WaitPhase.REVIEW` et la reprise
  `RECOVERY_REQUIRED` côté review) passe par un nouveau
  `_select_reviewer_worker`, symétrique de `_select_dev_worker` (Slice
  17) : `ComplexityEstimationRequest(role=REVIEWER_ROLE, ...)` ->
  `AdaptiveExecutionSelector.select(..., author_worker_id=...)` ->
  `AdaptiveExecutionDecision` persistée avant tout `ExecutionRecord`
  RUNNING. Aucune wiring séparée par chemin de reprise n'a été
  nécessaire : les trois convergeaient déjà vers `_run_review`. Jamais
  `Worker.profile()` par défaut quand l'adaptive execution est
  configurée ; l'auteur original reste exclu même après une reprise
  froide (relu depuis le dernier handoff **développeur**, jamais le
  handoff de reprise du reviewer lui-même — `_find_last_developer_handoff`,
  inchangé)
- `AdaptiveExecutionSelector.select()` gagne un paramètre optionnel
  `author_worker_id` (défaut `None`, rétrocompatible), forwardé tel quel
  à `WorkerSelectionRequest.author_worker_id` — c'est ce champ, et lui
  seul, qui déclenche la politique d'indépendance cross-provider
  (`prefer_distinct_provider_for_review`/`require_distinct_provider_for_review`)
  dans `WorkerSelector`. L'adaptive layer ne réimplémente ni n'affaiblit
  cette politique ; `ReviewRecord.__post_init__` (reviewer≠author) reste
  le filet de sécurité structurel final
- Une incapacité structurelle côté review (recommandation CRITICAL sans
  profil capable, etc.) n'est **jamais** attrapée par
  `_REVIEWER_SELECTION_ERRORS` (qui reste limité aux exceptions
  `WorkerSelector` : `NoEligibleWorkerError`/`ReviewIndependenceError`/
  `UnknownWorkerError`) — elle se propage fail-closed, symétrique du
  comportement development existant depuis Slice 17
- **Release planning + roadmap synthesis** (`orchestrator.planning`) :
  inspection réelle confirme que planner **et** synthesizer sont déjà de
  vraies exécutions Ralph/LLM (capabilities `release_planning`/
  `roadmap_synthesis`, déjà réelles depuis Slice 12) — **CAS B partout,
  jamais de synthèse déterministe dans ce dépôt** ; aucune exécution LLM
  n'a été inventée. `PlanningCoordinator` gagne deux dépendances
  optionnelles (`execution_recommendation_service`,
  `adaptive_execution_decision_store`, même patron opt-in que
  `adaptive_execution_selector` sur `MVPManager`). Choix architectural
  délibéré : ne PAS composer `AdaptiveExecutionSelector` telle quelle
  pour le planner (elle ne fait qu'un seul appel `WorkerSelector.select()`,
  incompatible avec la boucle de diversité existante de `_select_planner`
  qui élargit l'exclusion et retente) — à la place, les trois primitives
  dont `AdaptiveExecutionSelector` est elle-même composée
  (`ExecutionRecommendationService.estimate()`, le filtre
  `minimum_quality_tier` déjà supporté par `WorkerSelector`,
  `resolve_profile()` réutilisée verbatim, jamais une seconde
  implémentation) sont composées directement dans `planning.py` via un
  petit helper privé `_resolve_and_persist_decision` — une seule
  `AdaptiveExecutionDecision` persistée par planner/synthesizer
  réellement lancé, jamais une par tentative de la boucle de diversité
- Deux planners sur un snapshot identique partagent légitimement une
  `ExecutionRecommendation` (même fingerprint, calculée une fois par
  `run_planners`, jamais par planner) — explicitement acceptable — mais
  chacun obtient sa propre `AdaptiveExecutionDecision` persistée
- La synthèse obtient son propre pre-flight distinct (`role=SYNTHESIZER_ROLE`,
  jamais celui d'un planner), avec les `proposal_id` retenus intégrés à
  l'objectif du pre-flight pour que deux synthèses successives d'un même
  MVP n'entrent jamais en collision de fingerprint
- Diversité (`prefer_distinct_providers`, `prefer_distinct_synthesizer_worker`)
  et indépendance des propositions inchangées à l'identique — l'adaptive
  layer ne réduit jamais le multi-agent planning à un seul worker choisi
  puis réutilisé partout
- `ApprovalCoordinator`/`RoadmapApplicationService` **non touchés** —
  restent entièrement déterministes, aucun LLM adaptatif
- `RealizationReport` : **aucun changement de code nécessaire** — son
  agrégation (`list_for_work_item`) est déjà role-agnostique (`WHERE
  work_item_id = ?` seul) ; une recommandation/décision `role="reviewer"`
  apparaît naturellement à côté d'une `role="developer"`, prouvé par un
  nouveau test dédié (`test_reviewer_role_recommendations_and_decisions_surface_alongside_developer`)
- `WorkerSelector`/`ReviewPolicy` (indépendance author≠reviewer) et
  `PlanningPolicy` (Slice 12) restent la seule source de vérité de leurs
  garanties respectives ; l'adaptive execution ne les contourne jamais
- Aucun profil `CRITICAL` n'existe dans `config/workers.yaml` (toujours
  pas un bug) — testé explicitement côté review et côté planning :
  `NoCapableProfileError` se propage, jamais de profil fabriqué
- `config/workers.yaml` **non modifié** — aucun besoin réel de profil
  supplémentaire identifié
- Tests : nouvelles classes `TestReviewSelectionIsAdaptive`/
  `TestAdaptiveReviewResume` dans `tests/test_mvp_manager.py` (remplacent
  les anciennes `TestReviewSelectionIsNotAdaptive`/
  `TestAdaptiveReviewResumeUnaffected`, devenues incorrectes par
  construction) ; nouvelles classes `TestAdaptivePlannerSelection`/
  `TestAdaptiveSynthesizerSelection`/`TestRoadmapSynthesisIsRealLLM` dans
  `tests/test_planning.py` ; un nouveau test dans
  `tests/test_realization_report.py` — 790 tests offline PASS (772 + 18)
- Smoke réel : non relancé (le smoke Slice 17/18 existant reste la
  preuve réelle la plus récente) — cette Slice est validée entièrement
  offline, comme demandé
- Dépend de : Slice 17 (le mécanisme d'intégration existait déjà côté
  développement avant d'être étendu)

**Slice 20 — Git/PR/merge governance — ✅ DONE**
- Nouveau `src/orchestrator/git_governance.py` : `LocalGitWorkspace`
  (wrapper explicite argv autour du CLI `git`, jamais `shell=True`, jamais
  de commande destructive — pas de stash/reset --hard/clean -fd/rebase/
  force-push automatique), `GitGovernancePolicy` (base_branch, branches
  protégées, `require_clean_worktree`, `require_review`,
  `require_required_gates`, `merge_strategy` (fast-forward-only par
  défaut), `auto_merge` = **False par défaut**, `remote_pr_enabled`),
  `GitWorkItemRecord`/`GitWorkItemStore` (sqlite3, une ligne mutable par
  WorkItem + journal d'événements insert-only pour l'audit), et
  `GitGovernanceService` (`prepare_work_item`, `capture_head`,
  `assert_review_target`, `compute_merge_eligibility` — pur, déterministe,
  jamais LLM —, `merge`, `reconcile`, `publish_pull_request`).
- Branche gouvernée déterministe et stable après restart/retry/reprise :
  `work/<work-item-id-sanitisé>` (jamais recréée), `main` protégée par
  défaut (aucun worker n'y est jamais lancé directement pour un WorkItem
  gouverné), `base_sha` capturé une seule fois et immuable pour tout le
  cycle de vie du WorkItem.
- Working tree : tracked dirty => `DirtyWorkingTreeError` fail-closed
  (jamais de stash/reset/clean automatique) ; untracked signalé mais
  jamais bloquant par défaut (décision documentée dans
  `docs/GIT_GOVERNANCE.md`).
- Preuve liée au SHA exact : `compute_merge_eligibility` compare le
  `head_sha` courant du WorkItem à la fois au SHA du dernier quality gate
  et à celui de la dernière review — un nouveau commit après l'un ou
  l'autre invalide silencieusement l'ancienne preuve (jamais "probablement
  bon" ; `NOT_MERGEABLE` explicite dans tous les cas manquants/périmés).
  Aucun profil `CRITICAL` n'existe (inchangé, non fabriqué) ; sans
  incidence particulière sur la gouvernance Git.
- Stratégie de merge : fast-forward-only exclusivement
  (`git switch <base>` puis `git merge --ff-only <work_branch>`) — jamais
  de commit de merge artificiel, jamais de réécriture d'historique ; une
  divergence de `base_branch` est détectée (`git merge-base
  --is-ancestor`) et fait échouer le merge proprement (`MergeRefusedError`
  / statut `CONFLICT` persisté), jamais de rebase/force/résolution
  automatique.
- `auto_merge` reste `False` par défaut (le WorkItem s'arrête à
  `MERGE_READY`) ; testé réellement à `True` sur des dépôts git
  temporaires (fusion réelle, `main` avance exactement au head du
  WorkItem).
- Intégration `MVPManager` opt-in (`git_governance_service`,
  `validation_store` optionnels, comportement pré-Slice-20 inchangé si
  omis) : `prepare_work_item` avant le premier DEVELOPMENT (et repris,
  jamais recréé, sur REWORK/reprise WAITING/reprise RECOVERY —
  `_execute_work_item` reste le point de câblage unique, partagé par les
  trois chemins) ; `capture_head` après chaque exécution développeur ;
  `assert_review_target` avant chaque review (même mécanisme partagé que
  Slice 19, `_run_review`) ; `compute_merge_eligibility`/`merge` (si
  `auto_merge=True`) une fois le WorkItem `COMPLETED`, que ce soit via le
  chemin sans review ou via une review (fraîche ou reprise) approuvée.
- Reprise après restart : `GitGovernanceService.reconcile` reconstruit
  l'état depuis `GitWorkItemStore` + le dépôt réel (jamais depuis la
  branche « actuellement checkoutée ») ; branche manquante alors que le
  store attend un statut non terminal => `GitBranchMissingError`
  fail-closed (jamais de recréation silencieuse) ; head divergent =>
  reconcilié uniquement si le SHA réel correspond à un `ExecutionRecord`
  connu et persisté, sinon `GitHeadDriftError` fail-closed (aucune
  heuristique large) ; un WorkItem déjà `MERGED` reste `MERGED` après
  restart, prouvé par un test dédié.
- Abstraction PR optionnelle : `PullRequestPublisher`/`PullRequestRecord`
  + `GitHubCliPullRequestPublisher` (wrapper `gh pr create`, argv explicite,
  jamais de SDK GitHub, jamais `shell=True`). Une PR n'implique jamais une
  autorisation de merge (testé explicitement) ; `remote_pr_enabled=False`
  par défaut ; **aucune vraie PR/push distant créé pendant cette session**
  — uniquement testé avec un subprocess runner injecté (argv exact
  vérifié), zéro appel réseau dans `pytest`.
- `ReleaseManager` (extension minimale, opt-in via `git_work_item_store`) :
  nouveau `ReleaseCheck` `governed-work-items-merged` — un WorkItem
  `COMPLETED` *gouverné* doit être réellement `MERGED` pour que le
  release gate passe (jamais de release avec du code business gouverné
  encore non fusionné) ; un WorkItem jamais gouverné n'est jamais
  pénalisé. Pas de refonte : aucun tag/GitHub Release/changelog/semver
  ajouté (non requis par cette Slice).
- `RealizationReport` (extension minimale, opt-in via
  `git_work_item_store`) : nouveaux champs `git_base_branch`,
  `git_work_branch`, `git_merge_status`, `git_merged_sha`,
  `git_pull_request_number/url`, et les événements d'audit déjà persistés
  (`branch_prepared`, `head_captured`, `merge_ready`, `merged`,
  `merge_conflict`) apparaissent dans la timeline — projection directe du
  journal `GitWorkItemStore.list_events`, jamais une réanalyse du log git
  brut. Compatible sans `git_work_item_store` (champs `None`, aucune
  section rendue).
- `ApprovalCoordinator`/`RoadmapApplicationService` non touchés — la
  gouvernance Git du cycle de code d'un WorkItem reste découplée de
  l'application de décisions produit sur `ROADMAP.md`.
- Aucun push distant réel, aucune vraie PR, aucun reset credit consommé ;
  aucune session Claude/Codex réelle lancée (seul `claude --help`/`git`/
  repos temporaires locaux ont été utilisés). Smoke local réel exécuté
  (voir `tests/test_mvp_manager_git_governance.py::TestLocalGovernedSmokeE2E`) :
  WorkItem → branche préparée → commit réel du worker (faux mais git-réel)
  → gate PASS → review APPROVED → éligibilité PASS → `auto_merge=True` →
  merge ff-only → `main` avance exactement au head → redémarrage simulé
  (nouvelles instances de store) → `MERGED` toujours observable. **PASS**.
- Nouveaux fichiers de tests : `tests/test_git_governance.py` (73 tests,
  dépôts git temporaires réels — aucun mock de `git` pour les tests
  fondamentaux), `tests/test_mvp_manager_git_governance.py` (10 tests
  d'intégration bout-en-bout), extensions de `tests/test_realization_report.py`
  et `tests/test_release_manager.py`. 876 tests offline PASS (790 + 86).
- Voir `docs/GIT_GOVERNANCE.md` pour le détail : cycle de vie de la
  branche gouvernée, éligibilité au merge, preuve liée au SHA,
  rationnel fast-forward-only, sémantique de reprise.

---

**Revue de roadmap post-Slice 20 (2026-09-13, avec l'utilisateur)** — les
Slices 7-20 de Phase 1 sont DONE ; décision : ouvrir un nouveau cycle QA/
Regression Testing Governance (Slices 21-25 ci-dessous) plutôt que d'y
substituer une nouvelle Phase. Voir `docs/QA_STRATEGY.md` pour l'étude
d'architecture complète (contrats, workflow deux-phases, protection des
tests, classification des échecs, base de connaissance de régression) qui
sous-tend ce découpage — **aucun code fonctionnel n'a été modifié pour
cette revue**, uniquement `ROADMAP.md`/`docs/status.md`/
`docs/QA_STRATEGY.md`.

Principe directeur (même discipline que l'audit OmniRoute, § « Principe :
aucun biais en faveur de notre code ») : ne pas présumer qu'un QA/Test
Agent interne complet doit être construit. `docs/QA_STRATEGY.md` documente
explicitement trois modes d'architecture à garder ouverts jusqu'à preuve
contraire — **INTERNAL_QA** (agent QA interne, intégré au mécanisme
adaptatif Slice 17/19), **EXTERNAL_QA** (une solution spécialisée —
TestSprite, BrowserStack AI Agents, Momentic, Diffblue Cover, ou une
future candidate — utilisée directement comme moteur QA principal, pas
seulement comme un outil de plus), et **HYBRID_QA** (agent interne pour
unit/integration/contract, moteur externe pour E2E/browser/visuel, ou
l'inverse selon la technologie). `ai-dev-orchestrator` conserve dans tous
les cas la gouvernance (WorkItem, Git SHA, quality policy, audit, decision
release, persistence, retries/recovery, merge eligibility) — jamais
déléguée à un fournisseur externe.

Invariant central documenté (`docs/QA_STRATEGY.md` § « Test Protection »)
: un test de référence existant est un actif protégé — ni l'agent QA
interne ni une solution externe ne peut supprimer, affaiblir, skip ou
« self-heal sémantiquement » un test uniquement parce que le nouveau code
échoue, sans décision humaine versionnée traçable (acceptance criteria,
spec, décision d'architecture). Un verdict PASS doit toujours être fondé
sur des preuves exécutables (pytest/Playwright/mypy/Ruff/build/lint/API/
contract/E2E) — un verdict LLM seul n'est jamais une preuve de PASS.

**Slice 21 — QA Architecture + Build-vs-Adopt Study — ✅ DONE (arbitrage utilisateur 2026-09-14)**
- Étude Claude (`docs/QA_BUILD_VS_ADOPT_REPORT_CLAUDE.md`) : `HYBRID`
  80/100, second `BUILD_INTERNAL` 76/100. Étude Codex
  (`docs/QA_BUILD_VS_ADOPT_REPORT.md`) : `BUILD_INTERNAL` 75/100, second
  `HYBRID` 74/100 — les deux indépendantes, la seconde écrite sans lire la
  première (voir provenance dans chaque rapport). **Arbitrage utilisateur
  (2026-09-14, `docs/QA_BUILD_VS_ADOPT_ARBITRATION.md`)**, non tranché par
  les scores : **architecture cible `HYBRID-READY`** (contrats
  engine-independent, `ExternalQAEngine` reste une extension de premier
  ordre, aucun fournisseur externe requis pour le gate initial) ;
  **implémentation immédiate `BUILD_INTERNAL_MINIMAL`** — les deux études
  convergent en pratique sur `InternalQAEngine` first, aucun candidat
  externe ne passant toutes les eliminations gates aujourd'hui.
  TestSprite (limites V3 target/healing documentées par Codex — POC
  seulement après preuve que ces blockers sont levés), Momentic
  (challenger repo-centric sérieux, SHA/recovery/policies à qualifier),
  BrowserStack (spécialiste browser/mobile/visual, à activer sur besoin
  réel d'un target project) et Diffblue (spécialiste Java, génération
  sous protection stricte de baseline) restent des adaptateurs externes
  futurs possibles, aucun approuvé aujourd'hui comme gate final
  obligatoire. **Slice 23 décidée : `InternalQAEngine` MVP mince,
  Python/pytest first** — ne démarre pas cette session.
- Définir formellement les contrats `QARequest`/`QAResult`/`QAEngine` (le
  nom exact — `QAEngine` vs `TestAgent`/`QAProvider`/`VerificationEngine`
  — est lui-même un livrable de l'étude, pas figé d'avance)
- Comparer factuellement Internal QA Agent vs TestSprite vs BrowserStack
  AI Agents vs Momentic vs Diffblue Cover sur les axes listés dans
  `docs/QA_STRATEGY.md` (unit/integration/API/E2E navigateur/mobile/
  visuel, génération de tests, maintenance de tests, analyse d'échecs,
  diff-aware, full-codebase, MCP/API/CLI, headless/CI, langages, coût,
  exécution locale/privée, données envoyées à l'extérieur, auditabilité,
  vendor lock-in, adéquation à une orchestration autonome)
- Étudier les intégrations possibles par MCP, API HTTP, CLI, ou
  plugin/connector — le cœur ne doit dépendre d'aucun protocole
  spécifique
- Définir la policy conceptuelle `qa.mode` (internal/external/hybrid) et
  `qa.preferred_engine`, avec fallback policy explicite (jamais un
  fallback silencieux qui réduit le niveau de vérification requis)
- Choisir le premier spike factuel (TestSprite semble le candidat le plus
  général pour un premier spike d'après sa description publique
  actuelle — diff/codebase scope, test plan, génération de tests, API/
  E2E, exécution, MCP — mais cette Slice ne le sélectionne pas
  définitivement sur cette seule base ; le spike doit confirmer ou
  infirmer)
- Aucune dépendance produit imposée avant décision : pas de nouveau
  module `src/`, pas d'installation TestSprite/BrowserStack/Momentic/
  Diffblue, pas d'appel MCP/SaaS réel
- Critère de décision à la fin de la Slice : choisir explicitement parmi
  `BUILD_INTERNAL`, `ADOPT_TESTSPRITE`, `ADOPT_BROWSERSTACK`,
  `ADOPT_MOMENTIC`, `ADOPT_DIFFBLUE_FOR_JAVA`, `HYBRID`,
  `MULTI_ENGINE_BY_STACK`, ou `DEFER_EXTERNAL_QA` — avec preuve à
  l'appui, jamais par défaut

**Slice 21.5 — Evidence / SHA hardening — ✅ DONE**
- Quatre points techniques relevés par l'audit Codex (Slice 21) et
  **relus/reproduits dans le vrai code avant toute correction** (jamais
  pris pour argent comptant) :
  - **A. Manifest QA obligatoire vide** : `validation.py::_compute_passed`
    — un manifest sans aucune commande `required` produisait déjà
    `passed=True`, reproduit par un test dédié. Comportement légal
    conservé par défaut (ex. un gate entièrement optionnel) ; nouveau
    paramètre opt-in `require_nonempty_mandatory_manifest` (défaut
    `False`, rétrocompatible) sur `_compute_passed`/`QualityGateRunner.
    run_gate`/`ValidationStore.get_gate_result` — quand `True`, un
    manifest vide ou entièrement optionnel ne peut plus jamais produire
    PASS. Primitive prête pour la future QA Final Verification
    obligatoire (Slice 22/23), non construite ici.
  - **B. Policy/manifest snapshot** : `ValidationStore.get_gate_result`
    recalculait `passed` à partir de `get_project_commands` — la
    configuration **courante**, pas celle réellement appliquée au moment
    du run. Reproduit : un ancien PASS pouvait silencieusement devenir
    FAIL (ou l'inverse) après un simple changement de policy, sans
    qu'aucune preuve n'ait changé. Corrigé par
    `ValidationStore.record_manifest`/`get_manifest_for_run` — nouvelle
    table insert-only, un `validation_run_id` reste lié au manifest
    exact appliqué à l'exécution ; `get_gate_result` l'utilise en
    priorité, ne retombant sur la config courante que pour les runs
    antérieurs à cette Slice (aucun manifest enregistré).
  - **C. Invariance read-only** : `QualityGateRunner.run_gate` capturait
    le SHA avant exécution mais ne vérifiait rien après. Nouveau
    paramètre opt-in `verify_repository_unchanged` — si activé et que le
    HEAD a changé pendant l'exécution des commandes configurées, lève
    `ReadOnlyValidationViolationError` (fail closed) au lieu de renvoyer
    un résultat qui pourrait être confondu avec un PASS/FAIL légitime.
    Primitive pour la future QA Phase 2 (read-only), non construite ici.
  - **D. Merge TOCTOU / head drift** : `git_governance.py::merge` fusionnait
    par **nom de branche** (`merge_ff_only(record.work_branch)`), jamais
    par SHA pinné — reproduit avec un vrai dépôt temporaire : eligibility
    calculée pour un head H2, puis la work branch avance à H3 (toujours
    fast-forwardable depuis H2), puis `merge()` fusionnait H3 sur la
    seule preuve de H2. Corrigé : `merge()` relit désormais le tip réel
    de `work_branch` et exige qu'il soit strictement égal à
    `eligibility.head_sha` avant toute mutation de `base_branch` — sinon
    `GitHeadDriftError`, fail closed, aucun rebase/reset/force ; `main`
    n'est jamais touchée dans ce cas. La divergence de `base_branch`
    elle-même reste couverte nativement par `merge_ff_only` (aucune
    logique de verrouillage supplémentaire ajoutée).
- Tests réels dédiés (jamais un mock de `git`) : `TestMandatoryManifest
  Hardening`/`TestPolicySnapshotHardening`/`TestReadOnlyVerification
  Hardening` (`tests/test_validation.py`, 12 tests, dont les scénarios
  "confirms" qui reproduisent le bug avant la correction) et
  `TestMergeHeadDriftHardening` (`tests/test_git_governance.py`, 3
  tests, dépôts git temporaires réels). 892 tests offline PASS (876 +
  16).
- Voir `docs/GIT_GOVERNANCE.md` (§ « Head-drift hardening ») et
  `docs/QA_STRATEGY.md` (§ 8.1) pour le détail.
- N'implémente pas `InternalQAEngine` (reste Slice 23) — uniquement les
  primitives génériques nécessaires.
- Dépend de : Slice 20 (Git governance), Slice 8 (QualityGateRunner) ;
  prépare Slice 22/23 sans les anticiper.

**Slice 22 — QA Governance + Regression Knowledge Base — ✅ DONE**
- Nouveaux modules `src/orchestrator/qa.py` (contrats provider-independent
  `QARequest`/`QAResult`/`QAVerdict`/`QAVerdictStatus`/
  `FailureClassification`/`QARun`/`QARunStatus`/`QAPhase`/`QAPolicy`/
  `QAEvidenceManifest`/`QARunStore`/`QAEngine` (Protocol)/
  `QAEngineCapabilities`/`TestImpactRequest`/`TestImpactResult`, et
  `evaluate_qa_verdict` — la fonction de gouvernance déterministe, jamais
  LLM), `src/orchestrator/qa_knowledge.py` (`.qa/*.yaml`, chargeur/écrivain
  + analyseur Test Impact déterministe), `src/orchestrator/qa_protection.py`
  (baseline de tests protégés, SHA-256). Aucun `InternalQAEngine` ni
  fournisseur externe implémenté (reste Slice 23) ; aucune intégration
  `MVPManager`/`compute_merge_eligibility`/`ReleaseManager` (reste Slice 24).
- **Séparation centrale ENGINE RESULT != GOVERNED VERDICT** : `QAResult`
  (ce qu'un moteur observe, peut porter `engine_reported_status` à titre
  informatif) n'est jamais l'autorité du verdict ; `QAVerdict`
  (`PASS`/`FAIL`/`INCONCLUSIVE`) est calculé exclusivement par
  `evaluate_qa_verdict`, fonction pure des faits déjà persistés (SHA
  exact, manifest obligatoire non vide, aucune régression non résolue,
  aucune mutation de test protégée non autorisée, aucun engine requis
  manquant, statut du run) — ne lit jamais `engine_reported_status`,
  prouvé par deux faux moteurs (style interne/externe) donnant le même
  verdict gouverné pour la même preuve malgré des statuts auto-déclarés
  opposés (`tests/test_qa.py::TestEngineResultNeverAuthoritative`,
  `TestEngineIndependence`).
- `QARunStatus` (`CREATED`/`RUNNING`/`COMPLETED`/`FAILED`/`INTERRUPTED`)
  distinct de `QAVerdictStatus` — un run techniquement `FAILED`/
  `INTERRUPTED`/non terminé produit `INCONCLUSIVE`, jamais un `PASS`
  automatique.
- Policy/manifest snapshotés avec chaque `QARun` — exactement le motif
  `record_manifest`/`get_manifest_for_run` de la Slice 21.5, généralisé à
  la QA : un changement de policy après coup ne peut jamais faire dériver
  silencieusement le sens d'un ancien verdict.
- `QARunStore` (sqlite3, restart-safe) : `record_result`/`record_verdict`
  insert-only (`ResultAlreadyRecordedError`/`VerdictAlreadyRecordedError`
  sur un second appel), transitions de statut explicitement validées,
  journal d'événements insert-only.
- Phases `TEST_AUTHORING`/`FINAL_VERIFICATION` explicites sur
  `QAPolicy`/`QARun`. Final Verification read-only : réutilise
  **verbatim** les primitives Slice 21.5
  (`QualityGateRunner.run_gate(require_nonempty_mandatory_manifest=True,
  verify_repository_unchanged=True)` via
  `orchestrator.qa.run_final_verification_gate`, testé contre un vrai
  dépôt git temporaire) — violation de policy (mutation observée) =>
  `FAIL` ; incapacité infrastructure à prouver le read-only (pas un dépôt
  git) => `INCONCLUSIVE` ; jamais `PASS` dans les deux cas.
- Baseline de tests protégés (`qa_protection.py`) : hachage SHA-256
  déterministe, ensemble de chemins protégés toujours fourni par
  l'appelant (manifest/policy/knowledge base — jamais une convention
  `"tests/"` codée en dur) ; un nouveau fichier hors baseline n'est
  jamais inspecté (donc jamais une violation) ; `TestChangeAuthorization`
  (`ExpectedChangeSource` : `ACCEPTANCE_CRITERIA`/`SPECIFICATION`/
  `ROADMAP_DECISION`/`ARCHITECTURE_DECISION`/`HUMAN_APPROVAL`) requise
  pour qu'une mutation détectée ne bloque pas le PASS.
- `.qa/*.yaml` (`invariants`/`regression-map`/`critical-paths`/
  `known-flaky`) : absence de `.qa/` ou fichier manquant => connaissance
  vide valide, jamais une erreur, jamais une auto-création ; écriture
  atomique (`tempfile.mkstemp`+`os.replace`, même motif que
  `RoadmapApplicationService`/`RealizationReportService`) ; YAML invalide
  ou ID dupliqué => échec fermé ; `known-flaky.yaml` ne convertit jamais
  un FAIL en PASS. Frontière stricte : Git = connaissance produit durable
  (`.qa/`), SQLite = état runtime/audit orchestrateur (`QARunStore`) —
  jamais mélangés (`qa_knowledge.py` n'importe pas `sqlite3`).
- Analyseur Test Impact déterministe minimal (`analyze_test_impact_deterministic`)
  : chemin changé → correspondance préfixe avec `regression-map`/
  `critical-paths` → tests/invariants liés ; aucune analyse AST,
  dépendances multi-langage, ou sémantique (reste Slice 23+) ; un
  changement non lié n'invente rien (résultat vide).
- Ce dépôt seed son propre `.qa/invariants.yaml` minimal (4 invariants
  réellement démontrés par des tests existants : SHA-bound evidence,
  author≠reviewer, no silent quality downgrade, merge H2→H3 head-drift
  Slice 21.5) — pas une migration des 1004 tests dans un regression-map.
- 1004 tests offline PASS (892 + 112 : `tests/test_qa.py` 58,
  `tests/test_qa_knowledge.py` 36 (dont 3 tests dédiés au chargement du
  vrai `.qa/invariants.yaml` de ce dépôt), `tests/test_qa_protection.py`
  18).
- Voir `docs/QA_GOVERNANCE.md` pour le détail complet.
- Dépend de : Slice 21 (arbitrage), Slice 21.5 (primitives d'evidence
  hardening réutilisées verbatim) ; prépare Slice 23/24 sans les
  anticiper.

**Slice 23 — QA Engine MVP — ✅ DONE (Python/pytest uniquement)**
- Nouveau `src/orchestrator/internal_qa_engine.py` : `InternalQAEngine`
  (implémente le `QAEngine` de Slice 22, `run()` synchrone — enveloppe
  interne `asyncio.run` de son propre pipeline async), `InternalQAPlan`
  (zones impactées, tests sélectionnés, invariants requis, commandes
  ciblées/régression, rationale — inclus dans l'evidence du `QARun`),
  `InternalQATestAuthor` (authoring adaptatif), `run_qa_cycle` (séquence
  complète `QARun` create→RUNNING→…→`QAVerdict`→terminal, persistance
  avant tout retour). Composition stricte de l'existant — aucune seconde
  implémentation : `QualityGateRunner` (jamais un second runner de
  subprocess ; une commande ciblée pytest est juste un `ValidationCommand`
  de plus, exécuté via un `project_id` synthétique éphémère par run pour
  ne jamais muter la config durable du vrai projet), `analyze_test_impact_deterministic`
  (Slice 22, verbatim), `run_final_verification_gate`/`ProtectedTestBaseline`
  (Slice 21.5/22, verbatim), `evaluate_qa_verdict` (jamais dupliqué —
  l'engine ne calcule jamais lui-même PASS/FAIL/INCONCLUSIVE).
- **Scope honnête Python/pytest uniquement** : détection de stack minimale
  (`pyproject.toml`/`pytest.ini`/`setup.cfg`/`tox.ini`) ; absence =>
  `UnsupportedStackError` => run `FAILED` => `INCONCLUSIVE` (jamais
  `PASS`) ; aucune prétention JS/Java/mobile/browser/Playwright/
  BrowserStack/TestSprite/Momentic nulle part (code, docs, tests dédiés).
- Sélection des tests déterministe (Slice 22, réutilisée) : chemins
  modifiés → `regression-map`/`critical-paths` → tests/invariants liés ;
  IDs pytest (`fichier.py`, `::test`, `::Classe::test`) supportés tels
  quels, aucun parseur pytest construit. Stratégie deux temps : commande
  ciblée (TEST_AUTHORING, fast feedback) puis régression complète du
  projet en plus (FINAL_VERIFICATION, regression confidence). Aucune
  sélection + régression globale configurée => celle-ci sert de repli ;
  aucune sélection et aucune régression configurée =>
  `NoEvidenceAvailableError` => run `FAILED` => `INCONCLUSIVE`, jamais
  `PASS` sur preuve vide.
- Tests connus flaky (`.qa/known-flaky.yaml`) isolés dans leur propre
  commande (attribution correcte de l'échec, impossible en les
  regroupant dans une seule invocation pytest) ; re-run borné (budget
  extrait de `retry_policy`, jamais infini, testé jusqu'à épuisement du
  budget) ; toute tentative — succès ou échec final — reste dans
  `QAResult.risks`, jamais silencieusement supprimée ; un test qui échoue
  systématiquement reste un vrai échec malgré sa présence dans
  `known-flaky.yaml` (jamais un skip list).
- QA Test Authoring adaptatif : réutilise **exactement** le mécanisme
  existant (`ComplexityEstimationRequest` → `ExecutionRecommendationService`
  → `AdaptiveExecutionSelector` → `RalphExecutionEngine`, Slice 16/17/19,
  aucune seconde implémentation). Capability logique `qa_testing` ajoutée
  à `config/workers.yaml` (alice et victor — aucun worker dédié fabriqué).
  `qa_worker_id != developer_worker_id` **obligatoire** dès qu'un auteur
  existe (forwardé comme `author_worker_id`, la gouvernance
  `WorkerSelector` déjà existante, jamais réimplémentée) ;
  `qa_worker_id != reviewer_worker_id` **préféré, pas requis** — tentative
  avec exclusion, repli automatique sans exclusion si elle ne laisse
  aucun candidat (jamais de blocage avec seulement deux workers).
- Phase TEST_AUTHORING : le worker QA peut ajouter/modifier uniquement
  des tests/fixtures explicitement autorisés et `.qa/`, jamais le code de
  production. Après exécution, faits Git vérifiés **indépendamment**
  (`verify_authoring_git_facts` — jamais une confiance aveugle dans le
  payload structuré `qa.authoring.completed` que le worker émet
  lui-même) ; toute mutation de fichier de production détectée =>
  violation, fail closed, aucune réparation automatique. Un test protégé
  existant modifié sans `TestChangeAuthorization` valide reste bloquant
  (réutilise `qa_protection.py`, Slice 22, verbatim) ; un nouveau test
  peut toujours être ajouté. Bruit runtime (`.ralph/`, `__pycache__`,
  `.pytest_cache`, n'importe quelle profondeur de chemin) et fichiers
  `test_*.py`/`*_test.py` en dehors d'un dossier `tests/` (convention de
  découverte pytest elle-même, pas seulement une convention par
  répertoire) explicitement jamais des violations — trouvé et corrigé
  suite au smoke réel (voir plus bas).
- Événement applicatif strict `qa.authoring.completed`/`qa.authoring.failed`
  (jamais un topic Ralph réservé) : payload JSON structuré
  (`tests_added`/`tests_modified`/`fixtures_added`/`fixtures_modified`/
  `production_files_modified`/`findings`/`risks`/`recommended_actions`) ;
  absent ou invalide => échec fermé (`InvalidQAAuthoringEventError`),
  jamais un fallback permissif comme `review.parse_findings`.
- Classification des échecs déterministe d'abord : `TIMEOUT`/`ERROR` =>
  `ENVIRONMENT_FAILURE` ; un échec pytest brut reste `UNKNOWN` sans plus
  de contexte (jamais deviné `REGRESSION`/`TEST_DEFECT`/`EXPECTED_CHANGE`
  sans preuve) — conservateur, accepté explicitement par le brief.
- `RealizationReport` (Slice 18/20/21.5) enrichi en option (`qa_run_store`
  optionnel, même motif opt-in) : moteur/phase/verdict/SHA
  attendu-observé/compteurs/regressions dans le résumé + timeline
  (`qa_run_created`, `qa_verdict_recorded`, …) — compatible sans store QA
  (section masquée), HTML toujours une projection jamais une source de
  vérité (testé explicitement).
- Aucune intégration `MVPManager`/`compute_merge_eligibility`/
  `ReleaseManager` — confirmé, ces fichiers non modifiés. Aucun moteur
  externe implémenté ; les faux moteurs externes de Slice 22 restent
  compatibles avec le même `evaluate_qa_verdict` (`TestExternalEngineCompatibility`).
- **Smoke réel exécuté et PASS** (`scripts/smoke_internal_qa_real.py`,
  manuel, jamais lancé par pytest) : vrai worker `alice`
  (anthropic/claude_code/sonnet) sélectionné par le mécanisme adaptatif
  réel (aucun worker codé en dur) sur une copie jetable de
  `~/projects/ralph-spike` (bug connu `add()` retourne `a - b`) ; un test
  de régression réel ajouté (`test_review_candidate.py`) prouvant le bug
  (RED confirmé, exécution pytest réelle) ; **aucune modification du code
  de production** ; verdict gouverné `FAIL` +
  `requires_coding_agent=True` (jamais de correction automatique) ;
  `~/projects/ralph-spike` original inchangé (vérifié avant/après). Deux
  bugs réels trouvés et corrigés grâce à ce smoke (voir ci-dessus :
  fichiers `test_*.py` hors `tests/`, bruit `__pycache__` imbriqué).
- Voir `docs/QA_GOVERNANCE.md` pour le détail complet, y compris le
  résultat de l'acceptance self-dogfood (dev réel → QA réel sur une copie
  jetable complète de ce dépôt lui-même).
- Dépend de : Slice 22 (contrats/persistence/`.qa/`/protection), Slice 17
  (mécanisme adaptatif), Slice 21.5 (primitives read-only/manifest).
  Prépare Slice 24 (intégration MVPManager/merge/release) sans l'anticiper.

**Slice 24 — QA/Rework/Review/Merge Integration — ✅ DONE (ACCEPTANCE_DONE 2026-09-16)**
- QA devient une 6e/7e capacité opt-in de `MVPManager` (`qa_engine`/
  `qa_policy`/`qa_run_store`, plus `qa_protected_paths`), composée
  exactement comme les précédentes — omise, le comportement pré-Slice-24
  reste identique au bit près (suite existante de 1074 tests inchangée et
  toujours verte). `qa_engine` n'est utilisé qu'à travers le `Protocol`
  `QAEngine` (`.run(request) -> QAResult`) — `mvp_manager.py` n'importe et
  ne `isinstance`-check jamais `InternalQAEngine` (preuve directe :
  `tests/test_mvp_manager_qa_integration.py::TestProviderIndependence`,
  deux faux moteurs de forme différente traversent la même intégration).
- Workflow réellement implémenté : Development → QA Test Authoring
  (optionnel, adaptatif, avant les gates) → Quality Gates → Review
  indépendante → Final QA Verification (read-only, après review APPROVED,
  ou directement après les gates si review non configurée) → Merge
  Eligibility → Merge. QA FAIL avec `requires_coding_agent=True` → REWORK
  (réutilise entièrement le chemin adaptatif REWORK existant, aucun
  nouveau routeur) ; un FAIL après Final QA invalide naturellement la
  review déjà APPROVED (le HEAD a changé, SHA-binding existant) — une
  nouvelle review est donc obligatoire, jamais sautée. `QAPolicy.
  max_qa_cycles` (compté durablement via `QARunStore`) reste indépendant
  de `ReviewPolicy.max_review_cycles`.
- `compute_merge_eligibility` (git_governance.py) gagne 4 kwargs additifs
  (`qa_required`/`qa_passed`/`qa_git_sha`/`qa_run_terminal`, défaut
  `False`/`None` — rétrocompatibilité totale) ; reste un module pur, ne
  connaît toujours ni `orchestrator.qa` ni aucun moteur QA. `ReleaseManager`
  gagne un check additif `qa-verdict-pass` (même motif que
  `governed-work-items-merged`, Slice 20).
- `WaitPhase.QA_AUTHORING` (nouveau) : une reprise réentre directement en
  `RUNNING` (jamais `READY` — le développement ne doit jamais être rejoué,
  nouvelle transition d'état `RUNNING -> WAITING -> RUNNING`). Recovery :
  une exécution `qa_testing` orpheline est reconciliée comme une exécution
  `developer` (filtre de rôle étendu dans `RecoveryCoordinator`), l'auteur
  exclu reste celui du dernier handoff de développement, jamais le worker
  QA interrompu. `_execute_work_item` factorisé en
  `_continue_after_development`, point de câblage unique partagé par le
  chemin frais et la reprise QA.
- Bug réel trouvé et corrigé pendant l'intégration : `InternalQATestAuthor.
  run_authoring`'s `base_sha` (Slice 23) doit être le head *juste avant*
  l'exécution QA (post-développement), jamais le `base_sha` global du
  WorkItem — sinon chaque commit légitime du développeur est signalé à
  tort comme fichier de production non autorisé.
- Tests : `tests/test_mvp_manager_qa_integration.py` (15 tests, vrais
  dépôts git temporaires, sélection adaptative réelle via
  `AdaptiveExecutionSelector`+`WorkerSelector` réels, moteur QA factice
  scriptable) + ajustements ciblés dans `tests/test_project_state.py`
  (nouvelles transitions `RUNNING↔WAITING`). 1089 tests offline PASS
  (1074 + 15).
- Self-dogfood : **`ACCEPTANCE_DONE` (2026-09-16)** —
  `scripts/self_dogfood_full_pipeline_real.py` (nouveau, premier script
  d'acceptance réel à piloter le vrai `MVPManager.run_next_work_item`
  de bout en bout, jamais un câblage manuel) a fait passer un WorkItem
  gouverné, sur une copie jetable du dépôt, avec les deux providers réels
  (Claude + Codex) : Development → QA Test Authoring → Quality Gate →
  Review indépendante (APPROVED) → QA Final Verification (PASS) →
  éligibilité au merge → **merge réel** (`git merge --ff-only`), plus le
  contrôle négatif obligatoire (même vérification contre le défaut non
  corrigé → `FAIL`, comme exigé). Plan de contrôle (control plane)
  vérifié strictement inchangé avant/après. 5 tentatives réelles ont été
  nécessaires ; chacune a mis au jour un vrai bug d'intégration (jamais
  un artefact du script), corrigé et couvert par un test offline avant la
  tentative suivante :
  1. Le worker QA réel (Codex) modifiait des fichiers hors périmètre
     (`README.md`, `uv.lock`) → prompt QA-authoring durci
     (`internal_qa_engine.py`) pour interdire explicitement les
     commandes de gestion de paquets et les fichiers de config/lock/doc.
  2. `sqlite3.ProgrammingError` cross-thread : `MVPManager._run_qa_cycle`
     invoque un `QAEngine` réel via `asyncio.to_thread` (nécessaire
     puisqu'un moteur synchrone comme `InternalQAEngine` fait son propre
     `asyncio.run()`), mais sa `ValidationStore` avait été créée sur le
     thread principal → `ValidationStore.__init__` ouvre désormais sa
     connexion sqlite avec `check_same_thread=False` (`validation.py`).
  3. `GitHeadDriftError`/`observed_head_sha` erronés : Ralph committe
     systématiquement son propre état interne (`.ralph/`) à **chaque**
     exécution réelle qu'il lance, y compris une estimation de
     complexité ou une review censées être « analyse seule, jamais de
     git » — ce qui avançait le HEAD réel sans que la gouvernance s'y
     attende. Fix général, à la frontière (jamais des `reconcile()`
     ponctuels avant chaque consommateur) : `GitGovernanceService` gagne
     un helper partagé `_same_up_to_noise` (vérification par contenu
     réel via `git diff --name-only`, jamais une étiquette de rôle),
     utilisé par `reconcile()` **et** `compute_merge_eligibility()`
     (nouveau paramètre `noise_path_prefixes` sur les deux) ;
     `InternalQAEngine._observed_head` normalise de même le HEAD observé
     par rapport au `head_sha` attendu, sans jamais toucher
     `evaluate_qa_verdict` (qui reste une fonction pure). `current_head_sha`
     continue de toujours refléter la réalité git ; seules les
     *comparaisons* de SHA-binding tolèrent le bruit. Aucune review ni
     verdict QA n'est jamais réattribué à un SHA qu'il n'a pas réellement
     évalué : `ReviewRecord.git_sha_reviewed`/`qa_git_sha` restent
     exactement la valeur réellement évaluée.
  4. Même famille de bug, point non couvert : `GitGovernanceService.merge()`
     avait sa propre vérification TOCTOU (`actual_tip != eligibility.head_sha`),
     non tolérante au bruit → étendue avec le même `_same_up_to_noise`
     (nouveau paramètre `noise_path_prefixes` sur `merge()` aussi).
  5. Ralph pouvait laisser une modification **non committée** dans
     `.ralph/` (fichier de handoff de session réécrit après son propre
     auto-commit), bloquant `git switch` lors du merge (« local changes
     would be overwritten ») → `merge()` détecte ce cas (tous les
     fichiers modifiés non committés sont du bruit `.ralph/`) et les
     annule (`LocalGitWorkspace.discard_tracked_changes`, `git checkout --
     <fichiers exacts>` uniquement) avant de basculer de branche ; un
     seul fichier réel modifié non committé continue de faire échouer le
     switch normalement.
  1103 tests offline PASS (1089 + 14, nouvelle couverture dans
  `test_git_governance.py`, `test_mvp_manager_git_governance.py`,
  `test_internal_qa_engine.py`).
- **Suivi moyen terme** (non bloquant pour cette slice, voir la section
  « Éléments non re-séquencés explicitement » plus bas) : isoler les
  exécutions read-only vis-à-vis du HEAD gouverné, ou contribuer en amont
  à Ralph pour exposer un réglage explicite désactivant son auto-commit
  de housekeeping pour les rôles qui n'en ont pas besoin.
- Voir `docs/QA_GOVERNANCE.md` § « Slice 24 » pour le détail complet.
- Workflow cible en deux phases (voir `docs/QA_STRATEGY.md` §
  « Position dans le workflow ») : **QA Phase 1 — Test Design/Authoring**
  (avant la review finale ; peut analyser l'impact, créer tests/fixtures,
  compléter la couverture ; ne modifie jamais le code de production ; un
  commit de tests change le HEAD, donc gates+review portent ensuite sur
  ce nouveau HEAD) puis **QA Phase 2 — Final Verification** (après la
  review finale ; read-only sur code/tests ; exécute tests sélectionnés +
  régression pertinente + QA externe éventuel ; ne modifie jamais le HEAD
  déjà reviewé — si un test supplémentaire s'avère nécessaire, renvoie le
  WorkItem en boucle TEST_AUTHORING/REWORK plutôt que de modifier le SHA
  silencieusement)
- SHA-binding QA : un `QAVerdict.PASS` sur un ancien `head_sha` n'autorise
  jamais le merge d'un nouveau `head_sha` — même mécanisme que
  `compute_merge_eligibility` (Slice 20) pour les quality gates/reviews,
  étendu au QA
- `MergeEligibility`/`ReleaseManager` : exigent `QAVerdict.PASS` sur le
  `head_sha` courant lorsque QA est `required` par la policy — jamais de
  décision LLM dans l'éligibilité
- QA FAIL → classification de l'échec → coding/rework agent → nouveau
  HEAD → tests/gates/re-review/QA — cycles bornés (même principe que
  `ReviewPolicy.max_review_cycles`, Slice 9/17) ; au-delà :
  `BLOCKED`/`HUMAN_ESCALATION` selon policy, jamais de boucle autonome
  infinie
- Restart/recovery QA : mêmes garanties que Slice 11/17/19/20 (reconstruit
  depuis les stores persistés, jamais depuis un état en mémoire)
- Dépend de : Slice 22 (SHA-binding/knowledge base) et Slice 23 (un
  moteur QA concret doit exister pour être intégré à la boucle)

**Slice 25 — Advanced QA / External E2E (conditionnelle, non obligatoire)**
- Seulement si un besoin concret est établi par les Slices 21-24 : matrice
  navigateur/device, régression visuelle, capacités avancées TestSprite/
  BrowserStack/Momentic, vérification spécialisée performance/sécurité
- Cette Slice n'est **pas créée comme obligatoire** tant que le besoin
  n'est pas établi par l'usage réel — contrairement aux Slices 21-24,
  elle reste conditionnelle par principe

**Éléments non re-séquencés explicitement** (restent valables, à intégrer
quand le besoin se précise, sans rang fixe) :
- `OllamaAdapter` (provider local/gratuit, hors 3 adapters MVP 0.1)
- Abstraction `Workspace` (`prepare(task)`/`finalize(task, result)`) :
  **partiellement livrée par Slice 20** sous la forme de
  `LocalGitWorkspace`/`GitGovernanceService` (préparation de branche,
  capture de head, éligibilité au merge) — `RalphExecutionEngine`
  (Slice 6) continue de faire son `git rev-parse HEAD` en lecture seule
  directement, sans changement ; une abstraction `Workspace` générique
  multi-fournisseur (au-delà de Git local) resterait à envisager
  seulement si un besoin concret (ex. un `GitHubWorkspace` réel) apparaît
- CLI minimale (`python -m orchestrator ...`) : utile dès que Slice 7
  expose des commandes stables (`mvp create`, `workitem run`, `handoff
  show`, etc.) — pas de rang fixe imposé, à ajouter quand l'ergonomie le
  justifie
- **Isolation des exécutions read-only vis-à-vis du HEAD gouverné**
  (trouvé pendant l'acceptance réelle Slice 24, court terme déjà corrigé
  côté `MVPManager._reconcile_governed_head` — voir `docs/QA_GOVERNANCE.md`
  § Slice 24) : Ralph committe systématiquement son propre état interne
  (`.ralph/`, message `chore: auto-commit before merge`) à **chaque**
  exécution réelle qu'il lance, y compris une estimation de complexité
  supposée « analyse seule, ne touche jamais git ». Le correctif retenu
  pour l'instant réconcilie ce type de dérive uniquement quand elle
  correspond à un `ExecutionRecord.git_sha_after` réellement persisté
  (jamais une heuristique sur le contenu des fichiers). Deux pistes plus
  profondes restent à évaluer si ce pansement s'avère insuffisant : (a)
  faire tourner les exécutions strictement read-only (estimation) dans un
  worktree/copie jetable qui ne touche jamais la branche gouvernée réelle,
  ou (b) contribuer en amont à Ralph pour exposer un réglage explicite
  (ex. `landing.auto_commit: false`) désactivant ce commit automatique
  pour les rôles qui n'en ont pas besoin — jamais recréer Ralph nous-mêmes
  pour ça (principe REUSE FIRST)

**Explicitement hors périmètre Phase 1 — Ralph fournit déjà (REUSE)** :
- Event loop, runtime task queue
- Hats/roles, per-hat backend (Claude/Codex)
- Planner, Builder/TDD, review/rework, Finalizer/completion gates
- Timeouts, event/history persistence
- Workflows builtin utiles (`builtin:code-assist`, `builtin:review`)
- Intégration GitHub (Phase 2, hors Ralph)
- Rôles spécialisés supplémentaires (Phase 3)
- Infrastructure distribuée, UI, parallel orchestration

**Prérequis exécution** :
- Ralph installé localement (`npm install -g @ralph-orchestrator/ralph-cli`)
- Claude Code CLI disponible et authentifié (abonnement Pro ou Max)
- Codex CLI disponible et authentifié (ChatGPT Plus)
- SQLite3 (stdlib Python)

## Modèle conceptuel (aperçu)

Ce résumé sert de repère rapide ; le détail vérifiable est dans
`MVP_SPEC.yaml`.

- **MVP** : périmètre + critères d'acceptation d'un objectif produit.
- **Task** : unité de travail rattachée à un MVP, avec `required_role`,
  `required_capabilities`, et un `status` incluant `RECOVERY_REQUIRED`.
- **Worker** (depuis Slice 15) : identité d'exécution de gouvernance —
  `worker_id`, `provider`, `backend` fixes, `capabilities`, `priority`,
  `enabled`, et un ou plusieurs `ExecutionProfile` déclarés
  (`quality_tier`/`model`/`reasoning_effort`/`cost_rank`). Ce n'est donc
  plus simplement « un couple (provider, modèle, adaptateur) » : le modèle
  concret vit sur l'`ExecutionProfile` choisi, jamais figé sur le Worker
  lui-même. Voir `docs/ADAPTIVE_EXECUTION.md` §4-5.
- **Execution** : une exécution concrète d'un worker sur une tâche —
  identité complète (voir Phase 1 ci-dessus). L'invariant de gouvernance
  réel, vérifiable a posteriori à partir de cette identité, porte sur
  `worker_id` (`Reviewer.worker_id != Author.worker_id`, **obligatoire** —
  `WorkerSelectionPolicy.require_distinct_worker_for_review`) ; un
  `provider` distinct est **préféré** mais jamais requis par défaut
  (`prefer_distinct_provider_for_review=True`,
  `require_distinct_provider_for_review=False`). `model` n'est jamais un
  invariant de gouvernance en lui-même.
- **QuotaWindow** : une fenêtre de quota observée pour un provider (porté
  par `ProviderState`, jamais par un `Worker` individuellement) ; plusieurs
  fenêtres peuvent coexister pour ce provider (quotidienne, horaire,
  concurrente, etc.). Plusieurs workers déclarés sur le même provider
  partagent le même quota observé — ils ne créent jamais des fenêtres
  distinctes.
- **Workspace** : le dépôt de code sur lequel une tâche s'exécute. La
  gouvernance de branche/merge/tag appartient exclusivement à
  `GitGovernanceService`/`LocalGitWorkspace`, jamais au moteur d'exécution
  ni aux workers ; `RalphExecutionEngine` peut effectuer une seule lecture
  Git minimale (`git rev-parse HEAD`, en lecture seule) pour capturer un
  fait d'audit SHA. Une abstraction `Workspace` générique et
  interchangeable n'est pas actuellement implémentée — voir AC-12 de
  `MVP_SPEC.yaml`.

### Phase 2 — GitHub : branches et Pull Requests

> **Slice 20 (Git/PR/merge governance) est DONE** (voir le découpage
> incrémental sous Phase 1) et couvre la gouvernance de branche/merge
> locale + une abstraction PR optionnelle (`PullRequestPublisher`,
> `GitHubCliPullRequestPublisher` via le CLI `gh`, jamais de SDK). Le
> contenu ci-dessous reste le détail de référence pour ce qui n'a pas été
> nécessaire cette Slice (push/merge distant réel, statut CI, flux PR
> complet) — non requis tant qu'un besoin concret ne le justifie pas.

Objectif : remplacer le Git purement local par un flux Git + GitHub complet.

Livrables (esquisse, à détailler en phase 1 via un ADR dédié) :
- Une intégration GitHub (création de PR, lecture de statut CI)
  viendrait s'ajouter à `GitGovernanceService`, qui centralise déjà toute
  décision de branche/merge/tag — pas au moteur d'exécution, qui n'en
  possède aucune (voir « Modèle conceptuel » ci-dessus et AC-12 de
  `MVP_SPEC.yaml` : aucune abstraction `Workspace` générique
  interchangeable n'existe actuellement à modifier ou étendre)
- **REUSE FIRST** : cette future intégration s'appuierait sur le CLI `gh`
  (subprocess, déjà installé et authentifié) pour créer branches/PR/
  commentaires, plutôt que d'écrire un client API GitHub maison ;
  s'inspirer de l'abstraction multi-provider Git de The-PR-Agent/pr-agent
  (`git_providers/`) pour la forme de l'interface (voir
  `docs/ECOSYSTEM.md`, section 10)
- `Task` gagne des champs optionnels `branch_name`, `pr_url`, `pr_status`
  (migration additive, pas de rupture de schéma)
- Politique de merge : uniquement si tests + quality gates + review sont au
  vert

Prérequis : Phase 1 terminée et validée.

### Phase 3 — Séparation Developer / Reviewer — **HISTORICAL / SUPERSEDED**

> Recoupée par **Slice 9** (« Independent author/reviewer orchestration »)
> pour l'indépendance author/reviewer, puis par la décision produit
> `LEAN_FEATURE_FLOW` (2026-09-17) pour le comportement courant — le
> contenu ci-dessous reste le détail de référence pour les rôles
> spécialisés futurs, mais n'est plus l'architecture de gouvernance en
> vigueur.
>
> This section records the historical design intent. The current
> governance invariant is `worker_id` independence
> (`WorkerSelectionPolicy.require_distinct_worker_for_review=True`).
> Provider diversity is preferred but not required
> (`prefer_distinct_provider_for_review=True`,
> `require_distinct_provider_for_review=False`). Model diversity is not a
> governance invariant. See `ROADMAP.md`, « Chemin nominal actuel », for
> the current, authoritative rule.

Objectif historique (au moment de cette Phase) : imposer qu'un agent ne
valide jamais son propre code, et permettre `Developer.model !=
Reviewer.model` (puis `!= Reviewer.provider`) — intention de conception
d'alors, non l'invariant actuel.

Livrables (historique) :
- Règle de routage dans `WorkerSelector` : exclure du rôle Reviewer tout worker
  dont le `model` (puis `provider`) apparaît déjà dans une `Execution` avec
  le rôle Developer sur la même tâche — entièrement dérivé des champs
  `worker_id`/`provider`/`model`/`role` déjà présents sur `Execution` depuis
  la Phase 1, sans nouveau mécanisme
- Rôles spécialisés supplémentaires : Architecture, Tester, Security Reviewer,
  Documentation, Judge
- Rattachement d'une validation de review au `git_sha_after` réellement
  revu (champ déjà présent sur `Execution` depuis la Phase 1)

Prérequis : Phase 2 (les PR donnent le SHA à rattacher).

### Phase 4 — Suivi des quotas et resets

> Recoupée par **Slice 11** (« Quota waiting / interruption / durable
> resume ») dans le découpage incrémental sous Phase 1 — `WAITING_RESET`
> y est positionné comme état d'**orchestration**, jamais dans les
> contrats provider/exécution existants (`ProviderAvailability`,
> `ExecutionStatus`). Nuance retenue depuis Slice 11 : pas de bascule
> automatique de `WorkerSelector` pendant l'attente sans décision
> explicite — voir la note anti-retry-aveugle des principes directeurs.

Objectif : suspendre un provider en quota épuisé, basculer sur un autre
worker, reprendre automatiquement après le reset.

Livrables :
- Peuplement effectif de plusieurs `quota_windows` par worker (au lieu d'une
  seule en Phase 1) : quotidienne, horaire, concurrente, etc.
- Boucle de reprise (scheduler simple) qui repasse chaque fenêtre de
  `WAITING_RESET` → `AVAILABLE` à l'heure prévue (`reset_at`)
- Bascule automatique de `WorkerSelector` vers un worker alternatif pendant
  l'attente

Prérequis : Phase 1 (le schéma `quota_windows` existe déjà, seul son usage
se complexifie).

### Phase 5 — Multi-projets (dont HA-AI)

> Note : l'entité `Project` fait sa première apparition dès **Slice 7**
> (mono-projet : un `Project` possède un workspace, une roadmap, un MVP
> courant, un état persistant). Cette Phase 5 reste le périmètre pour
> **plusieurs** projets isolés dans une seule instance — Slice 7 ne
> l'anticipe pas au-delà du strict nécessaire pour éviter un couplage
> prématuré.

Objectif : piloter plusieurs projets indépendants depuis une seule instance
de l'orchestrateur.

Livrables :
- Entité `Project` (chemin de dépôt, config propre) en base
- Isolation stricte des opérations Git/GitHub par projet
- Documentation du processus d'ajout d'un nouveau projet (ex. HA-AI)

Prérequis : Phases 1 à 4.

### Phase 6 — Orchestration parallèle (exploratoire)

Objectif : exécuter plusieurs tâches en parallèle, sur plusieurs projets ou
plusieurs workers simultanément.

Non planifié en détail avant que les phases précédentes soient stables. Notes
de risques déjà identifiées dans `MVP_SPEC.yaml` / section risques ci-dessous.

## État actuel

- **Phase 0 — DONE** : Spécification et architecture.
- **Phase 0.5 — DONE** : Reuse Spike validant Ralph Orchestrator 2.10.1.
  Résultats détaillés dans `docs/SPIKE_RALPH.md`.
- **Phase 1 — DONE (2026-09-17, clôture MVP 0.1)** : MVP 0.1 (gouvernance +
  sélection + Ralph integration). Voir « Décision produit (2026-09-17) —
  Clôture MVP 0.1 / Phase 1 » plus bas pour la revue de clôture complète
  et `MVP_SPEC.yaml` v3 pour le contrat d'acceptation réaligné.
  - **Slice 0 (Contrats normalisés) — DONE** : `ProviderState`, `ProviderAvailability`,
    `QuotaWindow`, `ResetCredit`, interface `ProviderAdapter.probe()` — voir
    `src/orchestrator/providers/`. Purs, sans dépendance à Claude/Codex/Ralph.
  - **Slice 1 (ClaudeCodeAdapter) — DONE** : voir `src/orchestrator/
    providers/claude_code_adapter.py` et `tests/providers/
    test_claude_code_adapter.py`.
  - **Slice 2 (CodexAdapter) — DONE** : voir `src/orchestrator/
    providers/codex_adapter.py` et `tests/providers/
    test_codex_adapter.py`.
  - **Slice 3 (QuotaManager) — DONE** : voir `src/orchestrator/
    quota_manager.py` et `tests/test_quota_manager.py`.
  - **Slice 4 (WorkerSelector) — DONE** : voir `src/orchestrator/
    worker_selector.py` et `tests/test_worker_selector.py`.
  - **Slice 5 (Persistence execution audit) — DONE (volet Execution
    seulement)** : voir `src/orchestrator/execution_store.py` et
    `tests/test_execution_store.py`. `MVP`/`Task`/`Worker`/`QuotaWindow`
    persistants et réconciliation au démarrage restent à faire.
  - **Slice 6 (RalphExecutionEngine) — DONE** : voir `src/orchestrator/
    ralph_execution_engine.py` et `tests/test_ralph_execution_engine.py`.
    Un seul smoke test réel exécuté (Claude Haiku) pour valider
    l'intégration bout en bout.
  - **Slice 7 (Project/MVP orchestration core + durable handoff) — DONE** :
    voir `src/orchestrator/project_state.py`, `src/orchestrator/handoff.py`,
    `src/orchestrator/mvp_manager.py` et les tests associés. Détail dans la
    section « Découpage incrémental » ci-dessus.
  - **Slice 8 (Project validation commands + quality gates) — DONE** : voir
    `src/orchestrator/validation.py`, intégration minimale/opt-in dans
    `src/orchestrator/mvp_manager.py`, et les tests associés.
  - **Slice 9 (Independent author/reviewer orchestration) — DONE** : voir
    `src/orchestrator/review.py`, statuts `REVIEWING`/`NEEDS_REWORK` dans
    `src/orchestrator/project_state.py`, intégration minimale/opt-in dans
    `src/orchestrator/mvp_manager.py`, et les tests associés.
  - **Slice 10 (Release gate + activity report) — DONE** : voir
    `src/orchestrator/release.py`, `src/orchestrator/activity_report.py`,
    `src/orchestrator/release_manager.py`, cycle MVP étendu
    (`VALIDATING`/`RELEASED`) dans `src/orchestrator/project_state.py`, et
    les tests associés.
  - **Slice 11 (Quota waiting / interruption / durable resume) — DONE**
    (volet quota **et** volet recovery execution-level, tous deux fermés) :
    voir `src/orchestrator/wait.py` et `src/orchestrator/recovery.py`
    (nouveaux), enrichissement diagnostics dans
    `src/orchestrator/worker_selector.py`, statuts `WAITING`/
    `RECOVERY_REQUIRED` dans `src/orchestrator/project_state.py`,
    intégration minimale/opt-in (`wait_store`/`execution_store`) dans
    `src/orchestrator/mvp_manager.py`, et les tests associés.
  - **Slice 12 (Multi-agent release planning + roadmap synthesis) — DONE** :
    voir `src/orchestrator/planning.py` (nouveau : `PlanningStore`,
    `PlanningCoordinator`, `PlanningSnapshot`/`PlannerProposal`/
    `RoadmapProposal`), et les tests associés.
  - **Slice 13 (Notification + optimistic approval window) — DONE** :
    voir `src/orchestrator/approval.py` (nouveau : `ApprovalStore`,
    `ApprovalCoordinator`, `ApprovalWindow`, `ApprovalPolicy`), layered sur
    `RoadmapProposal` (Slice 12) par référence, et les tests associés.
  - **Slice 14 (Apply decided RoadmapProposal + create/start next MVP) —
    DONE** : voir `src/orchestrator/roadmap_application.py` (nouveau :
    `RoadmapApplicationStore`, `RoadmapApplicationService`,
    `RoadmapApplication`), garde de cohérence roadmap_hash, application
    déterministe et idempotente, section `ROADMAP.md` gérée/régénérée
    entre marqueurs dédiés, reconciliation explicite après crash, et les
    tests associés. Ferme la boucle autonome Release N -> MVP N+1.
  - **Slice 15 (Configurable Worker Registry + Execution Profiles) —
    DONE** : voir `src/orchestrator/worker_registry.py` (nouveau :
    `WorkerRegistry`), `src/orchestrator/worker_selector.py` (`Worker`
    devient l'agent logique seul, nouveaux `ExecutionProfile`/
    `QualityTier`), `config/workers.yaml` (exemple réel, sans secret),
    `PyYAML` en dépendance déclarée, et les tests associés. Aucun
    estimator, aucune sélection adaptative de profil (Slices 16/17).
  - **Slice 16 (Complexity pre-flight + persistent execution
    recommendations) — DONE** : voir `src/orchestrator/
    complexity_estimation.py` (nouveau : `ExecutionRecommendationService`,
    `ExecutionRecommendationStore`, `ExecutionRecommendation`,
    `ComplexityEstimationRequest`), fingerprint sha256 déterministe
    implémenté, cache par fingerprint, et les tests associés. Aucune
    sélection finale de worker/profile, aucune intégration `MVPManager`
    (Slice 17).
  - **Slice 17 (Adaptive Worker/Profile Selection for
    development/rework) — DONE** : voir `src/orchestrator/
    adaptive_execution.py` (nouveau : `AdaptiveExecutionSelector`,
    `AdaptiveExecutionDecisionStore`, `AdaptiveExecutionDecision`,
    `resolve_profile`), extension `WorkerSelectionRequest.minimum_quality_tier`,
    `ExecutionProfile.cost_rank`, intégration opt-in dans `MVPManager`
    (development + rework uniquement à cette étape), et les tests
    associés. Review/planning/release-planning rendus adaptatifs depuis
    par la Slice 19.
  - **Slice 18 (Realization reports + real cross-worker cold-resume
    acceptance) — DONE** : voir `src/orchestrator/realization_report.py`
    (nouveau : `RealizationReport`, `RealizationReportStore`,
    `RealizationReportService`), `scripts/smoke_cross_worker_real.py`
    (smoke manuel, jamais lancé par `pytest`), et les tests associés.
    Smoke réel exécuté et **PASS** (voir détail ci-dessus dans le
    découpage) ; preuve versionnée dans `docs/reports/`.
  - **Slice 18.5 (Stabilisation pré-Slice 19) — DONE** : convention
    canonique `code_review` (capability) vs `reviewer` (rôle) corrigée
    dans `MVPManager.REVIEW_CAPABILITY` ; transport `reasoning_effort`
    pour le backend `claude_code` (flag natif `claude --effort`)
    confirmé et câblé dans `RalphExecutionEngine._build_backend_args`.
  - **Slice 19 (Adaptive review/planning integration) — DONE** : voir
    l'entrée détaillée ci-dessus dans le découpage incrémental —
    `MVPManager._select_reviewer_worker` (review adaptatif, fresh +
    reprise WAITING + reprise RECOVERY, un seul point de câblage via
    `_run_review` déjà partagé), `AdaptiveExecutionSelector.select(...,
    author_worker_id=...)`, et `PlanningCoordinator` (planner +
    synthesizer adaptatifs — les deux confirmés être de vraies
    exécutions LLM, CAS B ; `ApprovalCoordinator`/
    `RoadmapApplicationService` non touchés). 790 tests offline PASS.
  - **Slice 20 (Git/PR/merge governance) — DONE** : voir l'entrée
    détaillée ci-dessus dans le découpage incrémental —
    `src/orchestrator/git_governance.py` (`LocalGitWorkspace`,
    `GitGovernancePolicy`, `GitWorkItemRecord`/`GitWorkItemStore`,
    `GitGovernanceService`), intégration opt-in `MVPManager`/
    `ReleaseManager`/`RealizationReport`, fast-forward-only par défaut,
    `auto_merge=False` par défaut (testé réellement à `True`), aucune
    PR/push distant réel cette session. 876 tests offline PASS.
  - **Revue de roadmap post-Slice 20 (2026-09-13, avec l'utilisateur) —
    DONE** : décision d'ouvrir un nouveau cycle **QA/Regression Testing
    Governance** (Slices 21-25, voir l'entrée détaillée ci-dessus et
    `docs/QA_STRATEGY.md`) plutôt que d'inventer unilatéralement la
    suite. Trois modes d'architecture gardés explicitement ouverts —
    `INTERNAL_QA`/`EXTERNAL_QA`/`HYBRID_QA` — sans biais en faveur d'un
    agent QA interne construit maison (même discipline que l'audit
    OmniRoute) ; aucun fournisseur externe (TestSprite/BrowserStack/
    Momentic/Diffblue) sélectionné définitivement à ce stade — cette
    décision reste un livrable de Slice 21. Session documentation
    uniquement : aucun code fonctionnel modifié, aucun test ajouté/
    modifié (876 tests offline PASS, inchangé).
  - **Slice 21 (QA Architecture + Build-vs-Adopt Study) — DONE,
    arbitrage utilisateur 2026-09-14** : voir l'entrée détaillée
    ci-dessus — études indépendantes Claude (`HYBRID` 80/`BUILD_INTERNAL`
    76) et Codex (`BUILD_INTERNAL` 75/`HYBRID` 74), arbitrage documenté
    (`docs/QA_BUILD_VS_ADOPT_ARBITRATION.md`, jamais tranché par les
    scores) : architecture cible `HYBRID-READY`, implémentation
    immédiate `BUILD_INTERNAL_MINIMAL`, Slice 23 = `InternalQAEngine` MVP
    mince Python/pytest first. Aucun fournisseur externe approuvé comme
    gate final aujourd'hui.
  - **Slice 21.5 (Evidence / SHA hardening) — DONE** : voir l'entrée
    détaillée ci-dessus — quatre points de l'audit Codex reproduits puis
    corrigés (manifest QA obligatoire vide, policy/manifest snapshot,
    invariance read-only, merge TOCTOU/head drift). 892 tests offline
    PASS.
  - **Slice 22 (QA Governance + Regression Knowledge Base) — DONE** :
    voir l'entrée détaillée ci-dessus — `src/orchestrator/qa.py`/
    `qa_knowledge.py`/`qa_protection.py`, séparation ENGINE RESULT !=
    GOVERNED VERDICT prouvée par deux faux moteurs, `.qa/*.yaml`
    provider-independent, réutilisation verbatim des primitives Slice
    21.5. Aucun `InternalQAEngine`, aucune intégration `MVPManager`/
    merge/release (restent Slice 23/24). 1004 tests offline PASS.
  - **Slice 23 (QA Engine MVP) — DONE, Python/pytest uniquement** : voir
    l'entrée détaillée ci-dessus — `src/orchestrator/internal_qa_engine.py`
    (`InternalQAEngine`/`InternalQATestAuthor`/`run_qa_cycle`), composition
    stricte de `QualityGateRunner`/Slice 22/`evaluate_qa_verdict` (jamais
    dupliqués), QA Test Authoring adaptatif (capability `qa_testing`
    ajoutée à `config/workers.yaml`), author-exclusion obligatoire,
    aucune intégration `MVPManager`/merge/release. 1074 tests offline
    PASS. Smoke réel PASS ; self-dogfood acceptance BLOCKED_BY_PROVIDER
    (honnête — dev réel réussi, QA bloquée faute d'un second provider
    disponible, dépôt source vérifié inchangé).
  - **Slice 24 (QA/Rework/Review/Merge Integration) — DONE,
    `ACCEPTANCE_DONE` (2026-09-16)** : voir l'entrée détaillée ci-dessus et
    `docs/QA_GOVERNANCE.md` § « Slice 24 » — QA intégrée comme capacité
    opt-in de `MVPManager` (workflow complet Development → QA Authoring →
    Gates → Review → Final QA → Merge Eligibility → Merge), extension
    additive de `compute_merge_eligibility`/`ReleaseManager`, wait/recovery
    QA, cycles bornés indépendants. 1089 tests offline PASS.
    `scripts/self_dogfood_full_pipeline_real.py` a fait passer un
    WorkItem gouverné de bout en bout avec les deux providers réels
    (Claude + Codex) jusqu'au merge réel — voir l'entrée détaillée
    ci-dessus pour le détail complet et
    `docs/reports/self-dogfood-full-pipeline-2026-09-15.html`. La
    tentative précédente (probe quota lu seul, 2026-09-15, un seul
    provider alors disponible) avait échoué en `BLOCKED_BY_PROVIDER`
    déterministe — **statut historique, superseded par le succès du
    2026-09-16 ci-dessus**, conservé ici uniquement comme événement passé.
- **Décision produit (2026-09-16) — `LEAN_FEATURE_FLOW` devient le
  workflow PAR DÉFAUT de `MVPManager`, `GOVERNED_FULL` (Slices 17-24 :
  estimation adaptative avant chaque phase, QA Test Authoring isolée +
  promotion gouvernée, Review isolée en lecture seule, Final QA séparée)
  devient `DEPRECATED` / `REMOVAL_CANDIDATE`** — voir
  `src/orchestrator/mvp_manager.py::WorkflowMode` pour la justification
  complète. Motivation : KISS/YAGNI/Extreme Programming — le pipeline
  `GOVERNED_FULL`, bien que correctement gouverné (validé sur trois runs
  externes réels, mars-rover run3/4/5), s'est avéré disproportionné pour
  une feature quotidienne simple (jusqu'à 8+ exécutions IA réelles et
  plusieurs heures pour un kata trivial, dont l'essentiel n'était pas dû
  à la difficulté du problème mais à l'estimation adaptative et à
  l'isolation systématique).
  - Nouveau flux nominal : `DEV A` → `DEV B` (second développeur
    indépendant, correctif — PAS en lecture seule, il corrige et committe
    directement) → une seule phase `QA` (déterministe, lecture seule,
    réutilise `QAPhase.FINAL_VERIFICATION` tel quel — aucune nouvelle
    phase) → merge gouverné (`GitGovernanceService.compute_merge_eligibility`/
    `merge`, réutilisés à l'identique) → tag Git (`feature/<work-item-id>/done`,
    nouveau : `LocalGitWorkspace.create_tag`). Exactement 2 exécutions LLM
    sur le chemin nominal (DEV A, DEV B) — QA est déterministe/lecture
    seule, jamais un agent IA.
  - QA FAIL : jusqu'à 3 tentatives QA au total (`QAPolicy.max_qa_cycles`,
    déjà 3 par défaut — aucun changement de politique nécessaire) — les
    2 premiers échecs déclenchent `DEV FIX → QA` (jamais un second DEV B) ;
    le 3ème échec passe le WorkItem en `BLOCKED` avec un motif
    `HUMAN_REVIEW_REQUIRED: ...` explicite et ajoute un bloc TODO
    déterministe (jamais réécrit par un LLM) dans le `ROADMAP.md` **du
    projet cible**, committé sur sa propre branche de travail.
  - Aucun nouveau store/table/selector/coordinator : entièrement composé
    à partir des primitives existantes (`WorkerSelector.select(...,
    author_worker_id=...)` pour l'indépendance DEV A/DEV B,
    `GitGovernanceService.prepare_work_item`/`capture_head`/
    `ensure_runtime_exclusion`, `_run_qa_cycle`, `WaitCoordinator` avec
    une nouvelle valeur `WaitPhase.DEV_B_REVIEW`). Les états WorkItem
    existants (`NEEDS_REWORK`, `BLOCKED`, `WAITING`, `COMPLETED`) sont
    réutilisés tels quels — `HUMAN_REVIEW_REQUIRED` n'est pas un nouveau
    statut, c'est `BLOCKED` + un motif texte distinctif.
  - `GOVERNED_FULL` reste sélectionnable explicitement
    (`workflow_mode=WorkflowMode.GOVERNED_FULL`) et sa suite de tests
    existante reste intégralement verte (aucune modification de son
    propre code) — mais il ne reçoit plus de nouvelle capacité, seules
    les régressions critiques y seront corrigées.
  - 10 tests ajoutés (`tests/test_mvp_manager_lean_feature_flow.py`,
    couvrant le flux nominal, DEV B correctif, les 3 tentatives QA bornées
    + `HUMAN_REVIEW_REQUIRED`, l'intégrité workspace/SHA avant QA, et le
    wait/resume DEV B), 1154 tests offline PASS au total (contre 1144
    avant cette décision).
  - **Acceptance réelle externe : non versionnée.** Un rapport
    `docs/reports/mars-rover-lean-feature-flow-2026-09-16.html` avait été
    cité ici comme preuve d'acceptance réelle ; l'audit de clôture MVP 0.1
    (2026-09-17) a confirmé qu'aucun fichier de ce nom n'existe dans le
    dépôt (tracké ou non — `git log --all` sur ce chemin est vide).
    Citation retirée. `LEAN_FEATURE_FLOW` est validé offline par sa suite
    d'intégration dédiée (`tests/test_mvp_manager_lean_feature_flow.py`,
    12 tests au 2026-09-17, voir AC-16 de `MVP_SPEC.yaml`) ; une
    acceptance externe fraîche reste à réaliser dans un futur pilote (kata
    Roman Numerals, voir « Next » plus bas), hors scope de cette clôture.
- **Décision produit (2026-09-17) — Worker pool fallback (config
  uniquement, `provider`/`backend` restent sur `Worker`, aucun refactor
  `ExecutionProfile`).** Revue d'architecture (`WorkerSelector`,
  `WorkerRegistry`, `QuotaManager`, `wait.py`, `handoff.py`) confirmant
  que le seul écart réel entre le comportement observé et l'invariant de
  continuité visé (« un quota provider épuisé ne doit jamais forcer
  `WAITING` si un autre worker compatible sur un provider disponible
  existe ») était la taille du pool : `config/workers.yaml` ne déclarait
  qu'un seul worker par provider (`alice`/anthropic, `victor`/openai), si
  bien qu'un DEV B sans provider différent disponible attendait toujours
  le reset — comportement prouvé par un test déjà existant
  (`tests/test_mvp_manager_lean_feature_flow.py::TestLeanDevBQuotaWait::
  test_no_eligible_dev_b_waits_then_resumes_on_reset`). Décision : YAGNI
  sur le découplage worker↔provider envisagé (`ExecutionProfile` reste
  `quality_tier`/`model`/`reasoning_effort`/`cost_rank` uniquement,
  `provider`/`backend` restent des attributs fixes de `Worker`) — aucun
  besoin concret non couvert par un second worker par provider, coût de
  refactor disproportionné pour un bénéfice nul. Seul changement réel :
  `config/workers.yaml` étendu à 4 workers (`bob`/anthropic,
  `oscar`/openai, capabilities/profils strictement identiques à leur
  worker primaire, `priority: 90` contre `100` pour que la sélection sans
  auteur préfère naturellement le worker primaire). **`src/orchestrator/`
  non modifié** — `WorkerSelector`/`WorkerRegistry`/`WorkerSelectionPolicy`
  fonctionnent sans changement, aucun nom de worker câblé en dur. 4 tests
  offline ajoutés (2 dans `tests/test_worker_registry.py` couvrant la
  forme du pool réel à 4 workers/2 par provider et le miroir
  bob≡alice/oscar≡victor ; 2 dans
  `tests/test_mvp_manager_lean_feature_flow.py` prouvant le repli
  same-provider — anthropic seul disponible et openai seul disponible —
  sans jamais passer par `WAITING`), 1158 tests offline PASS au total
  (contre 1154 avant cette décision). Documentation alignée
  (`ROADMAP.md`, `docs/status.md`, `docs/ADAPTIVE_EXECUTION.md`,
  `docs/QA_GOVERNANCE.md`, `README.md`) pour ne plus présenter
  `Developer.model`/`Developer.provider != Reviewer.*` comme un invariant
  du chemin nominal actuel (l'invariant réel porte sur `worker_id`,
  `provider` distinct restant préféré, jamais requis par défaut). Mars
  Rover (pilote externe) mis en pause par décision utilisateur — le dépôt
  pilote reste intact comme preuve/audit, aucun nouveau run lancé cette
  session, aucun smoke réel, aucune consommation de quota provider réel.
- **Décision produit (2026-09-17) — Clôture MVP 0.1 / Phase 1.** Une revue
  de clôture factuelle (matrice de preuves AC-par-AC contre le code/tests/
  Git réels, aucun nouveau code/test/config) a conclu qu'aucun gap de
  comportement produit ne bloque le MVP 0.1 — seul son contrat
  (`MVP_SPEC.yaml` v2, jamais modifié depuis sa création le 2026-09-12)
  était devenu stale. `MVP_SPEC.yaml` v3 réaligne les 14 critères
  d'origine sur le comportement réellement construit (CLI/Ollama
  de-scopés en options futures, `ModelRouter` → `WorkerSelector`, reset de
  quota simulé → re-probe réel, Workspace 100% abstrait → gouvernance Git
  centralisée avec une exception de lecture assumée) et ajoute 2 critères
  reflétant des capacités construites au-delà du contrat v2 initial
  (AC-15 indépendance auteur/second développeur, AC-16 workflow
  `LEAN_FEATURE_FLOW`) — aucun texte v2 original supprimé, entièrement
  récupérable via `git log -p -- MVP_SPEC.yaml`, chaque critère modifié
  porte un `historical_note` explicite. Statut Slice 24 réconcilié vers
  `ACCEPTANCE_DONE` (2026-09-16, déjà prouvé par
  `scripts/self_dogfood_full_pipeline_real.py`, jamais reflété dans le
  résumé « État actuel » jusqu'ici). Citation d'un rapport d'acceptance
  Lean inexistant retirée (voir ci-dessus). `Phase 1 — DONE`. 1158 tests
  offline PASS, inchangés (aucun code de production touché par cette
  clôture).
- **Next : POST-MVP 0.1 EXPERIMENT / DISCOVERY.** Pas encore un MVP 0.2 —
  décision à prendre avec l'utilisateur après cette clôture. Axes déjà
  convenus, **proposés, non démarrés** : (1) un pilote Lean frais sur un
  kata plus simple (Roman Numerals) — prochain petit pilote envisagé ;
  (2) étude de faisabilité Mistral/Vibe comme provider supplémentaire ;
  (3) étude build-vs-reuse Mammouth AI ; (4) étude de l'écosystème des
  workers/providers gratuits ou à coût marginal nul ; (5) échelle de
  difficulté progressive des projets de validation. Ces axes ne sont pas
  ordonnés au-delà du point (1). CLI/productisation reste une option
  future, non requise pour démarrer ces axes. Toutes les Slices 21-24 du
  cycle QA sont maintenant `DONE` (Slice 24 : `ACCEPTANCE_DONE`) — cette
  session ne décide toujours pas seule si un POC QA externe ou la Slice 25
  (Advanced QA / External E2E, restée conditionnelle) sont réellement
  utiles — voir `docs/QA_GOVERNANCE.md` et
  `docs/QA_BUILD_VS_ADOPT_ARBITRATION.md`. OmniRoute reste une
  qualification future optionnelle, hors roadmap principale — voir
  `docs/OMNIROUTE_ARBITRATION.md`.

## Comment reprendre ce projet à froid

1. Lire ce fichier en entier (il reflète l'état réel, pas la mémoire d'un agent).
2. Lire `MVP_SPEC.yaml` pour les critères d'acceptation de la phase en cours.
3. Lire `docs/ECOSYSTEM.md` pour les décisions build-vs-reuse déjà actées
   avant d'écrire un nouveau composant.
4. Lancer `pytest` pour vérifier l'état de santé du code existant.
5. Vérifier `git log` et l'état des branches pour voir le travail en cours.
6. Ne jamais supposer qu'une phase est terminée sans que ses critères
   d'acceptation mesurables soient effectivement vérifiés (tests verts).
