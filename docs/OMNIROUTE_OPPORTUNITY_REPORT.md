# Rapport d’opportunité — ai-dev-orchestrator × OmniRoute × Ralph

Date de l’audit : 13 septembre 2026. Décision proposée : **GO_INCREMENTALLY**. Architecture cible : **C, hybride sous contraintes**. Scénario : **HYBRID, précédé d’un spike MINIMAL réversible**.

## 1. Executive summary

OmniRoute est un candidat crédible pour mutualiser le transport d’inférence, les connexions à plusieurs fournisseurs, la télémétrie économique et la résilience HTTP. Il ne remplace ni notre gouvernance du développement ni Ralph. Sa richesse fonctionnelle est réelle, mais son adoption complète introduirait une surface opérationnelle et de sécurité disproportionnée à notre orchestrateur actuel.

Recommandation : conserver le chemin natif, qualifier une instance séparée et figée d’OmniRoute, commencer par une route à modèle unique, puis autoriser une optimisation dans un ensemble de profils explicitement admis. Aucun remplacement de composant métier n’est justifié. Aucune intégration n’a été réalisée pendant cet audit.

Trois bénéfices majeurs :

- Ajouter des API et modèles locaux sans construire leur proxy, leur traduction de protocoles et leur résilience dans notre Python.
- Exploiter santé, quotas, reset windows, coût et latence pour départager des candidats déjà conformes.
- Enrichir notre audit métier avec les tentatives d’inférence, les tokens et les coûts observés.

Trois risques majeurs :

- Un modèle effectivement exécuté, un effort modifié ou un appel auxiliaire peut échapper à la sélection initiale : risque de downgrade, d’atteinte à l’indépendance reviewer et de perte de traçabilité.
- Le service centralise credentials, prompts et accès réseau ; certains defaults permissifs et chemins web/OAuth nécessitent une configuration restrictive.
- Dépôt très volumineux, release étudiée non taguée, correctifs fréquents, typing partiel et droits encore non clarifiés pour certains assets redistribués.

Points qui changent la décision :

| Constat vérifié | Conséquence |
|---|---|
| Un combo avec models[] explicite n’est pas élargi automatiquement au catalogue connecté | Ensemble fini de modèles techniquement possible ; qualifier tous les chemins avant délégation |
| candidatePool contient des noms de providers | Ce champ seul ne représente pas nos candidats worker/profile/tier |
| auto/offline choisit des poids « offline-friendly » | Aucune garantie offline-only ; imposer une allowlist locale et une restriction réseau |
| Le reasoning peut être plafonné, traduit ou supprimé | Conserver le contrôle dans l’orchestrateur et certifier chaque mapping |
| Le cache appelé « semantic » utilise une signature SHA-256 | Cache exact normalisé, pas preuve d’un cache de réponses par similarité vectorielle |
| OmniRoute possède des quotas Claude/Codex OAuth | Recouvrement réel avec les abonnements, mais pas équivalence avec nos probes natifs |
| Les traces de sélection sont en partie temporaires ou recalculées | Notre audit persistant reste indispensable |
| La licence MIT du cœur coexiste avec des notices d’assets en HOLD | Pas de feu vert général pour redistribuer tout le paquet |

Sources décisives : [expansion des candidats][o-expansion], [moteur auto][o-auto-engine], [variantes auto][o-virtual], [Codex reasoning][o-codex], [cache][o-cache], [notices tierces][o-notices].

## 2. Scope

### 2.1 Versions réellement étudiées et évolution du workspace

| Projet | Référence constatée | Portée de la preuve |
|---|---|---|
| ai-dev-orchestrator au démarrage | main, 9e4d6f7651f17f6a705394c7cf74302020ced347 ; ahead 1 | Slice 17, « Implement adaptive development selection » |
| ai-dev-orchestrator pendant l’audit | 495588aa942b14351b6d97beea2249ef59dc599e ; ahead 2 | Slice 18, « Implement realization reports », créée par une autre activité concurrente |
| OmniRoute | branche release/v3.8.51 ; package 3.8.51 ; commit 152d95108c9c3d557562311ffed63240a511eb31 | Clone complet séparé du projet ; commit du 12 septembre 2026 à 01:29:29 UTC |
| Dernière release GitHub publiée observée | v3.8.50, publiée le 26 août 2026 à 19:30:30 UTC | Différente de la branche de travail étudiée ; ne pas assimiler 3.8.51 du package à un tag publié |
| Ralph local | CLI 2.10.1 ; ralph-spike au commit 3c59e6f | Dépôt de spike, configs et preuves historiques ; ce n’est pas le dépôt source complet de Ralph |

L’état initial contenait déjà des fichiers non suivis : realization_report.py, test_realization_report.py et scripts/smoke_cross_worker_real.py. Une autre activité a ensuite modifié ROADMAP.md, ajouté un rapport de smoke et committé la Slice 18. L’audit n’a ni produit ni réécrit ce commit. La cartographie finale inclut cette évolution ; les conclusions de sélection restent celles du code Slice 17, non modifié par la Slice 18.

Le chiffre de 740 tests vient de l’état connu fourni dans la demande. Il n’est pas présenté comme un résultat obtenu pendant cet audit. La suite complète n’a pas été lancée. Les tests ont été lus, notamment ceux des quotas, de la sélection, de la reprise, du moteur Ralph et des rapports.

### 2.2 Méthode et limites

Inspection de README.md, ROADMAP.md, docs/status.md, docs/ADAPTIVE_EXECUTION.md, docs/ECOSYSTEM.md, docs/SPIKE_RALPH.md, MVP_SPEC.yaml, config/workers.yaml, pyproject.toml, src/orchestrator et tests. MVP_SPEC.md n’a pas été trouvé. Pour OmniRoute : code de routage et ses appelants, exécuteurs, registres, quotas, caches, traces, CLI config, persistence, sécurité, licence, metadata, tests, CI, historique et publication GitHub.

Classification des capacités OmniRoute :

- **PROUVÉE PAR CODE** : implémentation identifiable, et raccordement au chemin d’exécution examiné lorsque pertinent. Ne signifie pas « exécutée avec succès sur nos comptes ».
- **DOC ONLY** : annonce ou garantie de résultat non établie par cet audit.
- **NON TROUVÉE** : pas de contrat ou mécanisme correspondant trouvé dans le périmètre recherché ; ce n’est pas une preuve mathématique d’absence dans tout le projet.

Pas d’installation de dépendances OmniRoute, pas de démarrage du gateway, pas de connexion OAuth, pas de probe LLM, pas de benchmark live, pas de consommation de reset credit par l’audit. Pour Ralph, seules les commandes statiques --version et --help ont été exécutées. Les observations des anciens spikes sont explicitement distinguées des vérifications de cette session.

Audit architectural et de surface de sécurité, pas pentest, certification juridique ni audit exhaustif de chaque ligne des centaines de milliers de lignes OmniRoute. Les gains de performance et de coût restent à mesurer.

[Commit OmniRoute étudié][o-head] ; [dernière release publiée][o-release].

## 3. Current ai-dev-orchestrator architecture

### 3.1 Architecture effective

~~~text
Project / roadmap
  → ProjectStateStore : MVP et WorkItems, dépendances et transitions
  → MVPManager : prochain travail, development/rework, gates, review, reprise
  → ExecutionRecommendationService : estimation via une Execution Ralph
  → WorkerSelector : capability, tier admissible, gouvernance, quota, priorité
  → AdaptiveExecutionSelector : profil admissible du worker choisi
  → ExecutionRequest : modèle et effort concrets
  → RalphExecutionEngine → Ralph → Claude Code ou Codex
  → ExecutionStore + HandoffStore + ValidationStore + ReviewStore
  → ReleaseManager → ActivityReport
  → PlanningCoordinator → RoadmapProposal
  → ApprovalCoordinator → RoadmapApplicationService → MVP suivant

En parallèle : QuotaManager → ProviderAdapter.probe()
Lecture transverse : RealizationReportService → rapport d’un WorkItem
~~~

Python >=3.10, SQLite standard, PyYAML >=6.0 comme seule dépendance runtime déclarée. Les services sont composables, souvent opt-in ; l’existence d’une brique ne signifie pas qu’un assemblage opérationnel complet est automatiquement activé. Le moteur n’accepte actuellement que claude_code et codex dans _build_backend_args.

### 3.2 Rectifications nécessaires par rapport aux descriptions historiques

1. WorkerSelector classe les workers par priorité décroissante puis worker_id lexical. Il ne compare pas les prix réels de leurs profils. L’économie se fait ensuite au sein du worker retenu, via cost_rank ; le choix n’est donc pas un optimum économique global.
2. resolve_profile filtre d’abord le tier, préfère un reasoning exact lorsqu’il existe, puis cost_rank, tier et profile_id. Le reasoning recommandé est actuellement un indice de préférence, pas une obligation dure.
3. QuotaManager est un cache mémoire avec TTL et single-flight par nom de provider. Il ne persiste pas les fenêtres, ne calcule pas de budget et ne déclare pas automatiquement AVAILABLE à l’heure du reset. Des passages anciens de MVP_SPEC/ROADMAP décrivent une cible différente.
4. Worker.provider regroupe aujourd’hui disponibilité et identité fournisseur. « openai » désigne ici le quota Codex de notre adapter ; ce n’est pas une preuve de disponibilité de l’API OpenAI facturée.
5. Le YAML contient Alice/Claude et Victor/Codex, trois profils chacun, jusqu’à COMPLEX. Aucun profil CRITICAL n’est configuré. Une demande CRITICAL doit échouer structurellement, sans fabriquer WAITING.
6. config/workers.yaml déclare code_review, alors que MVPManager.REVIEW_CAPABILITY vaut encore reviewer. Avec le YAML tel quel et la review activée, le registre ne suffit pas à sélectionner un reviewer par cette constante. Il faut traiter cette convention dans une future slice ; OmniRoute ne la résout pas.
7. L’adaptive selection concerne development/rework et leurs reprises. Review, planning et synthesis restent non adaptatifs ; la roadmap finale place leur extension en Slice 19.
8. Le moteur reçoit maintenant model/reasoning_effort sur ExecutionRequest ; les descriptions anciennes parlant de Worker.model ne reflètent plus le contrat courant.
9. L’audit des exécutions persiste les identités demandées, pas un inventaire de chaque appel HTTP ni des tokens/coûts réels. Les durées d’exécution peuvent être dérivées des timestamps.
10. Workspace/LocalGitWorkspace, CLI produit complète et gouvernance GitHub ne sont pas des fonctionnalités attestées par les modules présents. RalphExecutionEngine reçoit un Path et lit Git directement pour les SHA. Le contrôle Git/PR/merge reste une étape future.
11. Les quality gates et reviews sont opt-in. ReleaseManager accepte l’absence de commandes de validation comme un cas passant et se fonde sur les dernières preuves par WorkItem ; il ne constitue pas encore une validation exhaustive de toutes les preuves sur le SHA de release.
12. Le cold resume est établi par le test offline ; le nouveau rapport réel versionné montre deux processus et une passation Alice → Victor réussie. Il ne prouve pas à lui seul tous les scénarios de kill pendant un stream, de panne réseau ou de reprise de review.

Sources locales : [sélection][a-selector], [adaptation][a-adaptive], [quota][a-quota], [moteur][a-engine], [configuration][a-workers], [release gate][a-release-manager], [statut courant][a-status], [preuve de smoke][a-smoke].

### 3.3 Cartographie détaillée des composants

Coût : F = faible, M = moyen, E = élevé, TE = très élevé, estimation relative de migration et de revalidation, pas chiffrage commercial. Risque : LOW/MEDIUM/HIGH. « Remplacement » signifie perte ou migration de la responsabilité, pas renommage.

