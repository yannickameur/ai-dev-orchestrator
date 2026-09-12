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

**1. Contrats normalisés**
- **ProviderState, ProviderAvailability, QuotaWindow, ResetCredit** (data classes)
  - Sérialisation JSON claire pour échanges inter-modules
  - Champs : provider, account, availability, observed_at, quota_windows, reset_credits
  - Validés par fixtures spike avant implémentation d'adapters

**2. Interface ProviderAdapter**
- Signature minimale : `probe() → ProviderState`
- Garanties : jamais d'exécution (pas `codex exec` dans probe) — ne doit
  **jamais** devenir un moteur d'exécution, l'exécution appartient à
  RalphExecutionEngine (étape 9)

**3. ClaudeCodeAdapter**
- Parse stream-json des quotas natifs
- Retourne ProviderState normalisé

**4. CodexAdapter**
- Requête app-server `account/rateLimits/read`
- Retourne ProviderState normalisé

**5. Fixtures capturées + tests offline**
- Fixtures issues des captures du spike (stream-json Claude, app-server Codex)
- Valident les contrats et adapters sans appel réseau réel avant intégration

**6. QuotaManager**
- Consulte les ProviderAdapters pour découvrir l'état de chaque provider
- Gère la politique de fraîcheur :
  - Chaque observation porte `observed_at`
  - TTL / freshness configurable
  - Re-probe sur provider failure
  - Claude ne doit jamais être appelée juste pour rafraîchir un quota
- États par fenêtre : AVAILABLE, EXHAUSTED, WAITING_RESET, UNKNOWN/STALE, ERROR

**7. WorkerSelector**
- Ordre conceptuel strict :
  1. Capability match (peut-il faire le job ?)
  2. Governance rules (politique acceptée ?)
  3. Availability / Quota (ressource disponible ?)
  4. Cost tier (free > subscription > paid)
- Gouvernance :
  - Author != Reviewer : policy-driven and configurable
    - Preference : reviewer.provider != author.provider
    - Fallback allowed per policy : reviewer.model != author.model
  - Fallback must be explicit and policy-driven (peut être automatique si policy l'autorise)
- Retourne un `SelectedWorker` ou erreur (WAITING_RESET, NO_AVAILABLE, etc.)

**8. Persistence / execution audit** (SQLite)
- `MVP` : périmètre, critères d'acceptation
- `Task` : unité de travail de l'Orchestrateur (pas un Ralph runtime task)
  - Rôles/capabilities requis, statut (PENDING, IN_PROGRESS, DONE, FAILED, RECOVERY_REQUIRED)
- `Worker` : `worker_id` (stable, technique), `display_name` (lisible,
  jamais utilisé pour une décision de gouvernance), provider, `model`,
  `reasoning_effort` (si applicable), rôles, capabilities, coût tier
- `Execution` : identité complète (task_id, worker_id, provider, model,
  reasoning_effort, role, started_at, finished_at, status, exit_code,
  session_id, git_sha_before/after) + lien vers QuotaWindow à l'exécution —
  c'est l'audit trail permettant de vérifier post-facto author != reviewer
- `QuotaWindow` : multiple par provider, window_type, utilisation,
  reset_at, source (Claude stream-json, Codex API, local, etc.)
- Réconciliation : détection au démarrage des `Execution` orphelines
  `RUNNING` → `INTERRUPTED`, tâche en `RECOVERY_REQUIRED` ; réconciliation
  explicite avant retry (pas de retry aveugle)

**9. RalphExecutionEngine (wrapper)**
- Classe encapsulant Ralph 2.10.1
- Lance un processus Ralph avec un preset (code-assist ou review)
- Mappe les task_id / role / required_capabilities → hats Ralph
- Collecte les événements Ralph + résultats
- NE FAIT PAS d'appels Git directs : utilise Workspace
- Événements custom applicatifs (`work.start`, `review.ready`,
  `review.approved`, `review.rejected`) — jamais `task.*` (réservé au
  coordinateur Ralph)
- Ne se fie pas au `reason` de fin de boucle Ralph seul (peu fiable) ; lit
  les événements métier explicites publiés par les hats
- Retourne un `ExecutionResult` exploitable
- L'exécution appartient exclusivement à ce composant, jamais à ProviderAdapter

**10. MVPManager**
- Crée/valide/archive les MVPs
- Stocke les critères d'acceptation
- Lié aux tâches

**Compléments MVP 0.1** (pas de rang fixe imposé par le spike, à intégrer
autour des étapes ci-dessus) :

- **OllamaAdapter** (futur, hors 3 adapters MVP 0.1)
  - Gestion état local / API native

- **Abstraction Workspace (interface découplée du moteur)**
  - Interface : `prepare(task)`, `finalize(task, result)`
  - Le moteur Ralph **manipule Git via subprocess**, jamais via AI Dev Orchestrator
  - Implémentation MVP 0.1 : `LocalGitWorkspace`
    - Prépare un workspace Git local
    - Pas de réseau, pas de GitHub
    - Capture git_sha_before/after pour l'identité d'Execution
    - Stratégie d'isolation (branche locale, worktree, etc.) configurable
  - Workspace est une abstraction pour que le moteur ne dépende d'aucune stratégie Git
    particulière (branche locale, GitHub, worktree, PR) — c'est un détail interchangeable

- **CLI minimal**
  - `python -m orchestrator mvp create --name <name> --criteria <json>`
  - `python -m orchestrator task create --mvp <id> --role developer --capability ...`
  - `python -m orchestrator task run <task_id>`
  - `python -m orchestrator task list --mvp <id>`
  - `python -m orchestrator task reconcile <task_id> --action retry`
  - `python -m orchestrator quota status`

- **Suite de tests**
  - Unitaires : adapters, WorkerSelector, QuotaManager
  - Intégrés : orchestration complète task → Ralph → result
  - Fixtures : faux workers, faux quotas, Ralph mock si nécessaire

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
- **Phase 1 — READY TO START** : MVP 0.1 (gouvernance + sélection + Ralph integration).
  Aucun code Python du projet n'a encore été écrit.
- **Next** : Démarrer Phase 1 avec le contrat Provider Adapters (ProviderState).

## Comment reprendre ce projet à froid

1. Lire ce fichier en entier (il reflète l'état réel, pas la mémoire d'un agent).
2. Lire `MVP_SPEC.yaml` pour les critères d'acceptation de la phase en cours.
3. Lire `docs/ECOSYSTEM.md` pour les décisions build-vs-reuse déjà actées
   avant d'écrire un nouveau composant.
4. Lancer `pytest` pour vérifier l'état de santé du code existant.
5. Vérifier `git log` et l'état des branches pour voir le travail en cours.
6. Ne jamais supposer qu'une phase est terminée sans que ses critères
   d'acceptation mesurables soient effectivement vérifiés (tests verts).
