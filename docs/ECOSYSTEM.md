# ECOSYSTEM.md — Étude d'écosystème / Build vs Reuse

Ce document est une **source de vérité vivante** sur les projets open source
comparables à `ai-dev-orchestrator` et sur les briques potentiellement
réutilisables. Il doit être mis à jour chaque fois qu'un nouveau candidat
sérieux apparaît, ou qu'une décision `USE`/`ADAPT`/`INSPIRE`/`WATCH`/`REJECT`
change (nouvelle version, changement de licence, projet abandonné, etc.).

Principe directeur : **REUSE FIRST** — apprendre des projets existants,
utiliser ce qui existe, améliorer si nécessaire, et ne développer nous-mêmes
que ce qui manque réellement. Voir `ROADMAP.md`, principe directeur #9.

## Méthodologie

- Recherche menée le **2026-09-12** via `gh` CLI (métadonnées : stars, forks,
  contributeurs, dernier commit, licence) et clonage shallow temporaire
  (`git clone --depth 1` dans `/tmp/ecosystem-research/`, supprimé après
  lecture) pour inspecter l'architecture réelle au-delà du README, quand le
  projet le justifiait.
- Aucun code de ces projets n'a été copié ni installé dans ce dépôt.
- Les chiffres d'activité (stars, dernier commit) sont des instantanés à la
  date de recherche et se périment : revérifier avant toute décision `USE`
  définitive.
- Pour toute réutilisation future : vérifier explicitement la licence,
  documenter la provenance dans le commit/PR concerné, préférer une
  dépendance déclarée proprement (fichier de dépendances, sous-module) à un
  copier/coller non traçable.

## Légende des décisions

| Décision  | Signification |
|-----------|----------------|
| `USE`     | Dépendance directe envisageable telle quelle |
| `ADAPT`   | Code ou sous-composant réutilisable moyennant adaptation |
| `INSPIRE` | Aucune réutilisation de code, mais le pattern/l'architecture doit guider notre conception |
| `WATCH`   | Pas d'action maintenant, à resurveiller (maturité, licence, ou pertinence incertaine) |
| `REJECT`  | Non pertinent ou risque trop élevé pour notre besoin |

---

## Résumé exécutif

12 projets étudiés (dont 5 "ralph" homonymes d'auteurs différents — à ne pas
confondre). Aucun ne couvre notre besoin différenciant central : un
**QuotaManager multi-fenêtres pour des quotas d'abonnement CLI** (Claude Code
Max/Pro, Codex via ChatGPT Plus) distinct d'un simple budget d'API payante.
En revanche, plusieurs briques annexes (adaptateur CLI unifié, checkpoint/
resume, abstraction Git multi-provider, patterns de rôles) sont déjà bien
résolues ailleurs et doivent nous servir de référence de conception, voire
de dépendance directe pour certaines (`gh` CLI, `litellm`, workers candidats).

### Projets les plus proches (par ordre de pertinence)

1. **mikeyobrien/ralph-orchestrator** — le concurrent conceptuel le plus
   proche : hats/rôles avec backends indépendants, event bus, tâches et
   mémoires persistantes, quality gates, preset de review de code opérationnel,
   worktrees avec exécution parallèle réelle (loops parallèles + waves). Mais
   en Rust (158k+ LOC), sans QuotaManager de quotas d'abonnement, sans
   intégration GitHub PR via `gh`/API (vérifié en profondeur, voir section 1),
   et sans **garantie de politique** anti-auto-review (la configurabilité
   existe, pas la garantie). Décision provisoire, à confirmer par un test
   expérimental (Phase 0.5 — Reuse Spike).
2. **OpenHands/software-agent-sdk** — architecture la plus proche côté
   Python (Conversation/Event/Critic/subagent registry), mais orientée agent
   OpenHands natif plutôt que pilotage de CLI externes comme workers.
3. **RefoundAI/ralph** — DAG de tâches SQLite + protocole ACP + vérification
   post-tâche : très proche de notre Task scheduler/reviewer conceptuellement,
   mais stale (~6,5 mois sans activité) et en Rust.
4. **nitodeco/ralph** — le plus proche pour la gestion concrète de subprocess
   CLI (timeout, stuck detection, retry), actif, mais retry automatique
   contraire à notre principe anti-retry-aveugle.
5. **langchain-ai/langgraph** — référence de conception la plus mûre pour
   checkpoint/reprise explicite (`thread_id`, `Interrupt`/`Command`).

---

## 1. mikeyobrien/ralph-orchestrator

- **Nom** : Ralph Orchestrator
- **Dépôt** : https://github.com/mikeyobrien/ralph-orchestrator
- **Rôle/catégorie** : Orchestrateur multi-agent générique par boucles, piloté par événements typés et « hats » (personas).
- **Licence** : MIT
- **Activité/maturité** : 3134 stars, 295 forks, 35 contributeurs (dominé par le mainteneur), dernier push 2026-09-10 — très actif. Non archivé. ~158 500 lignes de Rust, workspace de 9 crates (`ralph-proto`, `ralph-core`, `ralph-adapters`, `ralph-tui`, `ralph-cli`, `ralph-bench`, `ralph-e2e`, `ralph-telegram`, `ralph-api`).
> **Correction (2026-09-12, seconde passe de vérification)** : la première
> passe de recherche sous-estimait la couverture réelle de Ralph sur la
> revue de code, l'exécution parallèle, et l'assignation de backends
> différents par rôle. Les points ci-dessous ont été re-vérifiés par clonage
> shallow et lecture directe du code/de la doc (pas seulement le README).
> Un point avancé lors de cette correction n'a en revanche **pas** été
> confirmé : voir « Git/GitHub » ci-dessous.