| Composant / source | Responsabilité et raison d’exister | Données détenues | Dépendances principales | Overlap OmniRoute / Ralph | Coût / risque de remplacement |
|---|---|---|---|---|---|
| ProviderAdapter [source][a-adapter] | Contrat de probe seul ; sépare observation et exécution | Aucun état imposé | ProviderState | Source quota / pas moteur Ralph | M / HIGH : confusion exécution-probe |
| ProviderState [source][a-contracts] | Snapshot générique daté | provider, availability, fenêtres, credits, observed_at | Contrats purs | ProviderQuotaState partiel / métriques non équivalentes | E / HIGH : changement transversal |
| QuotaWindow [source][a-contracts] | Représenter toutes les fenêtres observées | type, source, utilization nullable, reset_at, observed_at | Aucun transport | Fenêtres et dimensions OmniRoute / aucun équivalent fiable dans spike | M / HIGH : perte de fenêtres/unités |
| ResetCredit [source][a-contracts] | Décrire sans autoriser la consommation | titre, statut, nombre, auto_consume=False | Aucun | Affichage et endpoint de consommation OmniRoute / aucun | M / HIGH : contrainte non négociable |
| ClaudeCodeAdapter [source][a-claude] | Observer le CLI réellement utilisé | Paramètres de probe, dernier résultat retourné | subprocess stream-json, contracts | Usage OAuth Claude / backend Claude Ralph | M / HIGH : auth et signal différents |
| CodexAdapter [source][a-codex] | Lire account/rateLimits/read | primary/secondary, ordinaryUsageAllowed, credits | app-server JSON-RPC stdio | Quota WHAM OAuth ; app-server d’exécution distinct / backend Codex | M / HIGH : perdre le signal canonique |
| QuotaManager [source][a-quota] | Fraîcheur stricte, déduplication des probes | Map états et tâches in-flight | Adapters, TTL, horloge | Caches quota OmniRoute / aucun substitut Ralph | M / HIGH : stale réadmis |
| Worker [source][a-selector] | Identité logique de gouvernance | id, display, provider, backend, capabilities, priority, enabled, profiles | ExecutionProfile | Pas un compte gateway / hat proche mais non identique | E / HIGH : author/reviewer |
| WorkerRegistry [source][a-registry] | Charger et valider ce qui est autorisé | Workers/profils configurés | YAML, validation, Worker | Registry provider/model / hats config | M / HIGH : catalogue ≠ autorisation |
| ExecutionProfile [source][a-selector] | Configuration certifiée sélectionnable | profile_id, tier, model, effort, cost_rank | QualityTier | Modèle et options de route / backend args | M / HIGH : downgrade par alias |
| QualityTier [source][a-selector] | Ordre SIMPLE < STANDARD < COMPLEX < CRITICAL | IntEnum 1..4 | Aucun | Score/tier économique non équivalents / gates de sortie | E / HIGH : contrat produit |
| WorkerSelector [source][a-selector] | Éligibilité, gouvernance, disponibilité, priorité | Registry reçu, policy, diagnostics | QuotaManager | Candidate filtering et classement / backend configuré | E / HIGH : indépendance |
| WorkerSelectionPolicy [source][a-selector] | Anti-auto-review et diversité provider | distinct worker requis ; provider préféré/requis | WorkerSelector | Allowlists de connexions, pas identité d’auteur / persona review | E / HIGH |
| ComplexityEstimationRequest [source][a-estimation] | Définir les faits d’une estimation par rôle | Objectif, critères, IDs, SHA, handoff, findings | HandoffRecord, ReviewFinding | Classification du prompt / hat estimator possible | M / HIGH : perte contexte métier |
| ExecutionRecommendation [source][a-estimation] | Distinguer niveau recommandé et choix concret | tier, reasoning hint, raisons, fingerprint, estimator IDs | QualityTier | taskFit/complexity approximatifs / event transport | M / HIGH |
| ExecutionRecommendationService [source][a-estimation] | Estimator configuré, validation fail-closed, cache exact | Références aux services et policy | Selector, Ralph engine, store | Task-aware router / mécanisme hat réutilisé | E / HIGH : risque de doubler ou supprimer l’estimation |
| ExecutionRecommendationStore [source][a-estimation] | Historique insert-only des recommandations | Table SQLite, lookup fingerprint | sqlite3 | Cache de classification partiel / pas audit métier | M / HIGH |
| AdaptiveExecutionSelector [source][a-adaptive] | Composer estimation, choix worker, choix profil | Dépendances et décision produite | RecommendationService, Selector, DecisionStore | Scoring/routing partiel / aucun choix adaptatif prouvé | E / HIGH |
| AdaptiveExecutionDecision [source][a-adaptive] | Snapshot final et rationale | recommendation_id, worker/provider/backend/profile/model/tier/effort, IDs métier | Contrats | Trace de route / config du run | M / HIGH : demandé ≠ réellement exécuté |
| AdaptiveExecutionDecisionStore [source][a-adaptive] | Conserver chaque décision | SQLite insert-only | sqlite3 | Traces gateway partiellement temporaires / events | M / HIGH |
| RalphExecutionEngine [source][a-engine] | Lancer un hat et interpréter les événements métier | Config temporaire, résultat, sortie bornée | Ralph subprocess, ExecutionStore, lectures SHA | Transport d’inférence seulement / runtime Ralph directement réutilisé | TE / HIGH |
| ExecutionRequest [source][a-engine] | Contrat explicite d’exécution | worker, model, effort, role, instructions, topics, workspace, timeout | Worker | Requête HTTP partielle / config hat | M / HIGH |
| ExecutionRecord [source][a-executions] | Identité immuable et état terminal d’une tentative | IDs, provider/backend/model/effort, sessions, dates, statut, exit, SHA | Contrat pur | Call logs / events loop | E / HIGH : échelle différente |
| ExecutionStore [source][a-executions] | Transitions durables des exécutions | SQLite executions, list_running | sqlite3 | Usage DB / runtime history | E / HIGH |
| MVPManager [source][a-mvp] | Enchaîner WorkItems, dev, review, rework, gates, waits/recovery | Références aux stores/services ; pas seconde task queue fine | State, Selector, Ralph, gates, review, waits, recovery | Aucun équivalent MVP / workflow fin Ralph | TE / HIGH |
| ProjectStateStore [source][a-state] | États Project/MVP/WorkItem et dépendances | SQLite, transitions, détection cycles/inconnus | sqlite3 | Config gateway ≠ projet / tasks runtime partiels | TE / HIGH |
| HandoffStore [source][a-handoff] | Passation métier sans mémoire du worker | Objective, travail, décisions, tests, risques, prochaine action, SHA, IDs | sqlite3, HandoffRecord | Résumé conversationnel TTL / handoff Ralph partiel | E / HIGH |
| WaitCoordinator [source][a-wait] | Transformer un quota bloquant daté en attente durable | WaitStore, eligible_at, phase, sources | Diagnostics Selector, horloge | Cooldown et quota timer / timeout runtime | E / HIGH |
| RecoveryCoordinator [source][a-recovery] | Réconcilier tentative orpheline et préparer nouvelle tentative | Faits persistants et handoff de recovery | Execution, Handoff, State, Review stores | Retry réseau non équivalent / resume de loop partiel | TE / HIGH |
| QualityGateRunner [source][a-validation] | Exécuter des validations indépendantes du verdict LLM | argv, required, résultat, stdout/stderr, durée, SHA | subprocess, ValidationStore | Validation de réponse gateway / TDD/gates internes Ralph | E / HIGH |
| Review orchestration [sources][a-review] | Review indépendante, findings, cycles bornés | ReviewRecord, policy max cycles, author/reviewer | MVPManager, Selector, Ralph, ReviewStore | Judge fusion ≠ review / presets Ralph proches | TE / HIGH |
| Planning orchestration [source][a-planning] | Propositions distinctes et synthèse sur snapshot | Sessions, snapshots, propositions, désaccords, hash roadmap | Ralph, Selector, ActivityReport, PlanningStore | Pipeline/fusion partiels / planner natif | TE / HIGH |
| ReleaseManager [source][a-release-manager] | Décider release selon faits consolidés | ReleaseRecord/checks, ActivityReport | State, execution, validation, review/release/report stores | Aucun / finalizer fin seulement | E / HIGH |
| ActivityReport [source][a-activity] | Synthèse factuelle release/MVP pour décisions suivantes | Exécutions, gates, reviews, incidents, compteurs, durée | Stores métier, ReleaseManager | Analytics d’inférence / metrics loop | E / HIGH |
| RoadmapProposal [source][a-planning] | Proposition structurée de prochain MVP | KEEP/ADD/MOVE/DROP, critères, risques, accords, sources | PlanningCoordinator/Store | Aucun / plan technique partiel | E / HIGH |
| Approval window [source][a-approval] | Décision durable après notification et délai par défaut 20 min | deadline, statut, notification/error, décision, flag auto | ApprovalStore, notifier, horloge | Aucun équivalent roadmap / intervention runtime différente | E / HIGH |
| RoadmapApplicationService [source][a-application] | Appliquer seulement une proposition décidée, de façon idempotente | Hash attendu, mapping WorkItems, journal application | Approval, Planning, State stores, MVPManager | Aucun / aucun | TE / HIGH |
| RealizationReportService/Store [source][a-realization] | Snapshot factuel d’un WorkItem, timeline et HTML | Recommandations, décisions, executions, handoffs, waits, validations, reviews | Stores existants en lecture ; store propre insert-only | Analytics partielles / événements source seulement | E / HIGH |

La complexité de nos modules MVPManager, planning et roadmap_application vient surtout des invariants métier et de reprise, pas de l’absence d’un proxy. Les remplacer par OmniRoute déplacerait le problème sans préserver ces invariants.

## 4. OmniRoute architecture

### 4.1 Structure et trajet réel

1. Application Node/TypeScript, interface Next.js 16.3.3, commande omniroute.
2. Routes de compatibilité sous /v1 et routes de gestion sous /api.
3. Entrée de chat, contrôle d’accès et policies dans src/sse/handlers.
4. Handler d’inférence dans open-sse/handlers/chatCore.ts.
5. Combos, résolution de cibles, filtres et scoring dans open-sse/services.
6. Credentials par connexion, sélection de compte et cooldown dans src/sse/services/auth.ts.
7. Exécuteurs spécialisés ou génériques pour le dispatch upstream.
8. Traductions OpenAI Chat, Responses, Claude et Gemini, y compris streaming/tool calls.
9. SQLite et caches mémoire pour configuration, connexions, historique, quotas et contexte.
10. Tâches de fond, monitoring et options supplémentaires : Redis, services externes, MCP/A2A, desktop.

Le workspace open-sse importe des modules src/lib et les alias de l’application. Il n’est pas un petit SDK autonome que Python pourrait importer. La frontière HTTP d’un service séparé est beaucoup plus propre que la copie de modules.

### 4.2 Inventaire des capacités vérifiées

| Fonctionnalité demandée | Statut | Preuve et portée réelle |
|---|---|---|
| Multi-provider routing | PROUVÉE PAR CODE | Combos et exécuteurs ; plusieurs connexions par provider [combo][o-combo], [auth][o-auth] |
| Multi-model routing | PROUVÉE PAR CODE | models[], cibles provider/model et résolution des combos [structure][o-structure] |
| Dynamic routing / auto routing | PROUVÉE PAR CODE | Scoring et résolution au runtime [moteur auto][o-auto-engine], [scoring][o-scoring] |
| Task-aware routing | PROUVÉE PAR CODE | Détection de motifs de tâche, table d’intentions et branchement chat ; heuristique, pas analyse de notre WorkItem [task router][o-task] |
| Coding-aware routing | PROUVÉE PAR CODE | taskFitness et mode coding ; pas certification de notre tier [fitness][o-fitness] |
| auto/coding | PROUVÉE PAR CODE | Catalogue + variante quality-first [catalogue][o-catalog], [factory][o-virtual] |
| auto/fast | PROUVÉE PAR CODE | Mode ship-fast, scoring de vitesse |
| auto/cheap | PROUVÉE PAR CODE | Mode cost-saver ; « cheap » n’est pas une limite financière dure |
| auto/offline | PROUVÉE PAR CODE | Alias reconnu et poids offline-friendly ; contrainte réseau locale NON TROUVÉE pour cet alias |
| auto/smart | PROUVÉE PAR CODE | Mode quality-first, exploration augmentée ; branche pipeline dans chat selon configuration [chat][o-chat] |
| auto/subscription, thrifty, catégories/suffixes | PROUVÉE PAR CODE | Catalogue, subscriptionLadder et composition ; vérifier critères d’éligibilité à chaque upgrade |
| Quota telemetry / reset windows | PROUVÉE PAR CODE | Plusieurs familles de telemetry, fetchers et normalisation [quota][o-quota], [preflight][o-preflight] |
| Rate-limit handling / Retry-After | PROUVÉE PAR CODE | Classification, retry hints, cooldown et exécuteur [fallback][o-fallback], [base][o-base] |
| Health checks | PROUVÉE PAR CODE | /healthz, /livez, monitoring et santé credentials ; santé process ≠ santé upstream [health][o-health] |
| Retry | PROUVÉE PAR CODE | Retries au niveau transport et boucle combo ; budgets à coordonner |
| Circuit breaker | PROUVÉE PAR CODE | CLOSED/DEGRADED/OPEN/HALF_OPEN et escalade [breaker][o-breaker] |
| Provider cooldown | PROUVÉE PAR CODE | Tracker global distinct du breaker, activation configurable [cooldown][o-cooldown] |
| Connection cooldown | PROUVÉE PAR CODE | Un compte peut être écarté sans bannir tout son provider [auth][o-auth] |
| Model cooldown / lockout | PROUVÉE PAR CODE | Échecs et lockouts par modèle/connexion ; ne pas confondre avec panne provider [fallback][o-fallback] |
| Fallback | PROUVÉE PAR CODE | Chaîne de cibles, retours d’erreur, compat fallback ; pas invariant qualité métier [attempts][o-attempts], [compat][o-compat] |
| Candidate filtering | PROUVÉE PAR CODE | Visibilité, protocole, capabilities, quotas, santé, comptes autorisés ; certains filtres de contexte sont consultatifs |
| Session affinity | PROUVÉE PAR CODE | Fingerprint mémoire + stickiness et persistence compte/session [sessions][o-sessions], [affinity DB][o-affinity] |
| Context relay | PROUVÉE PAR CODE | Résumé conversationnel et injection au changement ; peut lancer un appel LLM additionnel [handoff][o-context] |
| Context caching / prompt cache affinity | PROUVÉE PAR CODE | Choix de connexion et cache fournisseur [cache affinity][o-cache-affinity] |
| Semantic response cache | PROUVÉE PAR CODE | Cache exact normalisé SHA-256, LRU+SQLite, isolation par API-key ID [cache][o-cache] |
| Cache de réponses par proximité sémantique vectorielle | NON TROUVÉE | Le chemin chat étudié n’effectue pas de comparaison embeddings/cosine ; mémoire vectorielle éventuelle = autre fonction |
| Token optimization | PROUVÉE PAR CODE | Moteurs de compression, règles, contexte ; bénéfice chiffré non validé [compression][o-compression] |
| Provider/model/cost/latency scoring | PROUVÉE PAR CODE | Facteurs distincts, poids, métriques historiques [scoring][o-scoring], [sorters][o-sorters] |
| Routing decision audit | PROUVÉE PAR CODE | Call logs, trace combo et route-explain ; persistence et exactitude variables [trace][o-trace], [explain][o-explain] |
| CLI integration Claude Code | PROUVÉE PAR CODE | Modification settings/env et endpoint Claude compatible ; pas preuve de notre chaîne Ralph complète [Claude config][o-cli-claude] |
| CLI integration Codex | PROUVÉE PAR CODE | model_provider, base_url, wire_api et credentials ; peut modifier config globale [Codex config][o-cli-codex] |
| OpenAI-compatible gateway | PROUVÉE PAR CODE | Chat/Responses, traduction, streaming ; compatibilité à certifier par usage |
| Modèles locaux / Ollama | PROUVÉE PAR CODE | Registre et dispatch local compatible ; aucune capacité matérielle du mini-PC déduite [providers][o-providers] |
| Mistral, Gemini, Anthropic, OpenAI | PROUVÉE PAR CODE | Registres, formats et exécuteurs génériques/spécifiques ; une entrée catalogue ne garantit pas accès à un modèle sur nos comptes |
| Autres providers | PROUVÉE PAR CODE | Codex OAuth, Claude OAuth, OpenRouter, Groq, DeepSeek, Copilot, Azure, etc. Plusieurs transports web/CLI aussi ; usage sélectif recommandé |
| Apprentissage opérationnel | PROUVÉE PAR CODE | EWMA, anomalies de stream, confiance et signaux de qualité [quality][o-quality] |
| Routage à partir d’évaluations | PROUVÉE PAR CODE | evalRouting reclassifie selon runs persistés, pass rate et latence [evals][o-evals] |
| Apprentissage « SIMPLE Python bugfix → profil X → succès de release » | NON TROUVÉE | Les dimensions Project/MVP/WorkItem, tier interne et verdict de review ne sont pas intégrées |
| Garantie universelle zéro coût, zéro perte de contexte ou no-downgrade | NON TROUVÉE | Les contraintes locales doivent être appliquées indépendamment |
| Économies 15–95 %, moyenne annoncée ~89 %, tokens gratuits disponibles pour nous | DOC ONLY | Annonces du README ; pas une mesure sur notre workload ni une allocation garantie |

