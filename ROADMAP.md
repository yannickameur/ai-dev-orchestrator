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
  Worker registry, Worker adapters, ModelRouter, QuotaManager,
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

### Phase 0.5 — Reuse Spike

Objectif : appliquer le principe REUSE FIRST jusqu'au bout — ne pas se fier
uniquement à une lecture de code/documentation (Phase 0) pour trancher
BUILD vs REUSE/ADAPT, mais **tester réellement** les composants les plus
proches sur un dépôt jetable avant de décider ce que nous devons construire.

Cette phase est **bloquante** : la Phase 1 ne doit pas commencer tant
qu'elle n'est pas validée par l'utilisateur.

#### Spike Ralph Orchestrator

Sur un dépôt jetable (pas `ai-dev-orchestrator`, pas `ha-ai`), tester :

1. installation et fonctionnement de Ralph ;
2. détection/utilisation de Claude Code comme backend ;
3. détection/utilisation de Codex comme backend ;
4. création d'un plan ;
5. exécution d'une petite tâche de bout en bout ;
6. utilisation de deux hats configurés avec deux backends différents ;
7. une quality gate déclenchée par un test qui échoue ;
8. persistance et reprise après interruption (kill du process) ;
9. le workflow reviewer (preset `presets/review.yml` ou `wave-review.yml`) ;
10. le workflow de revue de PR via `ralph loops publish-review`/`rebase` +
    `gh` (pour vérifier concrètement s'il existe une intégration `gh`/API
    GitHub réelle à l'usage, au-delà de ce que la lecture de code a montré) ;
11. observation des informations d'exécution accessibles : backend, modèle
    si disponible, session, erreurs, usage/coût, timeout ;
12. comportement observé lorsqu'un backend échoue (fallback ? erreur
    bloquante ? aucune action ?).

#### Questions auxquelles le spike doit répondre

Pour chacun de nos composants prévus, classer **après expérimentation**
(pas seulement après lecture de code) parmi `REUSE` / `ADAPT` / `BUILD` /
`DEFER` :

- Task scheduler
- Worker registry
- subprocess manager
- event loop
- persistence
- Workspace abstraction
- Git worktrees
- GitHub PR
- Reviewer
- Quality Gates
- Recovery

#### Critère de décision

Si Ralph couvre correctement une capability à l'usage, nous ne la
réimplémentons pas sans raison démontrable. Notre développement spécifique
doit se concentrer prioritairement sur ce qui manque réellement, notamment :

- gestion des quotas d'abonnements CLI ;
- plusieurs fenêtres de quotas/reset ;
- stratégie free/local > subscription > paid ;
- routage tenant compte des capacités ;
- garantie de politique d'indépendance auteur/reviewer (pas seulement
  configurabilité) ;
- gestion MVP au niveau supérieur, si Ralph ne la couvre pas suffisamment ;
- orchestration multi-projets, si nécessaire.

Livrables de cette phase :
- Un compte-rendu du spike (fichier à définir, ex. `docs/SPIKE_RALPH.md`)
  documentant, pour chaque question ci-dessus, ce qui a été observé
  concrètement (pas supposé) et la classification REUSE/ADAPT/BUILD/DEFER
  qui en résulte
- Mise à jour de la matrice Build vs Reuse de `docs/ECOSYSTEM.md` si le
  spike contredit une décision provisoire actuelle
- Mise à jour de `MVP_SPEC.yaml` uniquement si le spike change réellement le
  périmètre du MVP 0.1

Pour cette phase : aucune installation, aucun développement Python du
projet lui-même — uniquement l'expérimentation sur le dépôt jetable et la
documentation qui en résulte.

Sortie de phase : validation explicite de l'utilisateur.

### Phase 1 — MVP 0.1 : boucle cœur, mono-processus, local

Objectif : une boucle Task → Worker → Exécution → Résultat qui fonctionne de
bout en bout sur un seul projet, en local, sans GitHub.

Livrables :
- Modèles de données `MVP`, `Task`, `Worker`, `Execution`, `QuotaWindow` (SQLite)
- `Execution` porte une identité complète : `task_id`, `worker_id`, `provider`,
  `model`, `role`, `started_at`, `finished_at`, `status`, `exit_code`,
  `session_id` (optionnel), `git_sha_before`/`git_sha_after` (optionnels,
  renseignés quand un workspace Git est utilisé) — cf. section « Modèle
  conceptuel » ci-dessous