- **Fonctionnalités pertinentes** :
  - **Multi-backend, dont Claude Code et Codex** : interface commune (`CliBackend`/`CliExecutor` dans `crates/ralph-adapters/src/cli_backend.rs` + `cli_executor.rs`) — chaque backend (claude, codex, gemini, opencode, copilot, forge, amp, roo, omp, custom…) est une fonction de construction produisant une config (commande, args, mode de prompt, format de sortie). Construction fail-closed : backend inconnu → erreur explicite, jamais de fallback silencieux. `auto_detect.rs` détecte la disponibilité en PATH.
  - **Hats/rôles avec backends indépendants** : personas déclarés en YAML (`triggers`, `publishes`, `instructions`, `backend` override, `max_activations`). Chaque hat peut donc être **configuré** avec un backend différent (ex. builder sur `claude`, reviewer sur `codex`) — voir la nuance « configurabilité vs garantie » plus bas. Bus d'événements par topics avec glob matching (`build.*`, `*.error`).
  - **Events** : bus typé (topic/payload/source/target), journalisé en JSONL (`.ralph/events.jsonl`).
  - **Tâches et mémoires persistantes** : pas de SQLite — fichiers plats avec `flock` pour accès concurrent : tâches en JSONL (`.ralph/agent/tasks.jsonl`), mémoires long terme typées (`pattern`/`decision`/`fix`/`context`) en Markdown (`.ralph/agent/memories.md`), injectées en début d'itération avec budget de tokens et filtres, file de merge JSONL append-only.
  - **Backpressure / quality gates** : ⚠️ ce n'est PAS un throttling de quota — ce sont des « gates de qualité » (tests/lint/typecheck/audit/coverage/mutation) à satisfaire avant qu'un hat déclare « done ». Aucune notion de quota d'API/abonnement dans le runtime.
  - **Preset de review de code/PR confirmé** : `presets/review.yml` et `presets/wave-review.yml` (+ copies dans `crates/ralph-cli/presets/`) implémentent un pipeline de revue adversariale multi-hats (`reviewer` → `analyzer` → `synthesizer`/`closer`), utilisable via `ralph run --config presets/review.yml --prompt "Review PR #123"`. Sortie structurée (fichier Markdown avec sections Critical/Suggestions/Nitpicks/Positive). `wave-review.yml` exécute 3 reviewers spécialisés (Rust/Frontend/Docs) en parallèle. C'est un vrai workflow opérationnel de revue, pas juste un concept.
  - **Worktrees et exécution parallèle réelle** (sous-estimés en première passe) : deux mécanismes distincts et confirmés dans `docs/advanced/` — (1) **Parallel Loops** : plusieurs boucles `ralph run` simultanées, la première acquiert `.ralph/loop.lock` et tourne en place, les suivantes spawnent automatiquement dans `.worktrees/<loop-id>/` avec événements/tâches/scratchpad isolés et mémoires partagées par symlink ; (2) **Agent Waves** : parallélisme intra-boucle — un hat dispatche une « wave » d'éléments traités par des instances de backend parallèles, bornées par un réglage `concurrency` (sémaphore).
  - **Reprise après crash** : `loop_lock.rs` (flock avec PID/prompt, sinon spawn dans un worktree séparé), `hooks/suspend_state.rs` (état de suspension écrit en temp-file+rename, `.ralph/suspend-state.json` + signal `.ralph/resume-requested`), `handoff.rs` (génère un `handoff.md` prêt à reprendre).
  - **Git/GitHub** — nuance importante après vérification : Ralph ne modélise pas la Pull Request comme une primitive centrale de son moteur, **mais** fournit un workflow « remote review » opérationnel (`ralph loops publish-review`, `ralph loops rebase`) qui pousse une branche de loop vers un remote Git et écrit un artefact de synthèse local, en réutilisant les conventions worktree/registry/merge-queue existantes. **Point non confirmé** : aucune intégration `gh` CLI ni aucune dépendance à une crate d'API GitHub (`octocrab`, etc.) n'a été trouvée dans le code source (`grep` sur tous les `.rs` : aucun `Command::new("gh")`, aucune entrée `octocrab`/`github` dans les `Cargo.toml` en tant que dépendance). Le fichier `.github/workflows/claude-code-review.yml` présent dans le dépôt est une CI GitHub Actions pour la revue des contributions **au projet ralph-orchestrator lui-même** (via l'action `anthropics/claude-code-action`), pas une fonctionnalité que le binaire `ralph` livre à ses utilisateurs. Donc : « remote review » = push Git générique + résumé local, pas une intégration GitHub PR via `gh` CLI.
- **Chevauchement avec notre roadmap** : fort sur Worker adapters, rôles (hats) avec backends indépendants, event bus, mémoire long terme, reprise après interruption, revue de code multi-hats, exécution parallèle (worktrees + waves). Nul sur QuotaManager multi-fenêtres et sur intégration GitHub PR réelle (gh CLI/API).
- **Réutilisable précisément** : le pattern de `cli_backend.rs` (struct de config + fonctions de construction par backend, pas un trait par adaptateur) est transposable en Python. Le modèle JSONL+`FileLock` (`file_lock.rs`, `task_store.rs`) est une référence si on voulait éviter SQLite. Les presets `review.yml`/`wave-review.yml` sont une référence directe de prompt-engineering pour notre rôle Reviewer.
- **Inspiration seulement** : hats/events pour la modélisation des rôles et du routage ; mémoire long terme typée avec budget d'injection ; `handoff.md` comme reprise lisible par un humain ; verrouillage de loop + worktrees + waves pour l'exécution parallèle ; pipeline reviewer→analyzer→synthesizer comme modèle de revue en plusieurs passes.
- **Manques** : pas de QuotaManager multi-fenêtres (AVAILABLE/EXHAUSTED/WAITING_RESET/ERROR) ; pas d'intégration GitHub PR via `gh`/API (voir nuance ci-dessus) ; pas de ModelRouter dynamique par coût/capacité/disponibilité (le backend est assigné statiquement par hat en config, pas choisi dynamiquement selon quota/coût).
  **Distinction clé sur Developer/Reviewer** : Ralph permet de **configurer** des hats avec des backends différents (ex. builder=claude, reviewer=codex) — la capacité technique existe. Mais rien n'empêche structurellement de configurer `reviewer.backend == builder.backend` : il n'y a **aucune garantie de politique** qui interdise automatiquement à un même modèle/provider de valider son propre travail. C'est précisément notre besoin (la seconde option) qui reste non couvert, pas la première.
- **Coût/complexité d'intégration** : élevé — écosystème Rust monolithique, stack cible probable Python/légère.
- **Risques de dépendance** : MIT (sûr), mais gouvernance très centralisée sur un seul mainteneur, rythme de release rapide (breaking changes possibles), verrouillage fort si on bâtit dessus (format hats/events propriétaire).
- **Décision provisoire** : **INSPIRE, à re-confirmer par un test expérimental** — architecture riche et directement pertinente, couvrant plus de nos briques qu'initialement estimé (review preset, parallélisme réel, backends par rôle), mais toujours dans un autre langage/écosystème et sans les deux briques différenciantes (QuotaManager CLI, garantie de politique anti-auto-review). Voir Phase 0.5 — Reuse Spike dans `ROADMAP.md` : cette décision doit être testée sur un dépôt jetable avant d'être figée.

### Build vs Reuse — décision structurante

**(E) N'en reprendre que certains patterns — décision maintenue après
correction, mais à confirmer expérimentalement (Phase 0.5 — Reuse Spike).**

Justification : ralph-orchestrator résout un problème voisin, et couvre en
réalité davantage de nos briques qu'estimé en première passe (review preset
opérationnel, exécution parallèle réelle via worktrees et waves, backends
assignables par rôle). Il reste néanmoins sans QuotaManager de quotas
d'abonnement multi-fenêtres, sans **garantie de politique** structurelle
Developer≠Reviewer (la configurabilité existe, pas la garantie), et sans
intégration GitHub PR via `gh`/API (nos briques différenciantes, confirmées
non couvertes après vérification du code source). (B) construire dessus ou
(C) le forker imposerait d'adopter tout l'écosystème Rust/YAML/hats et
sacrifierait le contrôle fin sur QuotaManager/ModelRouter/la garantie de
politique qui constituent notre valeur ajoutée. (D) contribuer upstream ne
nous donnerait pas la structure Python légère visée et dépendrait de la
vision d'un mainteneur externe. (A) continuer seul reste
juste, mais ignorer ce projet serait une perte d'apprentissage : ses
patterns concrets (adaptateur CLI unifié par fonctions de construction,
event bus par glob topics, mémoire long terme typée, verrouillage de loop +
suspend-state en temp-file+rename, `handoff.md`) valent d'être copiés
conceptuellement en Python, sans dépendance ni fork.

**Conclusion générale (question posée explicitement) : à ce stade documentaire,
Ralph Orchestrator ne change pas notre décision de construire
`ai-dev-orchestrator`, mais cette conclusion reste provisoire.** Il confirme
que le problème vaut la peine d'être traité, tout en révélant que notre
différenciateur (quotas d'abonnement CLI multi-fenêtres, **garantie de
politique** — pas seulement configurabilité — Developer≠Reviewer,
intégration GitHub PR via `gh`/API) n'est couvert par aucun concurrent
existant, y compris Ralph une fois son périmètre réel mieux compris.
Cette conclusion a été établie par lecture de code, pas par exécution : la
**Phase 0.5 — Reuse Spike** (voir `ROADMAP.md`) doit la confirmer ou
l'infirmer expérimentalement avant que la Phase 1 ne démarre.

---

## 2. Sean-Shmulevich/ralph