### 4.3 Stratégies réellement présentes

Le tableau public du code contient **20 stratégies**, plus quota-share interne. Le README/guide annonce encore 19 : retenir le registre typé et le dispatch, pas le chiffre marketing. [Registre des stratégies][o-strategies].

| Stratégie | Statut | Sémantique / intérêt pour nous |
|---|---|---|
| priority | PROUVÉE PAR CODE | Ordre explicite, bon début déterministe |
| weighted | PROUVÉE PAR CODE | Tirage pondéré ; seed/décision à journaliser |
| fill-first | PROUVÉE PAR CODE | Préserve ordre, utilise capacité prioritaire |
| round-robin | PROUVÉE PAR CODE | Répartition cyclique ; peu utile mono-WorkItem |
| p2c | PROUVÉE PAR CODE | Power of two choices selon charge |
| random | PROUVÉE PAR CODE | Exploration ; désactiver au départ |
| strict-random | PROUVÉE PAR CODE | Deck et remainder mélangé ; pas un garde qualité |
| least-used | PROUVÉE PAR CODE | Nombre de requêtes ; pas quota natif exact |
| cost-optimized | PROUVÉE PAR CODE | Prix de cibles ; ne remplace pas facturation réelle |
| headroom | PROUVÉE PAR CODE | Saturation/marge disponible ; intérêt pour quotas inclus |
| reset-aware | PROUVÉE PAR CODE | Ordonnancement selon quota/fenêtres |
| reset-window | PROUVÉE PAR CODE | Affinité avec fenêtres de reset |
| quota-weighted | PROUVÉE PAR CODE | Pondération par état quota, contrôle de fraîcheur |
| context-relay | PROUVÉE PAR CODE | Prépare un résumé pour transfert de conversation |
| context-optimized | PROUVÉE PAR CODE | Taille de contexte ; pas de garantie d’exécution du code |
| cache-optimized | PROUVÉE PAR CODE | Affinité prompt/cache et connexions |
| lkgp | PROUVÉE PAR CODE | Last Known Good Provider ; succès antérieur de transport |
| auto | PROUVÉE PAR CODE | Score multifactoriel et sélection runtime |
| fusion | PROUVÉE PAR CODE | Panel de modèles puis judge ; coût et gouvernance supplémentaires |
| pipeline | PROUVÉE PAR CODE | Étapes d’inférence composées ; overlap avec workflow Ralph |
| quota-share, interne | PROUVÉE PAR CODE | Allocation de quotas partagés ; non exposée comme stratégie publique générale |

Implémentations : [ordonnancement][o-ordering], [tri][o-sorters], [quota strategies][o-quota-strategies], [dispatch][o-dispatch], [résolution][o-targets].

## 5. Ralph architecture

Ralph 2.10.1 est déjà la dépendance d’exécution. Le spike local n’est pas sa source Rust : les conclusions natives s’appuient sur ses configurations, l’aide CLI installée, les observations antérieures versionnées et le wrapper Python effectivement utilisé.

Ralph fournit hats, backend par hat, paramètres de modèle, transport d’événements, boucle et limites d’exécution. Les workflows builtin code-assist/review, les mémoires/tâches, waves et loops/worktrees sont documentés et partiellement éprouvés dans l’étude antérieure. Notre intégration actuelle génère **un hat par Execution** ; elle n’active pas automatiquement tout le potentiel multi-hats ou parallèle de Ralph.

Ralph ne garantit pas notre classification de qualité, l’optimisation de quotas d’abonnement ni les politiques author/reviewer. Le spike a observé des boucles continuant après un backend indisponible, ainsi que des métriques Codex à zéro malgré un travail réel. Le wrapper conserve donc les événements métier comme verdict, et l’exit code comme diagnostic.

OmniRoute possède aussi des capacités « agent » (MCP/A2A, certaines intégrations CLI, transport codex-app-server). Leur présence ne démontre pas une équivalence avec le workflow Ralph déjà validé. Le transport codex-app-server inspecté sait démarrer des threads/turns et relayer des outils ; ce n’est pas notre probe account/rateLimits/read et cela introduirait une seconde boucle agentique si mal utilisé.

**Frontière confirmée avec précision** : orchestrateur = décisions métier et autorisations ; Ralph/CLI = travail agentique ; OmniRoute optionnel = fourniture d’inférence sous ces autorisations. Le gateway est derrière le CLI dans le plan de données, même si la politique de route se prépare avant Ralph.

Sources : [spike Ralph][a-spike], [architecture adaptative][a-adaptive-doc], [wrapper][a-engine], [transport OmniRoute app-server][o-appserver].

## 6. Capability matrix

O = orchestrateur ; R = Ralph ; G = gateway OmniRoute. Actions : KEEP, MERGE, REPLACE, DELEGATE, INTEGRATE, OPTIONAL, REMOVE, DO_NOT_USE. MERGE désigne une consolidation future de signaux/contrats, jamais un merge de code pendant l’audit.

| Capability | ai-dev-orchestrator actuel | Ralph | OmniRoute | Overlap | Meilleur propriétaire | Action recommandée |
|---|---|---|---|---|---|---|
| Roadmap | Proposal/application persistantes | Plans techniques | Pas notre roadmap | Faible | O | KEEP |
| MVP | Entité et lifecycle | Pas notre agrégat | Pas notre agrégat | Aucun utile | O | KEEP |
| WorkItem | Unité haut niveau | Tasks runtime | Requêtes/agents | Niveaux différents | O + R fin | KEEP |
| Dependencies | Graphe MVP, cycles, blocages | Workflow/tasks | Combos DAG | Structure seulement | O | KEEP |
| Adaptive complexity estimation | Estimator + fingerprint | Exécute estimator | Classifie prompt | Partiel | O | KEEP |
| Quality tiers | 1..4, filtre dur | Gates de sortie | Scores/tier économique | Faux équivalent | O | KEEP |
| Worker registry | Identités/capabilities | Hats | Registry provider | Partiel | O | KEEP |
| Execution profiles | Modèle/effort/tier/cost_rank | Backend config | Model/route options | Partiel | O | INTEGRATE |
| Worker selection | Governance/quota/priority | Assignation déclarative | Pas identité worker | Faible | O | KEEP |
| Model selection | Profil admissible | Exécute le choix | Scoring/fallback | Fort | O autorise, G optimise | DELEGATE sous contraintes |
| Provider selection | Worker.provider | Backend | Connexions/routage | Fort | O gouverne, G dispatch | INTEGRATE |
| Reasoning effort | Recommandé et transporté | Arguments backend | Mapping/clamping | Fort | O policy + G traduction | KEEP + INTEGRATE |
| Quota probes | CLI Claude, app-server Codex | Metrics insuffisantes | OAuth/headers/fetchers | Partiel fort | Source du transport concerné | KEEP + OPTIONAL |
| Quota normalization | ProviderState/windows | Faible | Dimensions et fenêtres | Fort | O contrat, G source | MERGE des observations |
| Reset windows | Plusieurs, datées | Non fiable | Plusieurs, score/reset | Fort | O attente + G classement | INTEGRATE |
| Quota waiting | WaitStore, pull-based | Timeouts/loop | Preflight/cooldown | Durées différentes | O | KEEP |
| Provider health | Available/reason | Échecs backend | Santé détaillée | Partiel | G transport + O diagnostic | INTEGRATE |
| Circuit breaker | Non construit | Runtime limité | Détaillé | Nouvelle capacité | G | DELEGATE |
| Cooldown | Attente quota seulement | Non équivalent | Provider/compte/modèle | Nouvelle capacité | G | DELEGATE |
| Fallback | Nouvelle sélection/tentative | Pas policy fiable | Chaîne HTTP | Partiel | G borné + O reprise | INTEGRATE |
| Cost ranking | priority/cost_rank | Metrics limitées | Pricing/scoring | Fort | O policy + G chiffres | INTEGRATE |
| Cost optimization | Pas budget réel | Pas canonique | Budgets/cache/routing | Nouveau | O budget + G exécution | OPTIONAL |
| Model scoring | Tier configuré | Non | Facteurs + evals | Partiel | G + qualification O | INTEGRATE |
| Task-aware routing | WorkItem et rôle | Hat spécialisé | Heuristique de prompt | Partiel | O fournit task type | KEEP + OPTIONAL |
| Session affinity | Pas de verrou conversationnel | Session/backend | Stickiness/lease | Complément | R/CLI + G | OPTIONAL |
| Context continuity | Instructions/handoff | Session/mémoires | Relay/cache | Partiel | O durable + R/G session | KEEP + OPTIONAL |
| Handoff | Faits métier persistants | Handoff runtime | Résumé avec TTL | Fort lexical seulement | O | KEEP |
| Cold restart | Stores + nouvelle Execution | Resume loop | Persistence partielle | Partiel | O | KEEP |
| Recovery | Réconciliation explicite | Runtime resume | HTTP retry | Complément | O | KEEP |
| Execution audit | Verdict métier + identité | Events | Call logs | Fort, granularité différente | O agrège | KEEP + INTEGRATE |
| Review independence | worker distinct, provider policy | Personas/preset | Judge sans notre politique | Non équivalent | O | KEEP |
| Quality gates | Commandes et preuves | TDD/gates internes | Qualité de réponse | Partiel | O barrière + R feedback | KEEP |
| Release gates | MVP/validation/review/running | Finalizer | Pas notre release | Faible | O | KEEP |
| Activity report | Release/MVP | Metrics | Analytics HTTP | Partiel | O | KEEP |
| Realization report | Slice 18, timeline WorkItem | Events | Logs/route explain | Partiel | O | KEEP |
| Roadmap planning | Planners distincts + synthesis | Planner | Fusion/pipeline | Partiel | O | KEEP |
| Approval window | Persistante, 20 min par défaut | Intervention runtime | Pas équivalent | Aucun | O | KEEP |
| Git governance | SHA lus, gouvernance future | Git/auto-commits | Outils MCP annexes | Pas substitut | O + R sous contrôle | KEEP |
| Token optimization | Cache estimator exact | Contexte CLI | Compression/optimisations | Complément | O choisit + G option | OPTIONAL |
| Semantic caching | Fingerprint métier | Session | Cache exact de réponses | Partiel | O exact ; G prudent | DO_NOT_USE pour outils au départ |
| Model/provider extensibility | YAML ; 2 backends effectifs | Backends supplémentaires possibles | Catalogue et transport larges | Fort | O profiles + R backend + G catalogue | INTEGRATE |

## 7. Functional overlap

Les overlaps les plus forts sont les filtres de candidats, les quotas, le classement de modèles et l’audit technique. Ce sont des occasions de réutilisation ; les remplacer sans conserver la sémantique métier serait une régression.

Doublons inutiles à éviter :

- Construire en Python un proxy complet OpenAI/Claude/Gemini, un registre mondial de prix ou une collection de plusieurs centaines d’exécuteurs.
- Lancer simultanément notre estimator et un routage auto global qui réinterprète la complexité et choisit un autre niveau.
- Faire maintenir deux pollers du même compte et de la même fenêtre sans identité et provenance communes.
- Empiler retries CLI, retries HTTP, retries combo et reprise WorkItem sans limite commune.
- Activer pipeline/fusion OmniRoute pour reproduire nos planners distincts ou la review Ralph.
- Utiliser plusieurs caches de réponses agentiques sans fingerprint du workspace et des outils.

