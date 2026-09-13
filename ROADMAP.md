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

**Important — politique d'approbation optimiste (20 minutes)** : cette
politique de silence = approbation ne concerne QUE la gouvernance
roadmap/MVP (passage automatique au MVP suivant si personne ne répond à une
proposition de synthèse). Elle ne signifie EN AUCUN CAS que l'absence de
réponse autorise une action destructive, financière ou sensible ailleurs
dans le système. Elle devra rester configurable (délai, activation).
Non implémentée avant Slice 13 — voir plus bas.

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
  gouvernance > quota > coût). Jamais dupliqué ailleurs.
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
8. **Le moteur ne dépend d'aucune stratégie Git particulière** : il ne
   connaît qu'une abstraction de workspace/dépôt. La stratégie concrète
   (branche locale, GitHub, worktree, PR) est un détail d'implémentation
   interchangeable.
9. **REUSE FIRST** : avant l'implémentation d'une nouvelle capability,
   rechercher et documenter les implémentations existantes. Préférer
   reuse > adaptation > développement spécifique lorsque la qualité, la
   licence et le coût d'intégration le permettent. Ne pas recréer un
   orchestrateur déjà disponible sans raison. Voir `docs/ECOSYSTEM.md`.

## Phases

### Phase 0 — Spécification (en cours)

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
- `get(provider)` : sert le cache si frais, probe sinon ; si le probe
  échoue et qu'un ancien état existe, le retourne inchangé (jamais présenté
  comme frais, `observed_at` jamais réécrit) plutôt que de fabriquer une
  disponibilité ; sans ancien état, propage `ProviderProbeError`
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

**Slice 14 — Git/PR/merge governance si toujours nécessaire**
- Recoupe l'ancienne Phase 2 (« GitHub : branches et Pull Requests »)
  ci-dessous — `GitHubWorkspace`, CLI `gh`, politique de merge
- Positionnée en dernier dans ce découpage incrémental : à ré-évaluer une
  fois Slices 7-13 en place (peut-être partiellement anticipée si un
  besoin concret apparaît avant)

**Éléments non re-séquencés explicitement** (restent valables, à intégrer
quand le besoin se précise, sans rang fixe) :
- `OllamaAdapter` (provider local/gratuit, hors 3 adapters MVP 0.1)
- Abstraction `Workspace` (`prepare(task)`/`finalize(task, result)`) :
  `RalphExecutionEngine` (Slice 6) fait aujourd'hui du `git rev-parse HEAD`
  en lecture seule directement, sans cette abstraction — suffisant tant
  que Slice 14 (Git/PR/merge) n'est pas requise ; l'abstraction complète
  n'est réintroduite que si/quand ce besoin devient concret
- CLI minimale (`python -m orchestrator ...`) : utile dès que Slice 7
  expose des commandes stables (`mvp create`, `workitem run`, `handoff
  show`, etc.) — pas de rang fixe imposé, à ajouter quand l'ergonomie le
  justifie

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
- **Worker** : un couple (provider, modèle, adaptateur) déclaré en config,
  avec `roles`, `capabilities`, `priority_tier`/`cost_tier`.
- **Execution** : une exécution concrète d'un worker sur une tâche —
  identité complète (voir Phase 1 ci-dessus), c'est la seule source pour
  vérifier plus tard `Developer.model != Reviewer.model` et
  `Developer.provider != Reviewer.provider`.
- **QuotaWindow** : une fenêtre de quota observée pour un provider/modèle
  (il peut y en avoir plusieurs par worker : quotidienne, horaire,
  concurrente, etc.).
- **Workspace** : abstraction du dépôt de code sur lequel une tâche
  s'exécute ; le moteur ne connaît que cette interface, jamais une stratégie
  Git concrète.

### Phase 2 — GitHub : branches et Pull Requests

> Recoupée par **Slice 14** (« Git/PR/merge governance si toujours
> nécessaire ») dans le découpage incrémental sous Phase 1 — le contenu
> ci-dessous reste le détail de référence.

Objectif : remplacer le Git purement local par un flux Git + GitHub complet.

Livrables (esquisse, à détailler en phase 1 via un ADR dédié) :
- Nouvelle implémentation de l'abstraction Workspace (`GitHubWorkspace` :
  création de PR, lecture de statut CI) — le moteur n'est pas modifié
  puisqu'il ne dépend déjà que de l'interface Workspace
- **REUSE FIRST** : `GitHubWorkspace` s'appuie sur le CLI `gh` (subprocess,
  déjà installé et authentifié) pour créer branches/PR/commentaires, plutôt
  que d'écrire un client API GitHub maison ; s'inspirer de l'abstraction
  multi-provider Git de The-PR-Agent/pr-agent (`git_providers/`) pour la
  forme de l'interface (voir `docs/ECOSYSTEM.md`, section 10)
- `Task` gagne des champs optionnels `branch_name`, `pr_url`, `pr_status`
  (migration additive, pas de rupture de schéma)
- Politique de merge : uniquement si tests + quality gates + review sont au
  vert

Prérequis : Phase 1 terminée et validée.

### Phase 3 — Séparation Developer / Reviewer

> Recoupée par **Slice 9** (« Independent author/reviewer orchestration »)
> dans le découpage incrémental sous Phase 1 — le contenu ci-dessous reste
> le détail de référence, notamment pour les rôles spécialisés futurs.

Objectif : imposer qu'un agent ne valide jamais son propre code, et permettre
`Developer.model != Reviewer.model` (puis `!= Reviewer.provider`).

Livrables :
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
- **Phase 1 — EN COURS** : MVP 0.1 (gouvernance + sélection + Ralph integration).
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
- **Next** : Slice 14 — Git/PR/merge governance si toujours nécessaire (à
  ré-évaluer d'abord : rien dans les slices 7-13 ne l'a rendue bloquante
  jusqu'ici) ; sinon, la prochaine brique naturelle est l'application
  réelle d'une `RoadmapProposal` décidée (`APPROVED`/`AUTO_APPROVED`) —
  mutation de `ROADMAP.md` et création du MVP suivant dans
  `ProjectStateStore` — non encore numérotée comme slice explicite, voir
  « Découpage incrémental » ci-dessus.

## Comment reprendre ce projet à froid

1. Lire ce fichier en entier (il reflète l'état réel, pas la mémoire d'un agent).
2. Lire `MVP_SPEC.yaml` pour les critères d'acceptation de la phase en cours.
3. Lire `docs/ECOSYSTEM.md` pour les décisions build-vs-reuse déjà actées
   avant d'écrire un nouveau composant.
4. Lancer `pytest` pour vérifier l'état de santé du code existant.
5. Vérifier `git log` et l'état des branches pour voir le travail en cours.
6. Ne jamais supposer qu'une phase est terminée sans que ses critères
   d'acceptation mesurables soient effectivement vérifiés (tests verts).