- **Nom** : Ralph (Sean Shmulevich)
- **Dépôt** : https://github.com/Sean-Shmulevich/ralph
- **Rôle/catégorie** : CLI solo d'orchestration d'agents de codage à partir d'un PRD, avec fallback entre agents et reprise par état persistant.
- **Licence** : MIT
- **Activité/maturité** : 0 star, 0 fork, 1 contributeur, dernier commit 2026-03-10 (~6 mois d'inactivité). Non archivé mais stale. Rust (~10 modules : `agents/`, `orchestrator/`, `state/`, `watch/`, `git/`, `parser/`, `tui/`).
- **Fonctionnalités pertinentes** : fallback automatique entre agents après N échecs consécutifs (`src/orchestrator/mod.rs`, liste ordonnée type codex→gemini→claude→opencode) avec backoff exponentiel ; circuit breaker (`max_failures` → arrêt explicite, état `LoopState::Failed`) ; double timeout (mur + détection de « stall » sans sortie) ; détection de rate limit **heuristique textuelle** (`src/rate_limit.rs` : recherche de « 429 »/« usage limit »/« rate limit » dans la sortie, extraction best-effort d'un délai, sinon 60s par défaut) — pas de suivi structuré par fenêtre ; persistance en un seul fichier `.ralph/tasks.json` (écriture atomique temp+rename) + `LockFile` avec PID/progression.
- **Chevauchement avec notre roadmap** : fort sur fallback/priorisation de workers et circuit breaker (proche de QuotaManager mais bien plus simple), faible sur le reste (pas de rôles, pas de review IA, pas de GitHub PR).
- **Réutilisable précisément** : rien de dépendable en l'état (Rust, non maintenu) ; à la rigueur lire `orchestrator/mod.rs` et `rate_limit.rs` comme référence à réécrire en Python.
- **Inspiration seulement** : ordre de fallback configurable, backoff exponentiel, distinction timeout dur vs stall, fichier de lock avec PID pour un statut externe lisible.
- **Manques** : pas de QuotaManager par fenêtres/états formels (juste heuristique texte) ; pas de rôles ; pas de contrainte anti-auto-review ; pas de GitHub PR ; persistance très basique.
- **Coût/complexité d'intégration** : faible à moyen pour lecture/inspiration ; élevé pour réutilisation en dépendance.
- **Risques de dépendance** : activité quasi nulle (0 star, 1 contributeur, 6 mois sans commit) — à ne considérer que comme référence de lecture.
- **Décision provisoire** : **WATCH** — pattern fallback+circuit breaker simple et lisible, à garder en référence rapide, jamais comme dépendance.

---

## 3. RefoundAI/ralph

- **Nom** : Ralph (RefoundAI / Studio Sasquatch)
- **Dépôt** : https://github.com/RefoundAI/ralph
- **Rôle/catégorie** : Harness Rust pilotant un agent CLI (Claude Code par défaut) via un DAG de tâches SQLite, cycle spec → plan → decompose.
- **Licence** : MIT
- **Activité/maturité** : 6 stars, 1 contributeur, dernier commit 2026-02-27 (créé 2026-01-30, ~1 mois de développement intense puis silence ~6,5 mois). Non archivé mais stale.
- **Fonctionnalités pertinentes** : DAG de tâches en SQLite avec dépendances, détection de cycle, auto-transitions (déblocage des enfants, auto-complete/fail du parent) ; intégration réelle au protocole **ACP** (`agent-client-protocol`, SDK Rust de Zed) pour parler à tout agent CLI conforme ; « sigils » texte (`<task-done>`, `<task-failed>`, `<next-model>`) comme protocole de signalisation agent→orchestrateur ; agent de vérification en lecture seule après chaque tâche ; gestion SIGINT propre avec collecte de feedback utilisateur mi-boucle ; stratégies de sélection de modèle (cost-optimized, escalate, plan-then-execute) ; mémoire double (journal SQLite FTS5 + knowledge base Markdown zettelkasten).
- **Chevauchement avec notre roadmap** : fort avec Task/DAG/scheduler, Worker adapters, et le principe « verify avant trust » proche de notre Reviewer/Judge. Le concept ACP est structurellement proche de notre ModelRouter/Worker registry.
- **Réutilisable précisément** : rien à dépendre directement (Rust) ; le schéma logique de `src/dag/db.rs` (tables tasks/dependencies/transitions) et `src/acp/sigils.rs` (grammaire de sigils) sont des références concrètes à relire avant d'implémenter notre Task scheduler.
- **Inspiration seulement** : sigils texte comme canal de signalisation simple et parsable ; agent de vérification en lecture seule distinct de l'exécuteur ; auto-transitions cascadées du DAG ; interruption SIGINT avec feedback avant reprise.
- **Manques** : pas de QuotaManager multi-fenêtres, pas de rôles spécialisés (Architecture/Security Reviewer/Documentation/Judge), pas d'abstraction Workspace découplée de Git, pas de multi-projets, un seul provider de modèles dans les stratégies.
- **Coût/complexité d'intégration** : élevé pour réutiliser le code (Rust), faible pour s'en inspirer.
- **Risques de dépendance** : mono-contributeur à l'arrêt depuis ~6 mois ; MIT donc pas de risque légal, mais aucune garantie de maintenance.
- **Décision provisoire** : **INSPIRE** — le modèle DAG/sigils/verify-then-trust vaut d'être étudié en détail pour notre Task scheduler, code non réutilisable tel quel.

---

## 4. nitodeco/ralph

- **Nom** : Ralph (nitodeco)
- **Dépôt** : https://github.com/nitodeco/ralph
- **Rôle/catégorie** : CLI TypeScript/Bun pour développement piloté par PRD (JSON), orchestrant Claude Code, Cursor CLI ou Codex CLI en boucle longue.
- **Licence** : MIT
- **Activité/maturité** : 19 stars, 2 contributeurs, dernier commit le jour de la recherche — très actif (release automation, CI, docs Astro). Statut actif.
- **Fonctionnalités pertinentes** : configuration en couches (global `~/.ralph/config.json` + par-projet) avec `agentTimeoutMs`, `stuckThresholdMs` (détection d'agent bloqué par absence d'output, kill du process), `maxRetries`/`retryDelayMs`, `maxRuntimeMs` ; `AgentProcessManager` — cycle de vie subprocess avec SIGTERM puis SIGKILL différé, tracking par id, abort global/par-process, compteur de retry ; multi-agent adapters (Cursor/Claude/Codex) sélectionnés par config ; notifications (webhook, fichier marker, notif OS) ; détection de complétion et analyse d'échecs dédiées.
- **Chevauchement avec notre roadmap** : direct avec nos Worker adapters (subprocess CLI) et notre logique de reprise — mais leur « retry » est un retry automatique par timeout, contraire à notre principe **jamais de retry aveugle** (états INTERRUPTED/RECOVERY_REQUIRED + réconciliation explicite).
- **Réutilisable précisément** : le patron `AgentProcessManager.ts` (`src/lib/services/AgentProcessManager.ts`) — cycle de vie subprocess, SIGTERM→SIGKILL avec délai configurable, tracking d'état par id — directement adaptable pour nos Worker adapters, même en Python (asyncio subprocess + timeout).
- **Inspiration seulement** : seuil `stuckThresholdMs` basé sur l'absence d'output comme heuristique de blocage ; configuration en couches global/projet ; notifications de complétion.
- **Manques** : pas de DAG/dépendances formelles, pas de SQLite (JSON), pas de QuotaManager multi-fenêtres, pas de rôles spécialisés, pas de PR/branche Git formalisée, retry automatique contraire à notre contrainte anti-retry-aveugle.
- **Coût/complexité d'intégration** : moyen — patron transposable, mais le modèle de retry doit être réécrit pour respecter nos états.
- **Risques de dépendance** : jeune et actif (bon signe), 2 contributeurs seulement, API interne encore mouvante ; MIT sans risque légal.
- **Décision provisoire** : **ADAPT** — le patron `AgentProcessManager`/stuck-detection est réutilisable en l'adaptant à nos états (WAITING_RESET/RECOVERY_REQUIRED) plutôt qu'à leur retry automatique.

---

## 5. changkun/ralph

- **Nom** : ralph (changkun)
- **Dépôt** : https://github.com/changkun/ralph
- **Rôle/catégorie** : Harness Go implémentant 4 patterns d'agents de l'article « Goalless Agents » : standalone, think+act, think+act+evaluator, think+act+evaluator+archivist.
- **Licence** : ⚠️ aucun fichier `LICENSE` dans le dépôt malgré une mention « MIT » en texte dans le README — **statut légal ambigu, à clarifier directement avec l'auteur avant toute réutilisation**.
- **Activité/maturité** : 0 star, 0 fork, 1 contributeur, dernier commit 2026-03-30 (créé 2026-03-14, ~2 semaines d'activité puis arrêt ~5,5 mois). Projet expérimental/démonstrateur de blog, stale.
- **Fonctionnalités pertinentes** : architecture à 4 rôles — Strategist (propose un seul objectif suivant), Executor (l'implémente), Evaluator (vérifie sans re-planifier), Archivist (documente une connaissance durable dans CLAUDE.md/AGENTS.md) ; séparation stricte exécution/validation ; persistance de session par round (`round-XXX-<role>.json`) permettant une reprise (`ResumeRound` scanne `.ralph/` et reprend au dernier round complété) ; commit/push Git traité comme infrastructure, pas comme rôle agent ; interface `Backend` unique abstrayant Claude Code CLI et Codex CLI.
- **Chevauchement avec notre roadmap** : fort conceptuellement avec nos rôles (Developer/Reviewer/Judge) et notre contrainte anti-auto-review — leur séparation Executor/Evaluator va dans le même sens, en plus minimaliste (4 rôles fixes vs nos 8).
- **Réutilisable précisément** : rien en l'état sans clarifier la licence ; `internal/loop/resume.go` (52 lignes) illustre un mécanisme de reprise par scan de fichiers de round, utile comme référence de conception uniquement.
- **Inspiration seulement** : découpage Strategist/Executor/Evaluator/Archivist, et surtout l'Archivist comme rôle dédié à la mémoire durable inter-sessions — pertinent pour enrichir notre rôle Documentation ; traitement du commit Git comme infrastructure et non comme rôle agent.
- **Manques** : pas de DAG de tâches, pas de SQLite, pas de QuotaManager, pas de GitHub/PR, pas de multi-projets, un seul niveau de granularité (round global).
- **Coût/complexité d'intégration** : élevé pour réutiliser le code (Go, très petit projet), faible pour s'en inspirer.
- **Risques de dépendance** : projet expérimental à l'arrêt, licence non clarifiée malgré la mention README — ne pas copier de code sans confirmation de l'auteur.
- **Décision provisoire** : **WATCH** — intéressant pour le rôle Archivist/mémoire durable, mais trop petit, stale et à la licence ambiguë pour aller plus loin.

---

## 6. OpenHands/OpenHands

- **Nom** : OpenHands (le dépôt a été réorienté vers « Agent Canvas »)
- **Dépôt** : https://github.com/OpenHands/OpenHands
- **Rôle/catégorie** : Frontend/control center self-hosted pour piloter des agents de code (OpenHands, Claude Code, Codex, Gemini, tout agent compatible ACP) — plus une UI React/TS que le moteur d'agent lui-même.
- **Licence** : MIT
- **Activité/maturité** : 87 593 stars, 11 471 forks, ~460 contributeurs, dernier commit 2026-09-11 (quotidien), 799 issues ouvertes. Actif, en évolution rapide (l'ancien moteur « OpenDevin » a été déplacé vers `legacy`).
- **Fonctionnalités pertinentes** : orchestration multi-backend (local/Docker/VM/Cloud), automations déclenchées par webhook ou cron (Slack, GitHub, Linear), profils LLM, intégration ACP pour piloter Claude Code/Codex comme workers externes — exactement le pattern « worker adapter » visé.
- **Chevauchement avec notre roadmap** : fort conceptuellement (multi-worker, multi-projet, webhooks/scheduling) mais orienté UI web lourde (React, Electron, sandboxes Docker), pas un orchestrateur CLI léger piloté par YAML/SQLite.
- **Réutilisable précisément** : rien de directement dépendable sans embarquer tout le stack frontend ; le protocole **ACP** (Agent Client Protocol) est à étudier comme format d'échange standard avec Claude Code/Codex si on veut l'adopter plus tard (cf. aussi RefoundAI/ralph qui l'utilise déjà).
- **Inspiration seulement** : concept de backend switchable (local/Docker/Cloud) pour l'abstraction Workspace ; modèle d'automations déclenchées par webhook/cron avec run history.
- **Manques** : pas de SQLite léger (Postgres requis côté services), pas de quotas d'abonnement CLI, pas de rôles Reviewer/Security distincts avec contrainte anti-auto-review.
- **Coût/complexité d'intégration** : élevé (stack Node/React/Docker, services séparés).
- **Risques de dépendance** : architecture déjà migrée une fois (OpenDevin → OpenHands moteur → Agent Canvas UI) — risque de re-refonte ; licence MIT sûre.
- **Décision provisoire** : **WATCH** — référence d'architecture multi-agent/multi-backend, mais trop lourd et pas aligné avec l'approche CLI-first légère visée.

## 6bis. OpenHands/software-agent-sdk

- **Nom** : OpenHands Software Agent SDK
- **Dépôt** : https://github.com/OpenHands/software-agent-sdk
- **Rôle/catégorie** : le véritable moteur d'agent (ex-cœur d'OpenHands) — SDK Python/TS + serveur d'agent REST/WebSocket.
- **Licence** : MIT
- **Activité/maturité** : 1096 stars, 512 forks, ~117 contributeurs, dernier commit 2026-09-11 (très actif, publié en paper arXiv 2511.03690, score SWE-Bench 77.6). Actif, jeune (spin-off récent).
- **Fonctionnalités pertinentes** : packages séparés `openhands-sdk` (agent, conversation, event, tool, critic, git, subagent, mcp, skills, plugin, marketplace), `openhands-tools`, `openhands-workspace` (backends docker/apptainer/cloud/remote_api), `openhands-agent-server` (FastAPI : conversation/event/bash/git/workspace/hooks routers, pub/sub d'événements, persistence). Contient `critic/` (proche de notre rôle Judge) et `subagent/registry.py` (proche de notre Worker registry).
- **Chevauchement avec notre roadmap** : très fort — Conversation/Event store ≈ Task/scheduler + persistance ; Critic ≈ Judge ; subagent registry ≈ Worker registry ; `git/cached_repo.py` ≈ abstraction Workspace.
- **Réutilisable précisément** : package pip `openhands-sdk` (classes `LLM`, `Agent`, `Conversation`, `Tool`) utilisable comme moteur d'exécution si on acceptait la dépendance ; `openhands/sdk/critic/` et `openhands/sdk/subagent/registry.py` comme modèles de code à lire avant d'écrire nos équivalents.
- **Inspiration seulement** : séparation stricte SDK / Tools / Workspace / Agent-Server en packages distincts ; event-sourcing de la conversation (persistence + resume) pour notre reprise après interruption ; hooks comme points d'extension.
- **Manques** : pas de quotas d'abonnement multi-provider par fenêtres, pas de contrainte anti-auto-review, pas de pilotage de CLI externes en tant que « workers » subprocess — ici l'agent est l'agent OpenHands lui-même.
- **Coût/complexité d'intégration** : moyen à élevé en dépendance directe (packaging multi-module, FastAPI, event sourcing) ; faible en inspiration.
- **Risques de dépendance** : jeune (quelques mois), API mouvante, mais licence MIT et fort momentum (backing All Hands AI).
- **Décision provisoire** : **ADAPT** — architecture la plus proche de notre besoin côté Python ; à étudier ligne à ligne pour `critic/`, `subagent/registry.py` et `event/conversation_state.py`, sans en faire une dépendance directe tout de suite.

## 6ter. OpenHands/automation

- **Nom** : OpenHands Automation Service
- **Dépôt** : https://github.com/OpenHands/automation
- **Rôle/catégorie** : microservice FastAPI dédié au scheduling/webhooks/dispatch/run-history pour déclencher des conversations OpenHands.
- **Licence** : MIT
- **Activité/maturité** : 22 stars, 36 forks, 25 contributeurs, dernier commit 2026-09-11, statut **beta explicite** (« APIs and features may change without notice »). Actif mais jeune/petit.
- **Fonctionnalités pertinentes** : `scheduler.py` (cron), `dispatcher.py` (dispatch des runs en attente), `models.py`/`schemas.py` (SQLAlchemy + Pydantic), migrations Alembic, API keys par utilisateur, run history.
- **Chevauchement avec notre roadmap** : chevauche notre Task scheduler et une partie de la persistance, mais conçu pour Postgres + backend cloud, pas SQLite local mono-machine.
- **Réutilisable précisément** : découpage `scheduler.py` / `dispatcher.py` / `models.py` comme squelette de fichiers à reproduire, pas le code Postgres/Alembic directement.
- **Inspiration seulement** : séparation claire « qui décide quand » (automation) vs « qui exécute » (agent-server) — transposable à notre séparation Task scheduler / Worker adapters.
- **Manques** : aucune notion de quotas d'abonnement CLI, aucun ModelRouter, pas de rôles spécialisés.
- **Coût/complexité d'intégration** : élevé en dépendance directe (Postgres, Alembic, FastAPI complet), faible en lecture d'architecture.
- **Risques de dépendance** : très jeune, petit, marqué beta — risque de breaking changes fréquent.
- **Décision provisoire** : **INSPIRE** — bon exemple de séparation scheduler/dispatcher/run-history, trop lié à une stack cloud Postgres pour être repris tel quel.

---

## 7. BerriAI/litellm

- **Nom** : LiteLLM
- **Dépôt** : https://github.com/BerriAI/litellm
- **Rôle/catégorie** : AI Gateway / SDK+Proxy pour appeler 100+ API LLM (OpenAI, Anthropic, Bedrock, Azure, VertexAI, Ollama, vLLM…) via un format unifié, avec routing, cost tracking, guardrails, load balancing.
- **Licence** : ⚠️ `NOASSERTION` côté API GitHub — le dépôt mixe en réalité un cœur MIT et des fonctionnalités « Enterprise » propriétaires côté proxy avancé. **À vérifier fichier `LICENSE` précis avant tout usage**, en particulier pour les fonctionnalités de proxy/budget avancées.
- **Activité/maturité** : 58 551 stars, 11 373 forks, ~377 contributeurs, dernier commit 2026-09-12 (quasi quotidien), 5044 issues ouvertes (forte adoption, mais backlog important).
- **Fonctionnalités pertinentes** : `router.py` + `router_strategy/` (lowest-cost, tag-based, complexity-based routing, `budget_limiter.py` pour limites $ par provider/fenêtre temporelle), `router_utils/fallback_event_handlers.py` (chaînes de fallback avec cooldown), provider Ollama natif (local/gratuit), `budget_manager.py`, `cost_calculator.py`.
- **Chevauchement avec notre roadmap** : chevauche la partie « priorité coût » et « fallback entre providers » de notre ModelRouter, et une partie budget $ de QuotaManager — **uniquement pour des API HTTP de complétion**. Vérifié explicitement : `claude_code_endpoints.py` dans le proxy concerne un registre de marketplace de plugins/skills Claude Code servi par le proxy, **pas** le pilotage de sessions Claude Code CLI. Aucune trace de gestion de sessions CLI interactives, de quotas d'abonnement (Claude Code Max/Pro, Codex ChatGPT Plus) ni de subprocess management.
- **Réutilisable précisément** : package pip `litellm` utilisable comme couche d'appel unifiée si/quand on ajoute des workers « API payante » (OpenAI/Anthropic API classique) ou un worker Ollama HTTP ; `litellm.Router(fallbacks=...)` et `router_strategy/budget_limiter.py` réutilisables pour le sous-cas « coût/quota $ par provider API ».
- **Inspiration seulement** : pattern de fallback chain avec cooldown (`_trigger_cooldown_for_failed_deployment`) — bon modèle pour nos états EXHAUSTED/WAITING_RESET, transposé à des CLI plutôt qu'à des endpoints HTTP.
- **Manques (critique)** : LiteLLM route des appels API HTTP stateless, jamais des sessions CLI interactives avec authentification par abonnement (OAuth device flow Claude Code, login ChatGPT Codex). Ne sait ni lancer un subprocess CLI, ni suivre l'épuisement d'un quota d'abonnement non exposé via API, ni gérer un état « session interrompue à reprendre ». **Notre QuotaManager multi-fenêtres par CLI-worker n'a aucun équivalent ici.**
- **Coût/complexité d'intégration** : faible pour le seul sous-ensemble « API payante fallback/budget » ; non pertinent pour la partie CLI-quota (coût nul mais bénéfice nul aussi).
- **Risques de dépendance** : projet très actif mais monolithique (surface API immense), historique de tensions de licence (bascule Enterprise) à vérifier avant usage commercial ; verrouillage faible (couche HTTP interchangeable).
- **Décision provisoire** : **ADAPT** (pour le seul sous-ensemble routing/fallback/budget entre API HTTP payantes ou Ollama local) + **REJECT explicite** pour tout ce qui touche aux quotas d'abonnement CLI, qui reste entièrement à notre charge.

---

## 8. langchain-ai/langgraph

- **Nom** : LangGraph
- **Dépôt** : https://github.com/langchain-ai/langgraph
- **Rôle/catégorie** : Framework d'orchestration bas-niveau pour agents stateful (state machine/graph), exécution durable et persistance.
- **Licence** : MIT
- **Activité/maturité** : ~41 500 stars, dernier commit le jour de la recherche, 279 contributeurs, 778 issues ouvertes. Très actif, poussé par LangChain Inc.
- **Fonctionnalités pertinentes** : `BaseCheckpointSaver` (classe abstraite, `libs/checkpoint/.../base/__init__.py`) avec implémentations SQLite (`libs/checkpoint-sqlite`) et Postgres, clé de reprise via `thread_id` ; mécanisme `Interrupt`/`Command` (`libs/langgraph/langgraph/types.py`) permettant de suspendre un run et de le reprendre avec une valeur de résolution (`Command(resume=...)`) — proche du concept « reprise explicite / human-in-the-loop ».
- **Chevauchement avec notre roadmap** : recoupe fortement notre persistance SQLite et nos états INTERRUPTED/RECOVERY_REQUIRED — LangGraph a déjà un modèle mûr de checkpoint + reprise explicite (jamais de retry aveugle : reprise pilotée par `thread_id`/`interrupt_id`).
- **Réutilisable précisément** : le package `langgraph-checkpoint-sqlite` (PyPI) pourrait servir de moteur de persistance si on adoptait le modèle de graphe de LangGraph ; sinon, le schéma de table de `libs/checkpoint-sqlite/.../sqlite/__init__.py` est un bon exemple de structure.
- **Inspiration seulement** : le pattern `thread_id` comme clé de reprise ; séparation `Checkpoint` (état) / `CheckpointTuple` (état + métadonnées + `PendingWrite`) ; concept d'interruption identifiée par ID plutôt que « reprise au dernier point connu ».
- **Manques** : pas de notion de worker/quota/coût, pas de routage multi-provider LLM par capacité, pas de gestion Git/PR — moteur de graphe LLM, pas un orchestrateur de tâches de développement.
- **Coût/complexité d'intégration** : élevé pour adoption complète (modèle de graphe Python, écosystème LangChain) ; faible pour simple inspiration du schéma de checkpoint.
- **Risques de dépendance** : verrouillage fort à l'écosystème LangChain/LangSmith si utilisé en profondeur ; licence MIT saine ; excellente maintenance.
- **Décision provisoire** : **INSPIRE** — modèle de checkpoint/interrupt = référence de conception solide pour notre persistance et reprise, sans adopter le framework.

---

## 9. microsoft/agent-framework

- **Nom** : Microsoft Agent Framework (MAF)
- **Dépôt** : https://github.com/microsoft/agent-framework
- **Rôle/catégorie** : Framework d'orchestration multi-agent et de workflows (Python/.NET/Go), successeur déclaré d'AutoGen.
- **Licence** : MIT
- **Activité/maturité** : ~13 500 stars, dernier commit le jour de la recherche, 244 contributeurs, 640 issues ouvertes. Projet Microsoft actif.
- **Confirmation AutoGen** : le README pointe explicitement un « Migration from AutoGen » (guide dédié sur learn.microsoft.com), confirmant que MAF est bien positionné comme évolution/remplaçant d'AutoGen — cohérent avec la consigne de ne pas privilégier AutoGen pour un nouveau projet.
- **Fonctionnalités pertinentes** : workflows graph-based (séquentiel, concurrent, handoff, group), checkpointing via `WorkflowCheckpoint`/`CheckpointStorage` (Protocol) avec implémentations `InMemory`/`File` et packages dédiés (`packages/postgres`, `packages/azure-cosmos`) ; abstraction provider très large : packages séparés `anthropic`, `openai`, `bedrock`, `gemini`, `mistral`, `ollama`, `foundry`, `github_copilot`, `claude` — un adapter homogène par provider.
- **Chevauchement avec notre roadmap** : recoupe notre Worker registry/adapters et notre ModelRouter (abstraction provider), ainsi que notre reprise après interruption (checkpoint + restart).
- **Réutilisable précisément** : le package `agent-framework-ollama` (pattern d'adapter local/gratuit) et la Protocol `CheckpointStorage` sont des références directes ; pas de dépendance directe recommandée vu la lourdeur du framework global.
- **Inspiration seulement** : structure « un package par provider » pour l'abstraction LLM ; pattern `CheckpointStorage` comme interface pluggable (mémoire/fichier/Postgres/Cosmos) ; « restartability » comme propriété de premier ordre du workflow.
- **Manques** : pas de QuotaManager multi-fenêtres, pas de priorité coût local>abonnement>payant, pas de contrainte reviewer≠author model, pas de Git/PR natif comme moteur central.
- **Coût/complexité d'intégration** : élevé (écosystème Microsoft/Foundry/Azure) en dépendance directe ; faible en inspiration architecturale.
- **Risques de dépendance** : verrouillage vers l'écosystème Microsoft Foundry/Azure si utilisé en profondeur ; jeune (rebrand AutoGen/Semantic Kernel), API mouvante.
- **Décision provisoire** : **INSPIRE** — bon modèle pour le découpage provider-par-package et l'interface de checkpoint pluggable, trop lourd/orienté Azure pour être une dépendance directe.

---

## 10. The-PR-Agent/pr-agent

- **Nom** : PR-Agent (anciennement Codium-ai/pr-agent)
- **Dépôt** : https://github.com/The-PR-Agent/pr-agent
- **Rôle/catégorie** : Agent de revue de Pull Request automatisée (résumé de PR, suggestions de code, checklist de conformité, Q&A sur diff).
- **Licence** : MIT
- **Activité/maturité** : ~12 950 stars, dernier commit le jour de la recherche, 329 contributeurs, 97 issues ouvertes. Le README indique explicitement : *« community-maintained legacy project of Qodo »* — Qodo a fait évoluer le produit commercial (Qodo Merge) et a cédé ce dépôt à la communauté en maintenance, tout en restant sponsor gold. **Statut : communautaire/legacy**, comme anticipé dans la demande initiale.
- **Fonctionnalités pertinentes** : intégrations multi-plateformes prêtes à l'emploi dans `pr_agent/git_providers/` (`github_provider.py`, `gitlab_provider.py`, `bitbucket_provider.py`, `bitbucket_server_provider.py`, `azuredevops_provider.py`, `gitea_provider.py`, `gerrit_provider.py`, `codecommit_provider.py`, `local_git_provider.py`) ; outils `/review`, `/describe`, `/improve` pilotés par prompts ; action GitHub prête à l'emploi (`action.yaml`).
- **Chevauchement avec notre roadmap** : correspond exactement à notre rôle Reviewer/Security Reviewer côté génération de commentaires sur PR GitHub.
- **Réutilisable précisément** : `pr_agent/git_providers/github_provider.py` comme référence directe pour notre future implémentation `GitHubWorkspace` (Phase 2) ; `action.yaml`/`Dockerfile` comme inspiration pour notre intégration CI de revue.
- **Inspiration seulement** : découpage « un provider Git par fichier » derrière une interface commune (`git_provider.py`) ; `pr_compliance_checklist.yaml` comme pattern de quality gate déclaratif.
- **Manques** : ne couvre pas l'orchestration de tâches multi-agents, pas de contrainte reviewer≠author model, pas de scheduler/quota, se limite à la génération de commentaires — pas de merge/gate automatisé complet.
- **Coût/complexité d'intégration** : faible à moyen — utilisable en dépendance CLI/action externe pour la seule étape de revue GitHub, sans coupler notre cœur d'orchestration.
- **Risques de dépendance** : statut « legacy communautaire » post-Qodo — risque de ralentissement de maintenance à moyen terme malgré l'activité actuelle encore soutenue.
- **Décision provisoire** : **WATCH** — solution mature et proche de notre besoin de revue PR, mais son statut post-Qodo justifie d'observer sa pérennité avant d'en dépendre directement plutôt que d'implémenter notre propre Reviewer/Security Reviewer.

---

## 11. FoundationAgents/MetaGPT

- **Nom** : MetaGPT
- **Dépôt** : https://github.com/FoundationAgents/MetaGPT
- **Rôle/catégorie** : Framework multi-agent généraliste (« AI Software Company ») — orchestration de rôles LLM en pipeline pour produire un projet logiciel complet à partir d'une idée.
- **Licence** : MIT
- **Activité/maturité** : ~70 300 stars, ~8900 forks, ~116 contributeurs. Dernier push observé : 2026-01-21 (~8 mois avant la recherche) — actif mais ralenti. Non archivé.
- **Fonctionnalités pertinentes** : rôles typés (`ProductManager`, `Architect`, `ProjectManager`, `Engineer`/`Engineer2`, `QaEngineer`, `TeamLeader`, `DataAnalyst`) hérités d'une classe `Role` commune, chacun abonné (`_watch`) à des types d'`Action`/messages (bus pub/sub `Environment` plutôt qu'un scheduler central strict). `Team.run(n_round)` boucle jusqu'à idle ou budget épuisé (`NoMoneyException`), avec sérialisation/désérialisation de `Team` pour reprise.
- **Chevauchement avec notre roadmap** : rôles spécialisés proches des nôtres (Product, Architecture, Developer, Tester). Reprise via `Team.serialize/deserialize`, mais coarse-grained (état de toute l'équipe), pas un état de tâche fin type INTERRUPTED/RECOVERY_REQUIRED.
- **Réutilisable précisément** : rien à dépendre directement (framework monolithique, config LLM globale unique, pas de ModelRouter/QuotaManager multi-provider, pas d'adaptateur CLI externe type Claude Code/Codex).
- **Inspiration seulement** : découpage des rôles en classes dédiées avec prompts spécialisés ; pattern pub/sub par abonnement à des types de messages plutôt qu'un graphe figé.
- **Manques** : ⚠️ **point critique** — dans `engineer.py`, la revue de code (`WriteCodeReview`) est effectuée par le même rôle `Engineer` qui a écrit le code, **violation directe de notre contrainte anti-auto-review**. Pas de QuotaManager, pas de ModelRouter coût/capacité, pas d'abstraction Workspace découplée de Git.
- **Coût/complexité d'intégration** : élevé — framework monolithique avec ses propres conventions (Pydantic, Context, LLM config globale).
- **Risques de dépendance** : MIT sans risque. Activité ralentie mais large communauté ; risque de verrouillage architectural si on adopte son modèle de rôles/Environment tel quel.
- **Décision provisoire** : **INSPIRE** — s'inspirer du découpage des rôles et du bus pub/sub, **sans reproduire le pattern d'auto-review** Engineer/WriteCodeReview qui contredit directement notre exigence.

## 11bis. OpenBMB/ChatDev

- **Nom** : ChatDev
- **Dépôt** : https://github.com/OpenBMB/ChatDev — ⚠️ le dépôt a été refondu : la branche `main` (ChatDev 2.0 « DevAll ») est désormais une plateforme générique d'orchestration multi-agent par graphes (DAG, nœuds/edges, executors), plus une simulation d'entreprise logicielle. Le paradigme « entreprise virtuelle » classique (CEO/CTO/Programmer/Reviewer/Tester, ChatChain de phases) est conservé sur la branche legacy `chatdev1.0`.
- **Licence** : Apache-2.0
- **Activité/maturité** : ~34 300 stars, ~4300 forks, ~17 contributeurs visibles. Dernier push (main) : 2026-07-24, actif, non archivé. Rebranding v2.0 annoncé en janvier 2026.
- **Fonctionnalités pertinentes** : sur `chatdev1.0` (`CompanyConfig/Default/ChatChainConfig.json`), pipeline de phases explicite — DemandAnalysis → LanguageChoose → Coding → CodeCompleteAll (cycle) → CodeReview (cycle : CodeReviewComment/CodeReviewModification) → Test (cycle : TestErrorSummary/TestModification) → EnvironmentDoc → Manual — avec `RoleConfig.json` séparé. Modèle de workflow séquentiel typé par phase, proche conceptuellement d'un scheduler de tâches MVP.
- **Chevauchement avec notre roadmap** : le concept de phases enchaînées avec relecture/correction en boucle (Review → Modification) rejoint notre exigence « revue → échec → correction ». Mais rien n'indique, sur `chatdev1.0`, que le reviewer soit un modèle/provider distinct du programmeur.
- **Réutilisable précisément** : rien de directement dépendable : `chatdev1.0` est un prototype de recherche figé, `main` (v2.0) est un moteur de graphe générique sans notion de worker CLI externe, quotas ou Git/PR.
- **Inspiration seulement** : structuration ChatChain en phases avec sous-cycles de correction (Review→Modify, Test→Modify) — bon modèle conceptuel pour notre Task scheduler/état de tâche.
- **Manques** : aucune notion de quotas/coûts multi-provider, pas de séparation garantie Developer≠Reviewer, pas d'intégration GitHub PR native sur la version legacy ; la v2.0 a changé de nature et ne cible plus spécifiquement le développement logiciel.
- **Coût/complexité d'intégration** : élevé — deux bases de code disjointes, aucune alignée avec notre architecture cible.
- **Risques de dépendance** : changement de direction du projet (main devenu générique) = risque de dérive si on suit `main` ; `chatdev1.0` n'est plus développé activement. Apache-2.0 sans risque juridique.
- **Décision provisoire** : **INSPIRE** — reprendre conceptuellement le pattern ChatChain (phases + boucles de correction) de la version legacy, sans dépendre du code d'aucune des deux branches.

---

## 12. SWE-agent/mini-swe-agent et Aider-AI/aider (workers candidats)

Ces deux projets sont étudiés comme **workers de développement candidats**,
pas comme moteurs d'orchestration concurrents — conformément à la consigne.

### SWE-agent/mini-swe-agent

- **Nom** : mini-swe-agent (successeur officiel de SWE-agent/SWE-agent, désormais recommandé par son mainteneur à sa place)
- **Dépôt** : https://github.com/SWE-agent/mini-swe-agent
- **Rôle/catégorie** : Agent de développement autonome minimaliste — boucle agentique bash-only résolvant une tâche de code donnée.
- **Licence** : MIT
- **Activité/maturité** : ~7400 stars, ~1000 forks, ~39 contributeurs. Dernier push 2026-09-07 (quelques jours avant la recherche) — très actif, usage en production cité (Meta, NVIDIA, IBM, Princeton, Stanford). Non archivé.
- **Fonctionnalités pertinentes** : interface CLI scriptable `mini -t "<tâche>" -y -o output.json` (`-y`/`--yolo` = exécution sans confirmation, non interactif en CI) ; sortie = trajectoire JSON structurée (`exit_status`, `submission`, coût, nb d'appels) ; agent défini par ~100 lignes (`DefaultAgent`) avec limites configurables (`step_limit`, `cost_limit`, `wall_time_limit_seconds`) ; environnements pluggables (local, docker, podman, singularity/apptainer, bubblewrap) ; modèles via litellm/openrouter/portkey (large compatibilité, y compris locaux).
- **Chevauchement avec notre roadmap** : aucun côté orchestration — c'est exactement le profil « worker CLI subprocess » que notre Worker adapter doit piloter.
- **Réutilisable précisément** : directement invocable comme adaptateur worker subprocess (commande stable, mode non-interactif documenté, sortie JSON exploitable pour parser succès/échec/coût/soumission). Limites coût/temps/steps intégrées, utiles pour s'aligner avec notre QuotaManager.
- **Inspiration seulement** : boucle « linéaire » (table de messages append-only, `subprocess.run` par action) — modèle simple et robuste pour la sandbox/isolation de nos propres workers.
- **Manques** : ne gère ni Git/PR ni config multi-fichiers projet au sens de notre Workspace ; pas de notion de rôle (reviewer/tester), un seul agent générique.
- **Coût/complexité d'intégration** : faible — CLI simple, sortie JSON parseable, mode non interactif natif, correspond bien au modèle Worker adapter subprocess déjà prévu.
- **Risques de dépendance** : développement rapide (v2 récente avec guide de migration) — épingler une version. Licence MIT sans risque.
- **Décision provisoire** : **ADAPT** en tant que worker candidat — bon candidat pour un `MiniSweAgentWorkerAdapter` (subprocess + parsing JSON de trajectoire), à ajouter après Claude Code CLI/Codex CLI/Ollama.

### Aider-AI/aider

- **Nom** : Aider
- **Dépôt** : https://github.com/Aider-AI/aider
- **Rôle/catégorie** : CLI de pair-programming assisté par IA — édition de code multi-fichiers avec intégration Git native.
- **Licence** : Apache-2.0
- **Activité/maturité** : ~48 900 stars, ~4900 forks, ~172 contributeurs. Dernier push 2026-05-22 (~4 mois avant la recherche), 1861 issues ouvertes (volume élevé mais cohérent avec un projet très utilisé). Actif, non archivé.
- **Fonctionnalités pertinentes** : mode scriptable confirmé (`aider/args.py`) — `--message`/`-m` (ou `--message-file`) envoie un message unique puis quitte ; `--yes-always` auto-confirme ; `--exit` termine après démarrage sans attendre d'entrée ; `--auto-commits`/`--no-auto-commits`/`--dirty-commits` contrôlent le comportement Git ; mode `--architect` (séparation planification/édition, potentiellement deux modèles différents).
- **Chevauchement avec notre roadmap** : Aider committe lui-même en Git — chevauche potentiellement notre abstraction Workspace (branche par tâche) si non contraint (`--no-auto-commits` ou capture de diff sans commit).
- **Réutilisable précisément** : invocation scriptable directe (`aider --message "<tâche>" --yes-always --no-auto-commits <fichiers>`), code retour + diff/état du repo comme résultat exploitable. Le mode `--architect` (modèle planificateur distinct du modèle éditeur) est directement réutilisable pour respecter `Developer.model != Reviewer.model` si on assigne des providers différents aux deux rôles.
- **Inspiration seulement** : séparation architect/editor comme modèle à deux passes, transférable indépendamment de l'outil.
- **Manques** : pas de rôle Reviewer/Tester séparé en soi ; sa gestion Git automatique peut entrer en conflit avec notre Workspace si non désactivée ; pas de QuotaManager/ModelRouter multi-provider intégré.
- **Coût/complexité d'intégration** : faible à moyen — CLI mature, mode non interactif direct, mais nécessite de désactiver les auto-commits pour ne pas casser notre abstraction Workspace.
- **Risques de dépendance** : Apache-2.0 sans risque. Large communauté, rythme de commits en léger ralentissement, nombreuses issues ouvertes (charge de maintenance visible) — risque faible mais à surveiller.
- **Décision provisoire** : **ADAPT** en tant que worker candidat — bon candidat pour un `AiderWorkerAdapter` (subprocess, `--message`/`--yes-always`, auto-commits désactivés), complémentaire à Claude Code CLI/Codex CLI.

---

## Matrice Build vs Reuse par composant

> ⚠️ **Décisions provisoires, en attente de confirmation expérimentale.**
> Après correction de l'étude Ralph Orchestrator (voir section 1), les
> décisions **BUILD** ci-dessous pour *Worker registry*, *Worker adapters*,
> *Task scheduler*, *Recovery*, *Reviewer* et *Quality Gates* reposent
> encore sur une lecture de code, pas sur un test réel. Ralph couvre ces
> briques plus largement qu'estimé en première passe (review preset
> opérationnel, exécution parallèle réelle, backends assignables par rôle).
> La **Phase 0.5 — Reuse Spike** (voir `ROADMAP.md`) doit tester ces
> composants sur un dépôt jetable avant que BUILD ne soit confirmé pour
> chacun ; seuls **QuotaManager** (quotas d'abonnement CLI multi-fenêtres)
> et la **garantie de politique** anti-auto-review restent BUILD quel que
> soit le résultat du spike, aucun projet ne les couvrant même partiellement.

| Composant de notre architecture | Décision | Projet(s) de référence | Notes |
|---|---|---|---|
| **MVP Manager** (MVP + critères d'acceptation) | **BUILD** | INSPIRE nitodeco/ralph (PRD JSON), RefoundAI/ralph (spec→plan) | Aucun projet ne modélise un « MVP » avec critères d'acceptation en entité de premier ordre |
| **Task model** | **BUILD** | INSPIRE RefoundAI/ralph (`src/dag/db.rs`), OpenHands SDK (Conversation/Event) | États INTERRUPTED/RECOVERY_REQUIRED n'existent nulle part ailleurs sous cette forme |
| **Task scheduler** (DAG, dépendances) | **BUILD** | INSPIRE fortement RefoundAI/ralph (DAG SQLite, auto-transitions, cycle detection), OpenBMB/ChatDev legacy (ChatChain phases + boucles de correction) | Design transposé en Python, pas de code réutilisable (Rust) |
| **SQLite persistence** | **BUILD** (schéma propre) + **REUSE** `sqlite3` stdlib Python | — | Aucun package tiers ne correspond à notre schéma (MVP/Task/Worker/Execution/QuotaWindow) |
| **Worker registry** | **BUILD** | INSPIRE ralph-orchestrator (`cli_backend.rs`, construction par backend), microsoft/agent-framework (1 package par provider) | Registre déclaratif YAML propre à notre besoin (rôles + capacités + priorité coût) |
| **Worker adapters** (Claude Code/Codex/Ollama) | **BUILD** | ADAPT nitodeco/ralph (`AgentProcessManager` : SIGTERM→SIGKILL, stuck detection), INSPIRE ralph-orchestrator (construction par backend) | Adaptateurs subprocess propres, patron de cycle de vie process largement inspirable |
| **Worker adapters** (extensions futures) | **ADAPT** | SWE-agent/mini-swe-agent, Aider-AI/aider | Utilisables directement comme workers additionnels (CLI scriptable, sortie parseable) après les 3 adaptateurs MVP 0.1 |
| **ModelRouter** (capacité > rôle > quota > coût) | **BUILD** (cœur) + **ADAPT** partiel | ADAPT BerriAI/litellm (`router.py`/fallback/cooldown) pour le seul sous-cas API payante HTTP ; REJECT litellm pour la partie quotas CLI | Le cœur (capacité/rôle/quota CLI) n'a pas d'équivalent existant |
| **QuotaManager** (fenêtres multiples, CLI) | **BUILD** intégral | INSPIRE Sean-Shmulevich/ralph (heuristique texte de rate-limit), ADAPT litellm `budget_limiter.py` (sous-cas $ API payante) | Notre besoin différenciant central — aucun projet ne le couvre |
| **Recovery / réconciliation** | **BUILD** | INSPIRE fortement ralph-orchestrator (`suspend_state.rs`, `loop_lock.rs`, `handoff.rs`), langchain-ai/langgraph (`Interrupt`/`Command`, `thread_id`), changkun/ralph (resume par round) | Meilleure référence de conception : LangGraph (reprise explicite par ID) + ralph-orchestrator (état de suspension durable) |
| **Workspace abstraction** | **BUILD** (interface) | INSPIRE OpenHands SDK (`openhands-workspace`, backends pluggables), ralph-orchestrator (worktrees + merge_queue) | Interface fine, implémentation `LocalGitWorkspace` seule en MVP 0.1 |
| **Git/GitHub (Phase 2)** | **REUSE** | `gh` CLI (déjà installé/authentifié) comme mécanisme d'accès GitHub plutôt qu'un client API maison ; ADAPT The-PR-Agent/pr-agent (`git_providers/github_provider.py`) comme référence d'abstraction multi-provider Git | Reuse-first concret : ne pas écrire de client API GitHub |
| **PR workflow** | **REUSE** + **ADAPT** | `gh pr create`/`gh pr review`/`gh pr merge` (REUSE) ; ADAPT pr-agent (`pr_compliance_checklist.yaml`, `action.yaml`) pour le format des quality gates déclaratifs | |
| **Reviewer** | **BUILD** (contrainte anti-auto-review) | INSPIRE RefoundAI/ralph (agent de vérification en lecture seule), changkun/ralph (Evaluator séparé), ADAPT pr-agent (format de commentaires `/review`) ; **anti-pattern à éviter** : FoundationAgents/MetaGPT (`Engineer` review son propre code) | Aucun projet n'impose structurellement `Developer.model != Reviewer.model` |
| **Quality Gates** | **BUILD** | INSPIRE ralph-orchestrator (« backpressure » = gates tests/lint/typecheck/audit/coverage/mutation avant « done »), ADAPT pr-agent (`pr_compliance_checklist.yaml`) | |

---

## Enseignements les plus importants

1. **Notre différenciateur central (QuotaManager multi-fenêtres pour quotas
   d'abonnement CLI) n'est couvert par aucun projet étudié.** LiteLLM,
   Microsoft Agent Framework, LangGraph et toute la famille Ralph confondent
   ou ignorent la distinction entre budget d'API payante (bien traité) et
   état d'un abonnement CLI (Claude Code Max/Pro, Codex ChatGPT Plus) — un
   vrai vide que notre projet comble.
2. **La gestion de subprocess CLI (adaptateurs worker) est un problème déjà
   bien résolu ailleurs** : nitodeco/ralph (`AgentProcessManager`,
   SIGTERM→SIGKILL, stuck detection) et ralph-orchestrator (construction par
   backend, fail-closed) doivent directement informer notre implémentation
   plutôt que d'être réinventés à partir de zéro.
3. **Aucun projet n'impose une garantie de politique `Developer.model !=
   Reviewer.model`** — plusieurs (Ralph Orchestrator, changkun/ralph) rendent
   la chose *configurable* (des rôles peuvent pointer vers des backends
   différents), mais aucun n'empêche structurellement de les configurer sur
   le même modèle/provider. C'est un axe de différenciation produit réel :
   configurabilité ≠ garantie. Pire, MetaGPT viole même le principe de base
   (`Engineer` relit son propre code) : un anti-pattern documenté à ne pas
   reproduire.
4. **Le principe REUSE-first s'applique très concrètement à Git/GitHub** :
   s'appuyer sur le CLI `gh` (déjà installé, authentifié dans notre
   environnement) plutôt qu'écrire un client API GitHub, et s'inspirer de
   l'abstraction multi-provider de pr-agent (`git_providers/`) pour notre
   Workspace — d'autant que même Ralph Orchestrator, pourtant très complet
   par ailleurs, n'a **pas** d'intégration `gh`/API GitHub (vérifié dans le
   code source, pas seulement supposé).
5. **Le modèle de reprise/checkpoint le plus mûr techniquement est celui de
   LangGraph** (checkpoint + `Interrupt`/`Command` identifié par
   `thread_id`), complété par le mécanisme de suspend-state durable de
   ralph-orchestrator (temp-file+rename, `handoff.md` lisible) — la
   combinaison des deux doit guider notre conception de
   INTERRUPTED/RECOVERY_REQUIRED, déjà alignée avec ces références.
6. **Deux workers candidats gratuits/prêts à l'emploi existent déjà**
   (SWE-agent/mini-swe-agent, Aider) et pourront rejoindre notre Worker
   registry après les 3 adaptateurs du MVP 0.1, sans travail de recherche
   supplémentaire.

## Impact sur l'architecture du MVP 0.1

**Aucun changement de périmètre ou de critère d'acceptation mesurable n'est
nécessaire pour le MVP 0.1** : cette étude confirme et enrichit les choix
déjà actés dans `ROADMAP.md`/`MVP_SPEC.yaml` (QuotaManager multi-fenêtres,
états INTERRUPTED/RECOVERY_REQUIRED, abstraction Workspace, ordre de routage
capacité>rôle>quota>coût) plutôt qu'elle ne les remet en question. Les
apports se situent au niveau de l'**implémentation** (patterns à suivre,
détaillés dans la matrice ci-dessus), pas du **périmètre**.

Un seul ajustement de roadmap (hors MVP 0.1) : la Phase 2 (GitHub/PR) doit
explicitement prévoir de **réutiliser le CLI `gh`** plutôt que d'écrire un
client API GitHub — mis à jour dans `ROADMAP.md`.

**Mise à jour (correction Ralph)** : cette conclusion « aucun changement de
périmètre » reste valable, mais les décisions **BUILD** de la matrice pour
les composants où Ralph Orchestrator est le plus proche (Worker
registry/adapters, Task scheduler, Recovery, Reviewer, Quality Gates) sont
désormais explicitement provisoires — voir la **Phase 0.5 — Reuse Spike**
dans `ROADMAP.md`, qui doit les tester avant le début de la Phase 1.

## Suivi

Ce document doit être revu :
- avant chaque nouvelle capability majeure (principe directeur #9 de
  `ROADMAP.md`) ;
- si un projet listé change significativement de statut (licence, maintien,
  version majeure) ;
- si un nouveau candidat sérieux apparaît dans l'écosystème.