Recouvrements utiles à conserver : gates internes Ralph pour retour rapide au développeur, gates externes O pour preuve indépendante ; métriques G pour l’inférence, ExecutionRecord O pour le verdict métier ; affinité G pour une session, HandoffRecord O pour survivre à sa disparition.

Il n’existe **aucun composant actuel à REMOVE** au seul motif de l’arrivée d’OmniRoute. Les candidats à suppression sont surtout des développements futurs rendus inutiles par un gateway.

## 8. Architecture options A/B/C

A = sélection native actuelle. B = gateway libre de choisir provider/model à partir d’une intention/tier. C = ensemble autorisé construit par O, optimisation G strictement à l’intérieur.

| Critère | A actuelle | B OmniRoute décide | C hybride |
|---|---|---|---|
| No-downgrade | Fort sur tier configuré, dev/rework adaptatifs | Non garanti : score ne vaut pas seuil dur | Fort si tous appels/paramètres restent certifiés |
| Audit | Identité choisie persistée ; pas chaque requête | Risque alias demandé différent du modèle effectif | IDs métier + policy hash + tentatives réelles |
| Quota | Bon sur les deux CLI ; peu de sources | Large couverture, sémantiques hétérogènes | Sources natives et gateway selon compte/transport |
| Coût | cost_rank relatif, pas global | Classement économique riche mais policy potentiellement souple | Budget métier prioritaire, optimisation dans le set |
| Simplicité | Meilleure aujourd’hui | Apparente ; transfert de contraintes coûteux | Moyenne, exige un contrat explicite |
| Déterminisme | Priorité/tie-break stables | Exploration, état/affinité et poids mouvants | Mode déterministe initial, policy versionnée |
| Résilience | Reprise métier ; peu de résilience HTTP | Bonne HTTP, reprise métier manquante | Responsabilités complémentaires |
| Multi-provider | Deux backends effectifs | Nombreux transports | Extensibilité large sous qualification |
| Extensibilité | Ajout backend peut demander du code | Large catalogue | Catalogue découvert mais profils approuvés |
| Recovery | Durable déjà construit | Ne suffit pas pour reconstruire WorkItem | O conserve tentative et état, G peut être perdu |
| Session continuity | Handoff durable, sessions CLI | Affinité/relay avec limites | Pin session et nouvelle tentative si rupture |
| Reasoning effort | Codex explicite ; Claude non transmis aujourd’hui | Mappings pouvant modifier la demande | O décide ; mapping attesté ou refus |
| Provider independence | Worker/provider policy connue | Risque deux workers vers même upstream | Vérifie provider/compte effectifs et historique |
| Dépendance fournisseur logiciel | Faible | Forte | Adapter HTTP remplaçable, native toujours disponible |

**Choix recommandé : C.** A reste le mode disponible tant que les critères du spike ne sont pas satisfaits. B est rejetée pour la production de code gouvernée. Le choix C n’autorise pas un fallback « n’importe quel modèle assez bien scoré ».

## 9. Recommended target architecture

### 9.1 Position exacte et abstraction

OmniRoute est **D. Router/Gateway**, sous le Worker et derrière le backend CLI dans le trajet des prompts. Il peut fournir **E. un ProviderAdapter d’observation distinct**, mais n’est ni le Worker logique ni le provider réel. « Backend » reste le CLI/exécuteur piloté par Ralph. Un backend spécialement compatible gateway pourra être qualifié, sans renommer OmniRoute en « worker ».

Nom recommandé pour la frontière interne : **InferenceRouter**. Il décrit le choix d’une route d’inférence et évite la confusion avec WorkerSelector, le workflow d’exécution et le fournisseur réel.

~~~text
O : estimation → contraintes → candidats admissibles → politique de route
                                               ↓
                        native                gateway optionnel
                        profil concret        route certifiée/pinnée
                                               ↓
                  ExecutionRequest → Ralph → CLI/backend
                                               ↓
                         provider natif     OmniRoute → upstream
                                               ↓
                 O : verdict métier + audit réel + gates + recovery
~~~

Le YAML futur pourrait porter route_type=native|omniroute et une référence de route sur ExecutionProfile. **Worker.provider ne doit jamais devenir simplement "omniroute"** : cela détruirait la distinction des fournisseurs pour la review. À court terme, garder un provider effectif fixe par worker gateway ; l’élargissement inter-provider exige un contrat d’identité séparé.

### 9.2 Interfaces proposées, non implémentées

| Contrat futur | Champs / comportement |
|---|---|
| InferenceRouteRequest | request_id, execution_id prévu, role, minimum_quality_tier, recommendation_id, candidates immuables, policy_version/hash, deadline, quota constraints, cost/privacy policy, exclusions auteur |
| InferenceCandidate | candidate_id, worker_id, profile_id, tier certifié, upstream_provider, model canonique/version, backend, transport, quota_subject_id/connection_ref, reasoning contract, capabilities, context limit, local/residency tags |
| InferenceRouteDecision | route_id, candidate_set_hash, candidat sélectionné lorsque fixé, candidats de fallback autorisés, expiration, route metadata, raisons, snapshot du mapping reasoning |
| InferenceAttemptRecord | execution_id, route_id, request/attempt_id, provider/model/connexion réellement utilisés, requested/effective reasoning, tokens, coût et source, durée, résultat réseau, fallback/cache |
| GatewayProviderStateAdapter | probe() de télémétrie, sans génération ni consommation ; convertit observations dans notre contrat |
| NativeInferenceRouter | Comportement natif actuel, aucun appel à un gateway absent |
| OmniRouteInferenceRouter | Traduit le contrat interne vers une configuration/route gateway qualifiée ; aucun import de son schéma DB |
| RoutePolicyViolation | Modèle/connexion/effort hors set, configuration dérivée non attestée : fail-closed, trace, pas succès métier |

route(request) → decision est **notre interface conceptuelle**, pas une API OmniRoute existante découverte. Les API de combos et de routage exposent création/gestion, exécution et explication ; aucun contrat public stable « réserve ce modèle précis pour toute la future Execution Ralph » n’a été établi.

Conséquence : un simple appel de scoring avant Ralph ne suffit pas. Sans mécanisme de pin attesté sur toute la session, une décision reste un plan autorisé, pas une preuve de modèle exécuté. Démarrer par un singleton résout ce problème pour le spike. Pour une future sélection multi-modèles, il faudra soit une résolution/pin avec garantie vérifiée, soit une validation de chaque dispatch à l’intérieur du set et un audit multi-tentatives. Le contrôle après réponse détecte une violation mais n’empêche pas une fuite déjà produite : le filtre avant dispatch est indispensable.

### 9.3 Candidate-set et invariant quality-tier

**Oui, OmniRoute accepte une liste explicite de modèles via les combos. Non, on ne peut pas lui envoyer directement notre QualityTier et présumer une garantie équivalente.**

Le chemin possible : créer un combo nommé par un hash de politique, avec models[] constitué uniquement de cibles explicites ; l’appeler comme modèle sur /v1/chat/completions ou /v1/responses suivant le backend. Le service gère POST /api/combos et les paramètres de la route. L’expansion du pool s’arrête lorsque models[] est renseigné ; candidatePool du moteur auto filtre les providers. [API combos][o-combo-api], [expansion][o-expansion], [moteur][o-auto-engine].

Règles impératives :