- États `Execution.status` : `RUNNING`, `DONE`, `FAILED`, `INTERRUPTED`.
  États `Task.status` : `PENDING`, `IN_PROGRESS`, `DONE`, `FAILED`,
  `RECOVERY_REQUIRED`
- Détection au démarrage du moteur des `Execution` restées `RUNNING`
  (process précédent tué) → passage en `INTERRUPTED`, tâche associée passée
  en `RECOVERY_REQUIRED`. Une tâche en `RECOVERY_REQUIRED` ne peut pas être
  relancée automatiquement : une action explicite de réconciliation est
  requise (même simple : confirmation manuelle) avant tout nouveau `run`
- `WorkerRegistry` chargé depuis `config/workers.yaml` ; chaque `Worker`
  déclare ses `roles` (spécialisations) et ses `capabilities` (types de
  tâches qu'il sait traiter), en plus de son `priority_tier` / `cost_tier`
- `ModelRouter`, ordre de sélection strict :
  1. capacité(s) requise(s) par la tâche (`required_capabilities`)
  2. rôle/spécialisation requis (`required_role`)
  3. disponibilité et quota (via `QuotaManager`)
  4. classe de coût / priorité (`priority_tier`), utilisée en dernier
     comme départage entre candidats déjà éligibles sur 1-3
  Un worker gratuit/local mais inadapté à la tâche (capacité ou rôle
  manquant) n'est jamais sélectionné, même seul disponible.
- `QuotaManager` construit sur une table `quota_windows` (un provider/modèle
  peut avoir **plusieurs fenêtres** : `window_type`, `remaining` ou état
  connu/inconnu, `reset_at`, `observed_at`, `source`). Le MVP 0.1 peut ne
  peupler qu'une fenêtre par worker en pratique, mais le schéma autorise
  plusieurs fenêtres sans migration de rupture. États dérivés d'une fenêtre :
  `AVAILABLE`, `EXHAUSTED`, `WAITING_RESET`, `ERROR`
- Adaptateurs subprocess : Claude Code, Codex CLI, Ollama/Qwen
- Capture systématique : stdout, stderr, exit code, durée, worker, modèle
- Abstraction **Workspace/Repository** : interface (ex. `prepare(task)`,
  `commit_point()`, `finalize(task, result)`) dont dépend le moteur ; une
  implémentation `LocalGitWorkspace` (branche locale par tâche, sans réseau)
  pour le MVP 0.1. Le moteur ne manipule jamais Git directement
- CLI minimal (`orchestrator mvp ...`, `orchestrator task ...`, y compris une
  commande de réconciliation pour les tâches `RECOVERY_REQUIRED`)
- Suite `pytest`

Détail des critères d'acceptation : voir `MVP_SPEC.yaml`.

Explicitement **hors périmètre** pour cette phase : LangGraph, LiteLLM,
OpenHands, Temporal, PR-Agent, infrastructure distribuée, UI complexe,
orchestration parallèle, intégration GitHub, exécution multi-projets,
réconciliation automatique complexe après interruption (une réconciliation
manuelle/explicite suffit, mais le modèle ne doit pas empêcher d'automatiser
cela plus tard).

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
- Règle de routage dans `ModelRouter` : exclure du rôle Reviewer tout worker
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
- Bascule automatique de `ModelRouter` vers un worker alternatif pendant
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

- Phase en cours : **Phase 0.5 — Reuse Spike** (Phase 0 — Ecosystem Study
  terminée et corrigée, `docs/ECOSYSTEM.md` rédigé ; le protocole
  expérimental de la Phase 0.5 est défini, en attente de validation
  utilisateur avant exécution du spike)
- Aucun code Python n'a encore été écrit. Aucune installation effectuée.
- Prochaine étape après validation utilisateur : exécuter le spike sur un
  dépôt jetable, puis démarrer la Phase 1.

## Comment reprendre ce projet à froid

1. Lire ce fichier en entier (il reflète l'état réel, pas la mémoire d'un agent).
2. Lire `MVP_SPEC.yaml` pour les critères d'acceptation de la phase en cours.
3. Lire `docs/ECOSYSTEM.md` pour les décisions build-vs-reuse déjà actées
   avant d'écrire un nouveau composant.
4. Lancer `pytest` pour vérifier l'état de santé du code existant.
5. Vérifier `git log` et l'état des branches pour voir le travail en cours.
6. Ne jamais supposer qu'une phase est terminée sans que ses critères
   d'acceptation mesurables soient effectivement vérifiés (tests verts).