1. Filtrer dans O enabled, capability, tier, backend/protocole/outils, reasoning requis, contraintes de contexte/privacy/budget et indépendance.
2. Chaque candidat doit satisfaire selected_profile.quality_tier >= minimum_quality_tier. Le tier s’attache à un tuple modèle/version + effort + outils/backend + politique de contexte, pas au seul nom commercial.
3. Interdire wildcards provider, alias auto/*, références imbriquées mutables et ajout automatique de nouveaux modèles à ces routes.
4. Restreindre aussi les connection IDs et les modèles par les contrôles de clé disponibles ; qualifier leur application avant chaque dispatch et chaque fallback. Le scope de clé seul ne remplace pas le combo explicite.
5. Désactiver les remplacements task-aware/reasoning routing globaux, exploration, shadow routing, fusion/pipeline, résumés automatiques, transformations non qualifiées et caches qui changent la sémantique.
6. Versionner le combo : nouveau hash pour chaque changement ; empêcher une édition in-place pendant une Execution.
7. Réévaluer l’éligibilité à chaque nouvelle tentative métier ; quota inconnu/stale ne devient pas automatiquement utilisable.
8. Un ensemble vide structurellement est une erreur de configuration/capacité ; un ensemble dont tous les membres sont temporairement en quota suit WaitCoordinator.
9. Tant que la fermeture de tous les chemins n’est pas prouvée, **pas de fallback libre**. À défaut de contrat sécurisé multi-modèles, conserver singleton/native.

Attention à comboCompatFallback : le gateway peut reconsidérer des cibles rejetées comme incompatibles. Le contexte connu trop petit peut également rester une option de secours. Notre ensemble doit donc être préfiltré sur les contraintes dures, pas construit en espérant que ces filtres gateway soient des interdictions absolues.

### 9.4 Indépendance et granularité de décision

Une Execution Ralph contient plusieurs appels d’inférence. Le choix à l’échelle d’une requête HTTP peut changer le modèle au milieu d’un travail. À court terme : provider/modèle/effort fixes pour toute l’Execution ; fallback de compte uniquement si même tuple certifié et continuité compatible. En cas de changement nécessaire, interrompre proprement et laisser O créer une nouvelle Execution avec handoff.

À terme, autoriser plusieurs modèles dans une Execution seulement avec un audit explicite de chacun. Une review doit exclure les upstreams de toutes les tentatives d’auteur concernées lorsque la diversité provider est requise. Deux workers appelant la même gateway ne sont pas deux providers indépendants. La clé de comparaison doit devenir l’identité upstream effective, pas le fournisseur de transport.

## 10. Components to KEEP

À conserver dans leur responsabilité : Worker/Registry, QualityTier, policy de sélection, recommandations et leurs stores, MVPManager, ProjectStateStore, HandoffStore, WaitCoordinator, RecoveryCoordinator, RalphExecutionEngine, quality/review/release gates, planning/approval/application, ActivityReport et RealizationReport.

Conserver les adapters Claude/Codex natifs pour leurs transports et leurs comptes. Conserver ResetCredit.auto_consume=False ; ni le score ni l’expiration d’une fenêtre d’approbation roadmap n’autorisent sa consommation.

KEEP ne signifie pas que tout est terminé : certains modules demanderont des champs additionnels ou une correction ciblée indépendante du gateway.

## 11. Components to EXTEND

| Composant | Verdict | Ce qui reste / ce qui serait ajouté | Ce qui partirait / ce que G prendrait | Migration | Tests nécessaires / risque |
|---|---|---|---|---|---|
| WorkerRegistry | EXTEND | Identités et validation ; route_type et refs certifiées | Rien ; G fournit catalogue découvert séparé | Champs optionnels, config native inchangée | Chargement ancien, secrets rejetés, catalogue non auto-autorisé ; HIGH |
| ExecutionProfile | EXTEND | Tier/model/effort/cost_rank ; route, upstream, capabilities certifiées | Aucun tier chez G | Valeurs natives par défaut ; snapshot versionné | Alias, effort, tools/context, no-downgrade ; HIGH |
| WorkerSelector | EXTEND | Gouvernance et éligibilité ; exposition contrôlée de candidats admissibles | Classement économique fin optionnel chez G | Refactor interne sans dupliquer ses filtres | Même sélection native ; provider effectif reviewer ; HIGH |
| AdaptiveExecutionSelector | EXTEND | Preflight et décisions ; composer route certifiée | Optionnellement une partie du départage de profils | InferenceRouter injecté, fallback natif explicite | Reprise, ensemble vide, violations, décisions persistées ; HIGH |
| QuotaManager | EXTEND | TTL strict et single-flight ; identité de quota et multi-sources | Aucun cache métier abandonné ; G observe ses connexions | Remplacer clé provider ambiguë par sujet explicite, compat native | Conflits, stale, sources, fenêtres, inconnus ; HIGH |
| ProviderAdapters | EXTEND | Claude/Codex conservés ; GatewayProviderStateAdapter | Pas de suppression immédiate de probe natif | Mapping unités/source/timestamp/auth scope | Fixtures OAuth/header, pas de consume, pas de LLM probe caché ; HIGH |
| WaitCoordinator | KEEP AS IS initialement | Quota waiting durable ; futurs motifs de panne datée seulement si besoin | G prend retry/cooldown courts | Aucun changement en MINIMAL | Délai non inventé, reset ne prouve pas disponibilité ; MEDIUM |
| RalphExecutionEngine | EXTEND | Un hat/Execution, verdicts ; configuration/env isolés du backend | G prend le transport d’inférence derrière CLI | Contexte subprocess optionnel ; aucun nouveau moteur agentique | Env non global, arguments, timeout/process group, streams/outils ; HIGH |
| ExecutionRecord / ExecutionStore | EXTEND | Snapshot et transitions ; lien route/decision ; table de tentatives séparée | Rien ; G fournit faits HTTP | Additif, lecture des anciens records, ne pas réécrire leur provider | Requested/effective, multi-attempt, restart, pas double comptage ; HIGH |

Le coût relatif de ces extensions est M à E ; leur revalidation est le coût dominant. Aucun FULLY REPLACE ni REMOVE n’est recommandé.

## 12. Components potentially REPLACED

**PARTIALLY REPLACE**, uniquement à terme : le départage économique/santé de candidats admissibles peut être délégué à OmniRoute. Aujourd’hui, il est surtout constitué de priorités et de cost_rank ; le bénéfice est donc une capacité nouvelle plus qu’une suppression massive de code.

Une source HTTP OAuth pourrait éventuellement réduire les probes Claude qui génèrent un petit prompt, si l’équivalence de compte, de signal, de fraîcheur et les conditions d’accès sont validées. Elle ne justifie pas de supprimer ClaudeCodeAdapter avant mesure. CodexAdapter reste particulièrement utile pour ordinaryUsageAllowed et les credits natifs.

**FULLY REPLACE : aucun composant identifié.** Éviter de construire nos futurs adaptateurs HTTP d’inférence si G satisfait le besoin. Importer/coller les modules OmniRoute dans Python n’est pas une stratégie de remplacement raisonnable.

## 13. Components NOT to replace

Ne pas remplacer Ralph par un pipeline OmniRoute ; Worker par une connexion OAuth ; QualityTier par un score ; HandoffRecord par un résumé de conversation ; ExecutionRecord par un call log ; RecoveryCoordinator par retry HTTP ; review indépendante par un judge fusion ; quality/release gates par un HTTP 200 ; approbation roadmap par un budget gateway.

Ce sont des différences de contrats observables, pas des préférences de vocabulaire.

## 14. New features enabled by OmniRoute

| Fonction envisagée | Valeur | Propriétaire et condition |
|---|---|---|
| Pool dynamique de providers/modèles | MEDIUM TERM | G découvre ; O propose/qualifie, activation jamais automatique |
| Health-aware execution | IMMEDIATE VALUE après spike | G mesure, O évite de lancer une route connue malade |
| Cross-provider fallback sécurisé | MEDIUM TERM | O construit le set, nouvelle Execution tant que pin/audit multi-modèles non établi |
| Local-model fallback | MEDIUM TERM | Backend/outils testés, profil local certifié au tier requis |
| Budget projet/MVP | MEDIUM TERM | O agrège toutes les exécutions, G limite ses propres requêtes |
| Budgets tokens quotidiens/mensuels | MEDIUM TERM | Ledger couvrant aussi estimator/rework/review et sources natives |
| Max-cost-per-WorkItem / cost ceiling | MEDIUM TERM | Réservations et comptabilité cumulée O ; pas simple header par appel |
| Performance/cost telemetry | IMMEDIATE VALUE | Tentatives G corrélées à Execution/WorkItem, coûts inconnus explicités |
| Provider diversity policy | IMMEDIATE VALUE conceptuellement | Extension de l’indépendance existante aux upstreams effectifs |
| Candidate model benchmarking | MEDIUM TERM | Corpus offline représentatif, sandbox, budget, labels indépendants |
| Automatic benchmarking | LOW VALUE au début | Risque de consommation régulière avant assez de candidats/utilisateurs |
| Routing historique de succès métier | MEDIUM TERM | O produit labels, G optimise les routes sous contraintes |
| Route par type de WorkItem | MEDIUM TERM | Type structuré O, pas uniquement mots-clés du prompt |
| Automatic cooling | IMMEDIATE VALUE | Réutiliser cooldown G pour les pannes techniques |
| Quarantaine modèle après erreurs répétées | MEDIUM TERM | G pour anomalies réseau ; O pour régressions de gates/reviews |
| Mode local/offline d’urgence | MEDIUM TERM conditionnel | Matériel suffisant + absence réelle d’egress + pas de downgrade |
| Profils sensibles à la latence | MEDIUM TERM | Parmi profils déjà suffisants ; délai global workflow conservé |
| Privacy/data residency | MEDIUM TERM | Allowlists endpoints/régions, contrats et egress ; pas « privacy score » |
| Préférences local/provider/subscription | IMMEDIATE VALUE | Ordre de politique explicite avant coût monétaire |
| Compression systématique de toutes les tâches | AVOID | Risque de perdre contraintes, code et schémas d’outils |
| Fusion/chaos comme reviewer automatique | AVOID | Coût et dépendances d’auteur non maîtrisés ; doublon workflow |
| Multiplication de fournisseurs web gratuits | AVOID initialement | Auth fragile, conditions d’accès et exposition de données |

### 14.1 Apprentissage fondé sur l’historique

La matière existe déjà : recommandations, décisions, exécutions et leurs statuts, waits, handoffs, gates, reviews et durée dérivée. RealizationReport apporte une lecture transverse, pas une nouvelle preuve de succès.

OmniRoute fait déjà deux choses utiles : un signal opérationnel EWMA avec confiance, et un classement par résultats d’evals persistés. Il distingue même qualité opérationnelle et qualité sémantique fournie par un évaluateur externe. C’est plus qu’un simple « dernier provider qui a répondu », mais ce n’est pas notre segmentation métier. [Qualité][o-quality], [evalRouting][o-evals].

Coopération proposée :

1. O construit un dataset par type de tâche, langage, rôle, tier minimum, modèle/version, effort, backend, complexité et politique de contexte.
2. Labels : gate passée, review indépendante approuvée, rework nécessaire, incident ultérieur ; séparer panne provider et échec de code.
3. Corréler coût et durée de **toute** la résolution, y compris preflight/rework/review, pas uniquement le premier appel bon marché.
4. Commencer par tableaux descriptifs, échantillons minimums, intervalles d’incertitude, décroissance temporelle et baseline fixe.
5. Utiliser un score validé comme préférence dans le set déjà conforme ; jamais apprendre un abaissement de tier.
6. Tester en shadow avec données existantes avant toute exploration payante. Les benchmarks LLM supplémentaires restent explicitement budgétés.
7. Publier les agrégats via un adapter versionné ou les utiliser pour ordonner les combos ; ne pas écrire directement dans la SQLite OmniRoute.

« 98 % de succès » n’a de valeur qu’avec le nombre de tâches comparables, la définition du succès, l’intervalle d’incertitude et une validation temporelle. Les tâches faciles envoyées au modèle économique créent un biais de sélection ; le taux brut ne prouve pas qu’il réussirait les tâches confiées au modèle fort. Les snapshots actuels n’offrent pas partout une clé directe decision_id → execution_id : ajouter cette corrélation avant tout apprentissage automatique.

## 15. Quota implications

### 15.1 Six populations à ne pas fusionner

| Population | Notre observation | OmniRoute vérifié | Recouvrement / compatibilité | Décision |
|---|---|---|---|---|
| A. Claude Code subscription | Petit appel CLI stream-json ; statut allowed, fenêtres unifiedWindows | OAuth usage, 5h/7d et fenêtres supplémentaires, fallback legacy | Même abonnement possible ; auth et unités différentes, équivalence à établir | KEEP ClaudeCodeAdapter ; source G complémentaire après qualification |
| B. ChatGPT/Codex subscription | app-server account/rateLimits/read ; ordinaryUsageAllowed ; primary/secondary ; credits | WHAM usage OAuth, dual windows/Spark, credits ; transport app-server d’exécution séparé | Pas de lecteur equivalent ordinaryUsageAllowed trouvé dans G | KEEP CodexAdapter canonique pour CLI natif |
| C. API Anthropic | Non couverte par notre adapter subscription | Headers/telemetry générique et consommation gateway ; support selon endpoint/credentials | Le provider anthropic local actuel ne représente pas ce compte API | Nouvelle identité de quota/source dédiée |
| D. API OpenAI | Non couverte par notre adapter Codex | Headers, usage des requêtes et policies/budgets configurés | RPM/TPM/API spend ne sont pas quota ChatGPT | Nouvelle identité/source dédiée |
| E. Providers tiers via G | Pas d’adapter actuel | Fetchers pour certains, headers pour d’autres, estimation/config/unknown ailleurs | Aucun support universel garanti malgré catalogue large | GatewayProviderStateAdapter avec supported/confidence/source |
| F. Modèles locaux | Pas d’adapter opérationnel constaté | Registre local, santé/charge ; budget logiciel éventuel | Pas de reset d’abonnement à inventer | Health/availability ; quota_window absent si non pertinent |

Sources : [Claude natif][a-claude], [Codex natif][a-codex], [Claude OAuth][o-usage-claude], [Codex OAuth][o-quota-codex], [telemetry][o-quota], [usage dispatch][o-usage].

### 15.2 Agrégation proposée

**Oui, QuotaManager devrait pouvoir agréger plusieurs sources, mais pas sous la seule chaîne "openai" ou "anthropic".**

Introduire un sujet de quota stable : fournisseur upstream + compte pseudonymisé + produit (subscription/API/local) + transport + éventuel groupe de modèles + dimension/fenêtre. Séparer l’identité de gouvernance du fournisseur de l’identité de quota. Un même compte observé par deux sources reste un seul quota, pas deux allocations additionnables.

Conserver pour chaque valeur : source, moment réel de mesure upstream, moment de collecte, unité, fenêtre, scope et confiance. Ne pas remplacer le moment d’un cache gateway par l’heure du GET et faire croire à une observation fraîche. Préserver les fenêtres distinctes qui mesurent toutes deux des tokens ; collectQuotaState d’OmniRoute choisit par dimension dans son contrat générique, tandis que d’autres chemins exposent windows : ne pas écraser 5h et 7d en un seul champ tokens.

Priorité proposée : signal natif explicite du produit effectivement utilisé ; endpoint autoritatif de ce même compte ; headers spécifiques ; configuration ; estimation. Un désaccord est conservé et expliqué. Pour une sélection exigeant quota certain, la défaillance de la source canonique doit faire échouer fermé, sans promotion d’un ancien cache ou d’un pourcentage favorable.

La route GET /api/usage/provider-limits retourne un cache sanitizé sans refresh live ; c’est un premier endpoint envisageable. GET /api/usage/[connectionId] et POST provider-limits ont une sémantique différente, à ne pas traiter comme des lectures gratuites interchangeables. L’authentification de l’endpoint doit être vérifiée sur le serveur réel, y compris sa couche centrale. [Provider limits API][o-limits-api].

WaitCoordinator reste propriétaire du prochain essai métier. Il utilise actuellement le plus proche reset connu parmi les diagnostics, puis la sélection reprobe ; ce minimum n’affirme pas que toutes les fenêtres seront libérées. Amélioration future possible : identifier les fenêtres réellement bloquantes et éviter les réveils inutiles.

### 15.3 Reset credits

OmniRoute ne se limite pas à leur affichage : il expose un endpoint POST de consommation de credits Codex/Grok avec clé d’idempotence. C’est une capacité vérifiée, distincte de l’affichage. Rien n’autorise notre orchestrateur à l’appeler. Pas de preuve recherchée ici d’une consommation automatique générale.

La future identité applicative n’aura aucun droit de consommation. Si la télémétrie nécessite un scope de gestion trop large, interposer un accès de lecture restreint plutôt que donner ce scope au worker. ResetCredit.auto_consume=False demeure la règle locale. [Endpoint credits][o-reset-api].

## 16. Cost/token opportunities

| Opportunité | Classement | Pourquoi / mesure attendue |
|---|---|---|
| Pricing et usage de requêtes G | IMMEDIATE VALUE | Rend observables tokens/coût/latence manquants ; prix catalogue distinct du débit réel |
| Headroom/reset-aware pour quotas inclus | IMMEDIATE VALUE après qualification | Utiliser capacité disponible sans confondre abonnement et paiement à l’acte |
| Cache exact du preflight déjà présent | IMMEDIATE VALUE, KEEP | Évite estimation identique ; pas besoin du cache G pour cela |
| Affinité de prompt cache fournisseur | MEDIUM TERM | Économies répétées sans résumer le code ; mesurer cache_read/write |
| Compression ciblée de logs très répétitifs | MEDIUM TERM | Évaluer outils, contraintes et succès avant/après ; conserver contenu brut en preuve |
| Optimisation de taille de contexte | MEDIUM TERM | Conserver code pertinent et critères ; ne pas retirer schémas d’outils essentiels |
| Budget quotidien/mensuel / WorkItem | MEDIUM TERM | Ledger et réservations O, métriques G ; gérer concurrence et appels auxiliaires |
| Local-first | MEDIUM TERM | Dépend du matériel, du tier et des outils ; coût énergétique et latence inclus |
| Cheap-model-first | MEDIUM TERM si modèle déjà suffisant | Le « premier essai pas cher » peut augmenter coût total/rework |
| Semantic response cache sur agent avec outils | AVOID au départ | Réponse antérieure peut rejouer un tool call ou ignorer changement de workspace |
| Fusion/chaos spéculatif | LOW VALUE / AVOID par défaut | Multiplie prompts, tokens et surfaces de fuite |
| Redis/Qdrant pour la seule économie de tokens | LOW VALUE au départ | Infrastructure non nécessaire au premier cas d’usage |
| Auto-sélection du plus grand catalogue gratuit | AVOID | Prix 0 annoncé ≠ accès, quota restant, conformité ou qualité |

Le header X-OmniRoute-Budget peut influencer le routage par coût estimé **par requête**. Le comportement par défaut de budgetFallback peut choisir le moins cher même si tous dépassent le budget ; utiliser strict lorsqu’un plafond est exigé. Ce n’est toujours pas un plafond sur un WorkItem multi-tours. [Request controls][o-controls], [moteur budget][o-auto-engine].

Le Codex executor retire certains paramètres max_tokens/max_output_tokens refusés upstream. On ne peut donc pas prétendre garantir une facture maximale universelle à partir d’un max_output_tokens transmis. Une limite fiable demande prise en charge fournisseur, réservations, rapprochement des usages et arrêt des nouvelles requêtes ; l’arrêt n’annule pas des coûts déjà encourus. Pour les subscriptions, stocker séparément prix de référence, quota consommé et dépense effectivement débitée.

Le cache exact de réponses inspecté inclut model/messages/temperature/top_p/tools/tool_choice/response_format et apiKeyId. Il n’inclut pas explicitement reasoning_effort ni SHA du workspace. Cela suffit à refuser son activation pour nos flux agentiques tant que la clé sémantique nécessaire n’est pas démontrée, même après le correctif HEAD sur les outils.

## 17. Resilience opportunities

| Niveau | Propriétaire | Échecs concernés | Action |
|---|---|---|---|
| Réseau/upstream d’une requête | G | 429, timeout HTTP, 5xx, panne de compte, modèle indisponible | Retry borné, Retry-After, cooldown, breaker, cible autorisée |
| Session/CLI/hat | Ralph + CLI, sous contrat O | Stream interrompu, processus, protocole/outils, limite runtime | Arrêt et état explicite ; ne pas inventer un succès |
| Quota d’un travail | O | Tous candidats admissibles temporairement indisponibles | WaitRecord durable, revalidation à échéance |
| Continuité métier | O | Execution orpheline, interruption, travail partiel sur disque | Réconciliation, handoff, nouvelle décision et nouvelle Execution |

La séparation proposée dans la demande est confirmée. Attention : nos services supposent essentiellement un pilotage séquentiel ; RecoveryCoordinator ne démontre pas une détection de propriétaire vivant avec lease/process lock. Il classe le dernier RUNNING pertinent comme orphelin lors de la réconciliation. Ne pas ajouter de parallélisme au nom d’OmniRoute sans traiter cette hypothèse.

Limiter le temps total et le nombre de tentatives à l’échelle de l’Execution, pas indépendamment dans chaque couche. Distinguer failure-before-output et interruption après tool call partiel. Un retry d’inférence ne garantit pas l’absence d’effet déjà effectué par un outil ; le workflow ne doit jamais relancer aveuglément une tâche complète.

La disponibilité du gateway devient elle-même un signal séparé : un G arrêté ne signifie pas « quota OpenAI épuisé ». Les workers natifs restent sélectionnables si autorisés et disponibles. Une privacy policy offline-only ne doit jamais être abandonnée pour rétablir le service.

## 18. Context/handoff implications

OmniRoute dispose de **vraie persistence de certains context handoffs**, pas seulement d’un cache en mémoire : contextHandoffs SQLite, résumé, décisions, progression, entités et TTL. Son module contextHandoff prévoit par défaut un TTL de cinq heures et des résumés LLM ; son universal handoff résout un default enabled=true, à vérifier avec les flags effectifs du déploiement. Ce contrat reste conversationnel. [Handoff G][o-context].

Notre HandoffRecord contient l’identité Project/MVP/WorkItem/Execution et des faits de travail : objectifs, résultats de tests, risques, prochaine action, SHA. Il ne dépend ni du compte d’origine ni du maintien d’un cookie ou d’une mémoire de chat. RealizationReport est une projection factuelle plus riche ; il ne devient pas la source machine de reprise.

Synergie utile : joindre route_id, session_ref et éventuel résumé G au contexte **facultatif** d’une nouvelle tentative. La reconstruction minimale doit rester possible si toute la DB G disparaît. Ne pas copier d’encrypted reasoning entre providers comme s’il était portable. Garder les couples tool_call/tool_result et les contraintes de format lors des transitions.

Default recommandé pour le spike : pin modèle/compte, context relay et universal handoff désactivés, pas de résumé auxiliaire. Leurs appels de synthèse doivent eux aussi appartenir au périmètre de candidats/privacy/budget avant activation. Séparer sessions de developer et reviewer ; ne pas partager leur conversation cachée.

## 19. Reasoning-effort implications

Décision : **HYBRID pour le transport, KEEP IN ORCHESTRATOR pour la politique.**

| Cas | Comportement inspecté | Conséquence |
|---|---|---|
| Codex natif via notre moteur | --model et -c model_reasoning_effort | Contrôle explicite connu, à conserver |
| Claude Code natif via notre moteur | _build_backend_args ne transmet que --model | Effort non garanti pour ce backend aujourd’hui ; champ d’audit ≠ paramètre effectivement appliqué |
| Codex via G | Suffixe modèle, reasoning.effort, reasoning_effort puis defaults ; clamp par modèle | Refuser alias avec effort contradictoire ; journaliser mapping effectif |
| Claude via G | Thinking adaptatif/budget, fitting aux limites de sortie | Budget peut être réduit, thinking désactivé si impossible |
| Gemini et autres | Traductions spécifiques, capability registry, filtres params | Certifier modèle/version/protocole, pas mapping universel supposé |
| Modèle sans effort explicite | Paramètre ignoré/retiré selon support | Profil « effort non applicable » explicitement qualifié, jamais assimilation automatique à high |

Sources : [moteur natif][a-engine], [Codex G][o-codex], [thinking budget][o-thinking], [param filtering][o-params], [reasoning policies][o-reasoning].

Séparer recommended_reasoning (hint de l’estimator existant) d’une future contrainte required_reasoning. Conserver requested_reasoning et effective_reasoning/mapping_version dans l’audit. Si une contrainte high est dure et que le transport la plafonne, refuser cette cible au lieu de déclarer high dans ExecutionRecord. Si la provider API n’expose pas l’effort réellement appliqué, inscrire « mapping envoyé vérifié, application interne inconnue ».

Les effort caps sont provider/modèle-dépendants ; la même chaîne high n’est pas une quantité de calcul comparable entre fournisseurs. Le tier doit donc être qualifié avec son effort et son environnement d’outils.

## 20. Security assessment

Échelle : LOW = risque limité et contrôles simples ; MEDIUM = validation/config requise ; HIGH = peut compromettre secrets, code, dépenses ou gouvernance. Les constats ci-dessous ne constituent pas une preuve d’exploitation.

| Surface | Niveau | Constat de code / exposition | Recommandation |
|---|---|---|---|
| Secrets au repos G | HIGH | AES-256-GCM disponible ; sans clé de chiffrement le code accepte le plaintext | Clé explicite hors DB, permissions, sauvegarde/restauration de clé testée |
| Auth de l’inférence | HIGH | .env.example contient REQUIRE_API_KEY=false ; comportements centralisés et route-specific | Clé dédiée, bind loopback explicite, tests des endpoints sur serveur complet |
| API de gestion | HIGH | Config providers/CLI, refresh, systèmes et credits ; scopes puissants | Aucune clé admin dans les workers ; lecture télémétrie par façade restreinte si nécessaire |
| Reset credits | HIGH | POST de consommation présent | Bloquer cette action pour l’intégration ; pas seulement convention dans le prompt |
| OAuth Claude/Codex | HIGH | Tokens réutilisables, refresh et endpoints de produits distincts | Un propriétaire des credentials/refresh par compte ; accès subscription pas migré d’office |
| CLI config / environnement | HIGH | Routes d’intégration écrivent settings/config/auth globaux | Config temporaire par Execution ; ne pas utiliser les boutons d’auto-configuration globale |
| Logs de prompts/réponses | HIGH | Call logs et artifacts peuvent stocker request/response/pipeline | Collecter metadata minimale, désactiver payload capture, rétention courte et droits stricts |
| Redaction | MEDIUM | Helpers de masquage existent | Tester des canaris synthétiques ; ne pas croire que masquage protège tout contenu de code |
| Cache et mémoire | HIGH | Réponses/contexte persistés, possibles tool calls et collisions de contraintes | Désactivés pour flux agentiques initiaux ; isolation par projet/rôle/policy |
| Egress et providers tiers | HIGH | Grand catalogue, bases personnalisées, web/cookies, proxy/relay possibles | Allowlist explicite, DNS/redirect/SSRF vérifiés, providers officiels/local seulement au départ |
| Cloud sync / télémétrie / feeds | MEDIUM à HIGH selon données | Code cloudSync, URLs configurables, services/feeds de fond ; build Next telemetry explicitement coupée dans Docker | Désactiver cloud/feeds inutiles, observer egress réel ; ne pas conclure « zéro réseau » du terme local-first |
| Subprocess G | HIGH pour endpoints privilégiés | Outils CLI et installation/upgrades ; certains helpers utilisent correctement execFile(argv) | Désactiver contrôle des CLIs hôte et auto-install ; user de service distinct |
| Subprocess O | MEDIUM | create_subprocess_exec, pas de shell implicite ; environnement hérité ; outils exécutent du code projet | Env allowlist, pas de changement global, tests sandbox/process group avant production |
| Sandbox Ralph/CLI | HIGH si supposée sans preuve | Spike historique explicitement NOT VALIDATED pour overrides Codex | Test isolé avec permissions restreintes ; gateway ne sandboxe pas le code |
| Supply chain | HIGH | 82 deps directes, 8 optionnelles, bindings natifs, builds/installers | Version+lock/digest figés, SBOM/artifact scan au spike, pas latest/auto-update |
| Injection de politique par prompt | HIGH | Classifieurs/réécritures peuvent changer une intention ; prompts non fiables | Contraintes dans le code/credential scope, jamais dans le seul texte utilisateur |
| Rapport produit | LOW | Texte, liens et HTML sans payloads privés ni credentials | Échapper HTML, aucun chargement externe automatique |

Sources principales : [encryption][o-encryption], [auth gestion][o-management], [env defaults][o-env], [logs][o-logs], [CLI Codex][o-cli-codex], [CLI Claude][o-cli-claude], [Dockerfile][o-docker], [notices][o-notices].

Pas d’affirmation « aucun shell dans OmniRoute » : certains helpers sont sûrs, mais l’application comporte une vaste surface de commandes. Pas d’affirmation « quota GET toujours read-only » : une lecture d’usage live peut déclencher refresh credentials et persistence de statut ; choisir le cache de lecture avec sa sémantique connue.

## 21. License assessment

### 21.1 Licence principale

LICENSE et package.json déclarent **MIT**, copyright 2026 diegosouzapw. Le texte autorise notamment utilisation, copie, modification et redistribution, avec conservation de la notice de copyright et de permission dans les copies ou portions substantielles ; absence de garantie. Pas d’obligation de publier notre code induite par le seul texte MIT. [Licence du commit][o-license] ; [texte MIT de l’OSI](https://opensource.org/license/mit).

Aucune licence explicite de notre projet n’a été trouvée dans les fichiers versionnés inspectés ni dans pyproject.toml. Cela ne bloque pas techniquement une utilisation locale de logiciel MIT, mais empêche de certifier une compatibilité de redistribution avec une licence de projet encore non déclarée. Définir cette licence avant publication d’un assemblage distribué. Analyse factuelle de licences, pas avis juridique absolu.

### 21.2 Décisions par forme de réutilisation

| Forme | Classification demandée | Analyse technique/licence |
|---|---|---|
| Installer/utiliser un service séparé | USE AS DEPENDENCY | Voie recommandée ; notices conservées dans le paquet ; vérifier conditions d’accès upstream |
| Dépendance npm dans un autre produit | USE AS DEPENDENCY | MIT du cœur permissive ; closure de dépendances et assets redistribués à examiner |
| Importer un module TS | COPY/MODIFY CODE ou USE AS DEPENDENCY selon packaging | Possible sous licence applicable ; couplage technique fort avec src/lib et DB, déconseillé |
| Copier/modifier du code du cœur | COPY/MODIFY CODE | Conserver notices, provenance/commit et notices de code tiers réellement repris |
| Vendoriser tout ou partie | COPY/MODIFY CODE | Même obligations, plus suivi sécurité/upstream ; pas recommandé pour nous |
| Réimplémenter indépendamment le concept de score/cooldown | REIMPLEMENT CONCEPT | Pas copie du texte source ; documenter conception, ne pas reprendre assets ni supposer clearance brevets/marques |
| Redistribuer bundle complet avec tous logos et binaires | NOT LEGALLY CLEAR pour les éléments non clarifiés | MIT racine insuffisante pour conclure sur tous les éléments |
| Utiliser tokens OAuth/cookies avec un service upstream | NOT LEGALLY CLEAR sans vérifier les conditions du produit | La licence du gateway n’accorde aucun droit d’accès aux services tiers |

### 21.3 Notices qui empêchent un feu vert global

THIRD_PARTY_NOTICES décrit les transports wreq-js et leur closure native, codex-chatgpt-web, un codec GCF et des assets. Pour wreq-js, le projet indique que les tarballs natifs ne contiennent pas eux-mêmes de LICENSE/NOTICE et fournit un inventaire conservateur ; cela demande de conserver cet ensemble lors d’une redistribution, pas uniquement LICENSE racine.

Les notices theSVG recensent 65 assets identiques à un snapshot upstream. Plusieurs classes restent en HOLD : notices originales de 46 entrées revendiquées MIT, un NOTICE Apache, deux brand-use, un Custom MiniMax et un MISSING HuggingFace. Une provenance identifiée ne prouve pas que l’amont détenait tous les droits sur les logos. Ne pas copier l’interface/les logos dans notre produit au titre du seul MIT.

Risque : **LOW pour le texte MIT du cœur pris isolément ; MEDIUM pour une dépendance/service interne maîtrisé ; HIGH/non résolu pour redistribuer sans examen le bundle complet**. Avant distribution : choisir les assets nécessaires, obtenir/valider les notices applicables, SBOM des binaires et dépendances. Les droits de marque et les conditions fournisseurs restent séparés. [Notices tierces][o-notices].

## 22. Maintainability assessment

| Dimension | Observation reproductible | Évaluation |
|---|---|---|
| Taille | 365 458 lignes physiques TS/TSX/JS/MJS sous src et open-sse ; commentaires et tests internes inclus | Grosse application, pas bibliothèque mince |
| Inventaire | 11 018 fichiers TS/TSX/JS/MJS versionnés à l’échelle du dépôt | Périmètre large, includes tests/scripts/UI |
| Tests | 5 662 fichiers sous tests dont chemin/nom contient test ou spec | Compte de fichiers, pas assertions ni preuve que tout passe |
| CI | Lint, checks de typing ciblés, tests/coverage, build, sécurité, nightly | Investissement réel ; résultat actuel non exécuté ici |
| Coverage configurée | Gate 60 % statements/lines/functions/branches dans scripts | Seuil déclaré, pas couverture mesurée par cet audit |
| Persistence | 173 migrations SQL sous src/lib/db/migrations | Upgrades/rollback DB non triviaux |
| Typing | strict=false global et core ; strictNullChecks ciblé, noImplicitAny=false core | TypeScript ne garantit pas un contrat strict uniforme |
| Dépendances | 82 directes, 8 optionnelles, 58 dev dans package courant | Surface de supply chain significative |
| Organisation | Extractions combo en nombreux modules, imports croisés open-sse/src/lib | Amélioration de modularité visible, SDK autonome non établi |
| Release cadence | Nombreux tags 3.8.x ; latest publié 3.8.50 ; branche 3.8.51 avec beaucoup de correctifs | Activité forte mais peu de stabilité à présumer |
| Qualité de HEAD | Message de commit indique tests hérités rouges et ratchets de taille/provider count en échec sur la base | Signal de risque concret, non résultat reproduit ici |
| Bus factor observable | Sur 500 commits récents : auteur principal 179, second 59, troisième 44 ; signatures multiples | Contribution distribuée, pas preuve d’une maintenance indépendante des points critiques |
| Documentation | Très fournie, mais 19 vs 20 stratégies, compte providers variable et gros Unreleased | Code et version exacte doivent primer |
| Stabilité API | APIs de gestion couplées aux schémas/config et migrations ; pas contrat stable de route réservée trouvé | Pin version, adapter HTTP et tests de contrat |

Le nombre de contributeurs ou l’activité GitHub ne mesure pas à lui seul le bus factor opérationnel. Le message de HEAD cite 203/208 assertions vertes sur un lot et cinq échecs reproduits sur sa base ; il ne faut ni l’ignorer ni le transformer en résultat de notre audit. [Commit][o-head], [package][o-package], [CI][o-ci], [typing][o-tsconfig].

**Risque de dépendance : MEDIUM à HIGH**, acceptable pour une gateway optionnelle isolée ; trop élevé pour remplacer le cœur ou suivre automatiquement la branche.

## 23. Operational complexity

### 23.1 Comparaison des trajets

| Aspect | Native CLI direct | Ralph/CLI → OmniRoute |
|---|---|---|
| Processus | Python + Ralph + CLI | Les mêmes, plus serveur Node ; Redis selon déploiement |
| Réseau | CLI → provider | CLI → HTTP loopback G → provider |
| Traductions | Natives du CLI | Possibles conversions de formats, SSE et outils |
| Startup | CLI et Ralph | Service chaud permanent préférable ; startup/migrations supplémentaires |
| Latence | Baseline | Proxy/auth/SQLite/scoring, éventuellement quota live ; non mesurée |
| Cache | Provider/CLI | Caches additionnels, parfois bénéfice et parfois risque |
| Persistence | Stores O + state CLI/Ralph | DB G, WAL, logs/artifacts, secrets et sauvegardes |
| Availability | Provider et CLI | Dépend aussi de G ; chemin natif doit survivre à sa panne |
| Monitoring | Statuts/exécutions | /livez, /healthz, upstream health, taux erreur, TTFT, disk, lag event-loop |
| Exploitation | Dépendances Python/Ralph/CLI | Upgrades Node/package/native bindings, config de service, rotation |

Ne pas annoncer « quelques millisecondes » sans mesure. L’overhead d’un proxy chaud et celui d’un fetch de quota upstream sont très différents ; le second peut dominer le premier. Mesurer p50/p95 TTFT, durée complète, RSS au repos/en charge, startup, taille disque et writes DB.

### 23.2 Mini-PC Linux et installation proposée

Runtime package déclaré : Node **>=22.22.2 <23 ou >=24.0.0 <27**. Le Dockerfile étudié utilise Node 26 trixie-slim ; la compilation des bindings peut demander Python3/make/g++, et la base installe notamment libsecret et certificats. Python de notre orchestrateur reste indépendant.

Mode recommandé : **service dédié sous systemd, paquet/runtime figés dans un répertoire de release indépendant**, lancé par un compte de service distinct, accessible uniquement en loopback ; pas d’installation globale mouvante. Répertoire de données privé, clé de chiffrement séparée, configuration explicite. Les chemins de déploiement sont à décider au spike, sans recopier les homes/configs des CLIs utilisateur. Un artefact Docker minimal figé par digest est une alternative si Docker est déjà la convention du mini-PC.

Désactiver auto-update, auto-configuration des CLIs hôte, providers web, cloud sync et services accessoires. Ne pas monter le workspace ni les credentials Claude/Codex utilisateur dans G pour un premier essai API/local. Si local inference sur le même hôte, tenir compte de la mémoire consommée par le modèle ; aucune hypothèse GPU/RAM n’a été faite.

Le service expose habituellement dashboard 20128, API dédiée 20129 et live WS 20132 selon mode/config ; un seul port d’entrée utilisé par l’orchestrateur, interfaces restantes fermées ou loopback. Tous ces numéros proviennent de la config étudiée, pas d’un besoin intrinsèque de notre architecture.

Le Compose fourni démarre Redis sans profil, avec les profils applicatifs base/web/cli/host et de nombreux sidecars optionnels. Ne pas le présenter comme « un unique conteneur léger ». SQLite/in-memory peuvent suffire à un mode mono-instance si les chemins choisis sont qualifiés ; Redis n’est pas à ajouter sans usage démontré. Pas de Qdrant, Bifrost ou navigateur requis pour le scénario minimal. [Docker][o-docker], [Compose][o-compose], [package][o-package].

Upgrade : nouvel artefact figé, sauvegarde cohérente DB+WAL et clé, tests de contrat offline, canary limité, bascule des nouvelles exécutions. Rollback : ancien artefact **et données compatibles** ; un binaire ancien sur DB déjà migrée n’est pas un rollback garanti.

## 24. Migration strategy

### 24.1 Trois scénarios

| Scénario | Responsabilités G | Composants impactés | Effort relatif | Bénéfice | Risque | Rollback |
|---|---|---|---|---|---|---|
| MINIMAL | Une route d’inférence à modèle unique, optionnelle, d’abord upstream factice puis API/local qualifié | Profile/Registry, env du moteur, corrélation d’audit minimale | M | Prouver chaîne Ralph/CLI/proxy et mesurer service | MEDIUM : compatibilité outils/env | route_type=native pour nouvelles exécutions ; arrêter G après drainage |
| HYBRID | Pool fini certifié, telemetry, résilience bornée, optimisation économique contrôlée | InferenceRouter, Selector/Adaptive, QuotaManager/adapters, audit attempts, gates de policy | E | Gains de routing sans abandonner gouvernance | HIGH au développement, borné par tests et opt-in | Même chemin natif ; conserver décisions/handoffs ; restore G séparément |
| DEEP | Quasi tout classement modèle/provider et fallback dans G, pools dynamiques | Contrats identité/quota/review, tous rôles, multi-attempt, policy compiler, exploitation | TE | Optimisation centralisée pour nombreux fournisseurs/projets | HIGH : couplage et invariants cachés, bénéfice actuel insuffisant | Coûteux si anciens chemins supprimés ; ne jamais les supprimer prématurément |

**Scénario retenu : HYBRID progressif, dont MINIMAL est le premier jalon obligatoire.** DEEP n’est pas recommandé pour la roadmap proche.

### 24.2 Portes de validation avant adoption réelle

| Porte | Preuve attendue | Échec signifie |
|---|---|---|
| Compatibilité CLI/Ralph | Plusieurs tours, tool calls/outputs, events métier, streaming et timeout via upstream factice | Garder native |
| Aucun downgrade | Tests adversariaux primary/fallback/alias/compte/effort et reprise | Pas de multi-model routing |
| Identité réelle | Provider/model/effort/candidate hash liés à toutes tentatives | Pas d’audit fiable, pas production |
| Review independence | Auteur et reviewer vers upstreams autorisés ; cache/session séparés | Bloquer review routée |
| Quotas | Comparaison fixtures natives/OAuth, unités, fenêtres, stale, mauvais compte | Adapters natifs seuls |
| Zéro reset credit | Requêtes et autorisations démontrent absence de consume | Bloquer intégration |
| Sécurité | Auth, egress, logs canaris, permissions, absence d’édition globale | Ne pas connecter de credentials réels |
| Recovery | Panne G, kill CLI/stream, restart O, DB G absente | Aucun remplacement d’un chemin natif |
| Valeur mesurée | Coût total/réussite/latence comparés sur corpus pertinent | OPTIONAL_LATER pour l’étape d’optimisation |
| Exploitation | Install/upgrade/restore reproduits sur environnement mini-PC | Spike seulement |

Ces portes sont des critères de roadmap ; aucune exécution supplémentaire de workers ou de tests live n’est autorisée par leur simple description.

## 25. Risks

| Risque | Gravité | Mitigation | Priorité |
|---|---|---|---|
| Downgrade via fallback ou mapping effort | HIGH | Set fermé, profils certifiés, pin, refus avant dispatch | P0 |
| Audit enregistre l’alias au lieu du modèle réel | HIGH | requested/effective + tentative liée à Execution | P0 |
| Deux reviewers logiques vers même upstream | HIGH | Comparaison identité effective et historique auteur | P0 |
| Confusion API/subscription/compte | HIGH | QuotaSubject distinct, source et fenêtre | P0 |
| Modification des homes CLI | HIGH | Env/config par processus et compte de service isolé | P0 |
| Fuite code/secrets par logs/fallback/summarizer | HIGH | Egress allowlist, payload logging off, appels auxiliaires off | P0 |
| Retries multiplicatifs / effets outils répétés | HIGH | Budget de tentatives/délai global, reprise métier explicite | P0 |
| Branche de release mouvante et DB migrations | HIGH | Pin commit/digest, contrat et backup restore | P1 |
| Redistributions assets non clarifiées | HIGH | Pas de copie UI/assets, clearance avant bundle | P1 |
| Surcoût d’exploitation sans économie prouvée | MEDIUM | Mesure MINIMAL et seuil de décision | P1 |
| Qualité tiers déclarative non calibrée | HIGH | Benchmarks contrôlés, pas score gateway assimilé à certification | P1 |
| Biais de routing historique | MEDIUM | Labels indépendants, corpus, échantillons/confidence | P2 |
| Dette locale préexistante (capability review, SHA gates, recovery séquentielle) | HIGH pour élargissement | Corriger/qualifier dans slices dédiées avant complexification | P0/P1 |

Un HTTP 200, un low cost ou une bonne santé provider ne compensent aucun de ces invariants.

## 26. Rollback strategy

1. L’intégration reste désactivable au niveau profil/policy, sans import obligatoire au démarrage ni appel réseau si absente.
2. Les workers et adapters natifs conservent leur configuration et leurs credentials propres.
3. Un rollback choisit le chemin des **nouvelles** exécutions ; il ne reroute pas silencieusement une session active. Drainer ou marquer l’interruption, puis handoff/nouvelle Execution.
4. Les migrations O sont additives et les snapshots historiques ne sont pas réécrits. Un record dont provider était anthropic reste celui de cette époque.
5. Conserver localement les traces nécessaires avant la suppression/rotation des caches G. Pouvoir reconstruire le travail sans sa base.
6. Sauvegarder/restaurer ensemble l’artefact G, sa DB cohérente et sa clé de chiffrement ; ne pas mélanger schémas de versions.
7. Après bascule native, vérifier les contraintes toujours applicables : pas de retour cloud pour une tâche offline-only, pas de tier inférieur pour « débloquer ».
8. Ne jamais utiliser un reset credit pour réussir le rollback ou rétablir la disponibilité.

## 27. Recommended roadmap

La roadmap ci-dessous est **une proposition dans ce rapport**, sans modification de ROADMAP.md. Les IDs OR-* évitent de collisionner avec les slices numérotées du projet en cours.

| Slice proposée | Objet concret | Dépendances | Sortie vérifiable | Priorité |
|---|---|---|---|---|
| Précondition locale | Stabiliser Slice 18 et préparer Slice 19 adaptive review/planning ; résoudre convention reviewer/code_review | État courant | Même invariant de tier sur tous rôles, tests gouvernance | P0 |
| OR-0 — Gateway qualification spike | Instance isolée/pinnée, upstream factice, scénario MINIMAL singleton | Aucun code prod changé pendant l’audit ; décision ultérieure | Rapport compatibilité, ressources, auth/logs/egress, install/restore | P0 |
| OR-1 — Optional inference route | route_type, InferenceRouter natif/G, env par Execution, audit requested/effective | OR-0 | Chemin natif sans G ; un modèle fixé ; échec fermé | P1 |
| OR-2 — Certified candidate-set routing | Génération de combos immuables, fermeture des fallbacks, mapping reasoning, upstream identity | OR-1, gouvernance review | Aucun appel hors set, pin/corrélation, tests alias/tools/compte | P1 |
| OR-3 — Quota subjects and telemetry | Identités produit/compte, GatewayProviderStateAdapter, fraîcheur/source/conflits | OR-1 ; avant optimisation de quotas OR-2 | Fixtures A..F, pas de fusion de compte, aucun consume | P1 |
| OR-4 — Bounded transport resilience | Retry/cooldown/health G avec deadline globale, recovery O | OR-2 + OR-3 | Pannes avant/après stream, reprise cross-worker, pas double outil | P1 |
| OR-5 — Cost and token policies | Ledger, coût de résolution complet, plafonds, cache affinity mesurée | Audit fiable | Mesures réelles vs estimées ; strict budget quand supporté | P2 |
| OR-6 — Local and privacy profiles | Un backend/local-model qualifié, policy offline-only et egress | OR-2, matériel mesuré | Réseau bloqué sans fallback cloud, tier conservé | P2 |
| OR-7 — Historical outcome routing | Jointures robustes, dataset, score descriptif puis shadow | Volume suffisant + OR-5 | Résultats temporels avec échantillons/confidence, pas auto-downgrade | P3 |

Ne pas retarder indéfiniment la gouvernance Git/PR/merge (Slice 20 annoncée) pour ajouter des stratégies de routage. Le gain produit immédiat reste une exécution gouvernée, traçable et reprenable. Une grande palette de modèles sans ces garanties ne rend pas la livraison plus fiable.

### 27.1 Tableau de décision synthétique

| Area | Current component | OmniRoute equivalent | Decision | Benefit | Risk | Priority |
|---|---|---|---|---|---|---|
| Quota natif | Claude/Codex adapters | OAuth usage fetchers | KEEP + optionally aggregate | Source alternative, moins de probes Claude possibles | Mauvais compte/signal/unité | P1 |
| Fraîcheur | QuotaManager | Caches/pollers | EXTEND | Plusieurs transports | Stale ou double quota | P1 |
| Modèle/tier | AdaptiveExecutionSelector | Auto/combo scoring | KEEP contraintes + DELEGATE départage | Santé/coût au runtime | Downgrade | P1 |
| Identité agent | WorkerRegistry | Provider/model registry | KEEP + EXTEND route refs | Extensibilité propre | Catalogue non approuvé | P0 |
| Runtime | RalphExecutionEngine | CLI compatibility gateway | KEEP + EXTEND env | Autres inférences | Casser outils/session | P0 |
| Workflow | Ralph | Pipeline/fusion | KEEP Ralph ; DO_NOT_USE substitution | Réutilisation validée | Deux boucles agentiques | P0 |
| Audit | ExecutionStore | Call logs/trace/explain | KEEP + EXTEND attempts | Coût et causalité | Trace reconstruite/incomplète | P0 |
| Résilience HTTP | Pas de couche dédiée | Breaker/cooldown/retry | DELEGATE borné | Réutiliser beaucoup de code | Retry multiplicatif | P1 |
| Reprise métier | Wait/Recovery/Handoff | Cooldown/context relay | KEEP | Continuité durable | Résumé TTL pris pour état | P0 |
| Raisonnement | Profile/ExecutionRequest | Thinking translation | HYBRID | Multi-protocoles | Effort plafonné/supprimé | P0 |
| Coût | cost_rank | Pricing/budgets | EXTEND policy + INTEGRATE metrics | Optimiser coût total | Coût estimé pris pour facture | P2 |
| Cache | Estimator fingerprint | Semantic response cache | KEEP local ; DO_NOT_USE agentic cache initial | Évite régression | Outils/reasoning/SHA absents | P0 |
| Privacy | Native config limitée | Connexions/policies | EXTEND + allowlist | Choix local/région | auto/offline non strict | P1 |
| Roadmap/release | Managers/stores/approval | Pas équivalent | KEEP AS IS | Gouvernance préservée | Aucun gain à remplacer | P0 |
| Historical routing | Faits persistants | Quality EWMA + evalRouting | OPTIONAL coopération | Meilleurs choix mesurés | Biais/peu de données | P3 |

## 28. Go / No-Go recommendation

**GO_INCREMENTALLY.**

Raison principale : OmniRoute apporte des briques d’inférence et de résilience coûteuses à reconstruire, tout en permettant une frontière HTTP optionnelle. Sa sélection libre et ses fonctions auxiliaires n’offrent pas directement les garanties métier du projet ; l’adoption doit passer par une qualification ciblée.

Architecture recommandée : **C hybride**. Scénario : **HYBRID, démarré par MINIMAL singleton**. Aucun remplacement complet. Native reste disponible. La décision n’est pas GO_NOW pour déployer la branche actuelle avec les defaults.

Trois bénéfices : extensibilité multi-provider, réutilisation de résilience/quotas, visibilité coût/performance.

Trois risques : violation des contraintes modèle/effort/indépendance, centralisation de secrets/prompts, maintenance et packaging/licences tierces.

Si OR-0/OR-2 ne prouvent pas un routage fermé et compatible avec Ralph/CLI, rester en A et reporter OmniRoute. Cela n’invalide pas le travail déjà construit et ne justifie aucune dégradation pour forcer l’intégration.

### Livrables et validation de l’audit

- Markdown : docs/OMNIROUTE_OPPORTUNITY_REPORT.md.
- HTML autonome : docs/reports/omniroute-opportunity-report.html, projection du Markdown, UTF-8, CSS embarqué, navigation et tableaux, aucun JavaScript ni CDN.
- Aucune modification de code fonctionnel, tests, configuration ou roadmap effectuée par l’audit.
- Aucun commit ni push effectué par l’audit ; le commit concurrent 495588a a été observé et pris en compte.
- Aucun reset credit consommé, aucun worker LLM lancé par l’audit.
- Validation finale : diff whitespace, diff stat, statut Git et contrôle du périmètre des fichiers ; pas de suite complète de tests.
- Les sources locales ci-dessous sont celles du projet inspecté ; les liens OmniRoute sont figés au commit analysé. Les chiffres et contrats pourront changer après cette version.

## Annexe — Sources et index de preuve

Les tableaux renvoient à des fichiers/symboles concrets. Pour réexaminer une conclusion, partir des liens, puis de leurs tests et appelants. Les références locales sont relatives au rapport Markdown ; le HTML ajuste leur base pour rester utilisable depuis docs/reports.

Tests locaux particulièrement pertinents : tests/test_adaptive_execution.py, tests/test_worker_selector.py, tests/test_quota_manager.py, tests/providers/test_claude_code_adapter.py, tests/providers/test_codex_adapter.py, tests/test_ralph_execution_engine.py, tests/test_wait.py, tests/test_recovery.py, tests/test_review.py, tests/test_planning.py, tests/test_release_manager.py, tests/test_roadmap_application.py, tests/test_realization_report.py et tests/integration/test_cross_worker_resume_e2e.py.

Tests OmniRoute lus ou repérés pour les frontières critiques : [expansion pool][o-test-expansion], [qualité opérationnelle/sémantique][o-test-quality], [telemetry et état inconnu][o-test-quota], [cache de réponses][o-test-cache], [reasoning clamp][o-test-effort]. Ils constituent des cibles pour le spike, pas des résultats verts produits ici.

[a-selector]: ../src/orchestrator/worker_selector.py
[a-adaptive]: ../src/orchestrator/adaptive_execution.py
[a-quota]: ../src/orchestrator/quota_manager.py
[a-engine]: ../src/orchestrator/ralph_execution_engine.py
[a-workers]: ../config/workers.yaml
[a-release-manager]: ../src/orchestrator/release_manager.py
[a-status]: status.md
[a-smoke]: reports/real-cross-worker-resume-2026-09-13.html
[a-adapter]: ../src/orchestrator/providers/adapter.py
[a-contracts]: ../src/orchestrator/providers/contracts.py
[a-claude]: ../src/orchestrator/providers/claude_code_adapter.py
[a-codex]: ../src/orchestrator/providers/codex_adapter.py
[a-registry]: ../src/orchestrator/worker_registry.py
[a-estimation]: ../src/orchestrator/complexity_estimation.py
[a-executions]: ../src/orchestrator/execution_store.py
[a-mvp]: ../src/orchestrator/mvp_manager.py
[a-state]: ../src/orchestrator/project_state.py
[a-handoff]: ../src/orchestrator/handoff.py
[a-wait]: ../src/orchestrator/wait.py
[a-recovery]: ../src/orchestrator/recovery.py
[a-validation]: ../src/orchestrator/validation.py
[a-review]: ../src/orchestrator/review.py
[a-planning]: ../src/orchestrator/planning.py
[a-activity]: ../src/orchestrator/activity_report.py
[a-approval]: ../src/orchestrator/approval.py
[a-application]: ../src/orchestrator/roadmap_application.py
[a-realization]: ../src/orchestrator/realization_report.py
[a-spike]: SPIKE_RALPH.md
[a-adaptive-doc]: ADAPTIVE_EXECUTION.md
[o-expansion]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/autoStrategy.ts#L428
[o-auto-engine]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/engine.ts
[o-virtual]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/virtualFactory.ts#L1064
[o-codex]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/executors/codex.ts#L1360
[o-cache]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/semanticCache.ts#L188
[o-notices]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/THIRD_PARTY_NOTICES.md
[o-combo]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo.ts
[o-auth]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/sse/services/auth.ts
[o-structure]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/comboStructure.ts
[o-scoring]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/scoring.ts
[o-task]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/taskAwareRouter.ts
[o-fitness]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/taskFitness.ts
[o-catalog]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/builtinCatalog.ts
[o-chat]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/sse/handlers/chat.ts
[o-quota]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/quota/providerQuotaTelemetry.ts
[o-preflight]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/quotaPreflight.ts
[o-fallback]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/accountFallback.ts
[o-base]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/executors/base.ts
[o-health]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/healthz/route.ts
[o-breaker]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/shared/utils/circuitBreaker.ts
[o-cooldown]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/providerCooldownTracker.ts
[o-attempts]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/comboAttemptLoop.ts
[o-compat]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/comboCompatFallback.ts
[o-sessions]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/sessionManager.ts
[o-affinity]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/db/sessionAccountAffinity.ts
[o-context]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/contextHandoff.ts
[o-cache-affinity]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/promptCacheAffinity.ts
[o-compression]: https://github.com/diegosouzapw/OmniRoute/tree/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/compression
[o-sorters]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/targetSorters.ts
[o-trace]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/decisionTrace.ts
[o-explain]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/usage/routeExplain.ts
[o-cli-claude]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/api/cli-tools/claude-settings/route.ts
[o-cli-codex]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/api/cli-tools/codex-settings/route.ts
[o-providers]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/config/providerRegistry.ts
[o-quality]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/routing/quality.ts
[o-evals]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/evalRouting.ts
[o-strategies]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/shared/constants/routingStrategies.ts
[o-ordering]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/applyStrategyOrdering.ts
[o-quota-strategies]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/quotaStrategies.ts
[o-dispatch]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/dispatchPrelude.ts
[o-targets]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/combo/targetResolution.ts
[o-appserver]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/executors/codex-app-server.ts
[o-combo-api]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/api/combos/route.ts
[o-usage-claude]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/usage/claude.ts
[o-quota-codex]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/codexQuotaFetcher.ts
[o-usage]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/usage.ts
[o-limits-api]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/api/usage/provider-limits/route.ts
[o-reset-api]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/app/api/usage/codex-reset-credit/route.ts
[o-controls]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/services/autoCombo/requestControls.ts
[o-thinking]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/translator/request/openai-to-claude/thinkingBudget.ts
[o-params]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/open-sse/translator/paramSupport.ts
[o-reasoning]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/reasoningRouting/policy.ts
[o-encryption]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/db/encryption.ts
[o-management]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/api/requireManagementAuth.ts
[o-env]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/.env.example
[o-logs]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/src/lib/usage/callLogs.ts
[o-docker]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/Dockerfile
[o-license]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/LICENSE
[o-package]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/package.json
[o-ci]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/.github/workflows/ci.yml
[o-tsconfig]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tsconfig.typecheck-core.json
[o-compose]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/docker-compose.yml
[o-test-expansion]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tests/unit/combo-auto-candidate-expansion.test.ts
[o-test-quality]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tests/unit/routing-quality.test.ts
[o-test-quota]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tests/unit/quota-telemetry-adaptive-routing.test.ts
[o-test-cache]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tests/unit/semantic-cache.test.ts
[o-test-effort]: https://github.com/diegosouzapw/OmniRoute/blob/152d95108c9c3d557562311ffed63240a511eb31/tests/unit/reasoning-effort-clamp-and-retry.test.ts
[o-head]: https://github.com/diegosouzapw/OmniRoute/commit/152d95108c9c3d557562311ffed63240a511eb31
[o-release]: https://github.com/diegosouzapw/OmniRoute/releases/tag/v3.8.50
