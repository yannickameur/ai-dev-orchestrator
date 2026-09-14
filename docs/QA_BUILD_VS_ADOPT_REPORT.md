# Slice 21 — QA Architecture + Build-vs-Adopt Study

Recherche principale : 13 septembre 2026 ; compléments et finalisation : 14 septembre 2026 (Europe/Paris). Projet : ai-dev-orchestrator.
Statut : STUDY DONE — proposition soumise à arbitrage utilisateur, aucune implémentation.
Recommandation primaire : BUILD_INTERNAL, moteur mince réutilisant le socle existant.
SECOND_BEST_OPTION : HYBRID, après qualification d'un besoin et d'un moteur externe.

## 1. Executive summary

Si nous commencions aujourd'hui, pour le portefeuille actuellement démontré par ce dépôt — orchestration Python, gouvernance Git, reprise et tests déterministes — nous construirions le minimum de QA interne qui manque. Nous ne construirions ni un nouveau runner généraliste, ni un device cloud, ni un autre système d'audit/recovery.

Ce choix n'est pas une obligation de posséder un « QA LLM maison ». Une architecture DIRECT EXTERNAL reste recevable si le fournisseur satisfait le contrat et les critères éliminatoires. L'étude trouve une vraie autonomie externe, mais pas la preuve qu'un candidat remplace aujourd'hui les tests Python existants, protège leur sémantique et délivre une preuve finale immuable complète.

Les résultats importants :

- TestSprite est le candidat fonctionnel le plus proche d'un moteur externe autonome web/API. Son CLI public offre de vrais objets run et des artefacts structurés. Mais son code documente des chemins frontend V3 où l'URL demandée et la désactivation du healing ne sont pas respectées : exclusion du gate final obligatoire dans cette configuration.
- BrowserStack est le meilleur choix pour une matrice navigateurs/appareils réels. C'est une famille de produits, pas un QAEngine généraliste unique. Les assertions et suites standard conservées dans Git offrent une voie complémentaire solide.
- Momentic mérite une véritable évaluation, pas une réduction à un outil SaaS UI : YAML local, sélection par diff, CLI, résultats avec commit SHA et mobile simulé sont documentés. Quarantine, recovery, caches, classification influençant l'exit code et dépendances cloud imposent une policy stricte.
- Diffblue Cover est le meilleur spécialiste de génération de tests Java. Ce n'est pas un moteur QA Python. La release étudiée signale une suppression possible de tests existants en mode merge ; génération séparée et protection des baselines sont indispensables.
- L'interne n'est pas déjà conforme : le QAEngine, la protection des tests et la vérification finale read-only n'existent pas encore. Le rapport identifie également une fenêtre de dérive SHA au merge dans le code actuel.

La scorecard donne INTERNAL 75, HYBRID 74, BROWSERSTACK 71, MULTI_ENGINE_BY_STACK 70, MOMENTIC 62, DIFFBLUE 56, TESTSPRITE 55. Ce sont des points d'adéquation et de coût d'intégration, non une mesure expérimentale de qualité. L'écart de 1 point INTERNAL/HYBRID n'est pas significatif ; le périmètre actuel et les gates éliminatoires départagent les options.

Un POC interne comparatif est proposé ; un second, TestSprite, reste conditionnel à la levée préalable de ses blocages documentés. Aucun n'a été exécuté. Aucun fournisseur n'est approuvé pour Slice 23.

## 2. Current orchestrator QA capabilities

### Publication roadmap et provenance

Le vrai HEAD lu avec git rev-parse est 0d85a802c873d8a7639070e0a547d90c9264a633, message « Plan QA and regression testing roadmap ». Auteur et committer : yannickameur <yannick.ameur@gmail.com>. Les métadonnées et le message ne contiennent aucun des trailers/attributions interdits.

Après git fetch origin : origin/main est ancêtre de HEAD, ahead/behind = 0/0, aucun commit distant supplémentaire. Le git push origin main autorisé a répondu « Everything up-to-date » : ce commit était déjà publié. HEAD et origin/main sont identiques au hash ci-dessus. Aucun force, rebase, reset ou amend.

Le worktree contenait avant cette étude des modifications de ROADMAP.md et docs/status.md, ainsi que deux rapports suffixés CLAUDE. Ils ont été préservés. Le résumé Claude était visible dans les documents internes requis ; ses rapports n'ont pas été utilisés comme sources ni lus pour la comparaison. Cette recherche ne prétend donc pas être une évaluation en aveugle.

### Sources internes lues avant les recherches externes

Lecture intégrale de [README](../README.md), [ROADMAP](../ROADMAP.md), [status](status.md), [QA_STRATEGY](QA_STRATEGY.md), [ADAPTIVE_EXECUTION](ADAPTIVE_EXECUTION.md), [GIT_GOVERNANCE](GIT_GOVERNANCE.md) et [SPIKE_RALPH](SPIKE_RALPH.md). Lecture du runner et inspection ciblée des huit modules d'intégration demandés ; les déclarations de roadmap sont distinguées du code exécutif.

| Fonction | État établi | Conséquence pour la QA |
| --- | --- | --- |
| Project / MVP / WorkItem, RalphExecutionEngine | CODE_CONFIRMED pour les points d'intégration inspectés ; conception documentée | Garder les mêmes identités et l'exécution gouvernée |
| QualityGateRunner | CODE_CONFIRMED : argv configurés, exécution subprocess, timeout, exit code, stdout/stderr, persistence et SHA capturé | Réutiliser ; ajouter contrat de sélection et preuve de tests, pas recréer le runner |
| AdaptiveExecutionSelector / worker selection | CODE_CONFIRMED : routing, profils, quotas, décisions persistées | Réutiliser pour l'authoring et l'analyse interne ; ne pas créer un second routeur |
| ExecutionStore / HandoffStore / cold restart | DOC_CONFIRMED ; appels et intégration inspectés, pas audit exhaustif de ces stores | Réutiliser les identités/audits ; ajouter le cycle QA, pas prétendre qu'il existe |
| Independent review | CODE_CONFIRMED : reviewer distinct et record lié au SHA | Toute modification de tests invalide la review antérieure |
| Git governance / branches / PR / ff-only | CODE_CONFIRMED : base/head stockés, evidence SHA, eligibility sans LLM, merge ff-only | Étendre le contrat QA et contrôler le ref réel au moment du merge |
| Release gate / ActivityReport / RealizationReport | CODE_CONFIRMED pour les projections et checks inspectés ; ActivityReport documenté | Projeter les événements QA dans le reporting existant |
| QA impact, protection sémantique, final QA, knowledge base | NOT_FOUND comme runtime QA actuel | C'est précisément le différenciateur restant |

Références de code : [validation.py](../src/orchestrator/validation.py), [review.py](../src/orchestrator/review.py), [mvp_manager.py](../src/orchestrator/mvp_manager.py), [git_governance.py](../src/orchestrator/git_governance.py), [realization_report.py](../src/orchestrator/realization_report.py), [release_manager.py](../src/orchestrator/release_manager.py), [worker_selector.py](../src/orchestrator/worker_selector.py), [adaptive_execution.py](../src/orchestrator/adaptive_execution.py).

### Limites que l'architecture doit traiter, sans correction dans cette slice

1. validation.py:192–204 : aucune commande requise implique actuellement passed=True. C'est acceptable pour certains gates techniques, pas une preuve de QA exécutée. Un QA PASS doit exiger un manifest non vide de vérifications obligatoires.
2. validation.py:340–360 : la relecture d'un résultat recalcule passed avec la configuration courante. Pour la QA durable, enregistrer aussi la policy et le manifest appliqués lors du run.
3. QualityGateRunner.run_gate capture le SHA avant exécution, mais ne garantit ni filesystem read-only ni invariance du ref après exécution. Les logs tronqués ne remplacent pas des artefacts complets et des compteurs de tests.
4. git_governance.py:959–1066 : l'eligibility compare review/gates au current_head_sha stocké ; merge utilise ensuite le nom de branche sans comparer explicitement son tip réel à eligibility.head_sha. Une avance concurrente fast-forwardable peut donc sortir du périmètre attesté. C'est un risque déduit de lecture, pas une exploitation exécutée.
5. Le commentaire « pure » de compute_merge_eligibility n'est pas littéralement exact : la méthode lit le store/Git et change des statuts. Elle reste déterministe dans sa logique de décision et n'appelle pas de LLM.
6. Le point de finalisation MVP doit rester commun aux chemins frais, rework et recovery. Le gate release actuel ne constitue pas une seconde preuve QA finale sur le SHA.

## 3. Requirements / invariants

### Vocabulaire de preuve

| Label | Signification |
| --- | --- |
| CODE_CONFIRMED | Comportement visible dans du code primaire inspecté ; ne certifie pas un SaaS fermé |
| DOC_CONFIRMED | Contrat ou comportement explicitement décrit dans une documentation officielle |
| MARKETING_ONLY | Promesse commerciale sans mécanisme suffisamment vérifiable dans les sources examinées |
| NOT_FOUND | Élément recherché mais absent des sources consultées ; ce n'est pas une preuve universelle d'absence |
| UNKNOWN | Information inaccessible, ambiguë, version/plan non déterminé ou contradiction non résolue |

Les classements NATIVE / PARTIAL / BUILD_AROUND / NOT_AVAILABLE évaluent l'adéquation à notre besoin. BUILD_AROUND signifie travail d'intégration à faire, pas fonctionnalité disponible. NOT_AVAILABLE désigne une fonction explicitement hors périmètre du produit étudié. SAFE_WITH_POLICY n'est jamais une certification par défaut.

Aucun test fournisseur n'a été exécuté. Les mécanismes documentés ne prouvent ni efficacité réelle, ni recall, ni absence de faux négatifs. Les scores de couverture issus des pages commerciales restent MARKETING_ONLY.

### Douze invariants et mécanismes de contrôle requis

| # | Invariant | Contrôle d'acceptation |
| --- | --- | --- |
| 1 | PASS fondé sur des tests réellement exécutés | Manifest obligatoire non vide, résultats terminaux, assertions et compteurs, artefacts |
| 2 | Tests de régression existants = actifs protégés | Baseline versionnée incluant tests générés déjà acceptés |
| 3 | Pas d'affaiblissement/suppression/skip opportuniste | Diff de code, fixtures, snapshots, expected values et configuration ; refus sans autorisation |
| 4 | Changement attendu d'un test justifié | Référence à une décision fonctionnelle versionnée approuvée ; le code modifié n'est pas la spécification |
| 5 | PASS lié au HEAD exact | SHA complet et identité du build testé attestés |
| 6 | PASS A n'autorise jamais B | Invalidation à chaque changement ; contrôle du ref sous exclusion concurrente avant merge |
| 7 | FAIL explicable | Test ID, expectation, observation, commande, artefacts et classification justifiée |
| 8 | INCONCLUSIVE n'est pas PASS | État distinct et bloquant pour les obligations |
| 9 | Provider indisponible n'est pas PASS | Incident et attente/retry bornés, jamais substitution par du succès |
| 10 | Pas de fallback silencieux | Engines obligatoires définis dans une policy immuable par run |
| 11 | Final verification read-only | Sources/tests montés read-only ; sorties hors arborescence protégée ; aucun droit Git d'écriture |
| 12 | Connaissance durable avec le repo | Manifest et savoir Git versionnés ; preuves volumineuses référencées par digest |

L'évaluation détaillée par candidat des contrôles 1–12 se trouve en sections 10, 14, 18–22 et 29. Aucun produit, y compris notre futur interne, ne reçoit aujourd'hui un certificat global de conformité.

## 4. Candidate overview

| Candidat | Version / date étudiée | Mode et protocole | Maturité apparente, non benchmarkée |
| --- | --- | --- | --- |
| TestSprite | CLI public 0.11.0 ; commit 125872fd1b19e948528177b6f6a2ac25c83dfd89 du 12/09/2026 ; SaaS sans version publique unique, chemins V2/V3 | CLI Node, HTTPS REST /api/cli/v1, MCP, GitHub App ; exécution SaaS, tunnel local | CLI en évolution rapide ; contrats utiles mais écarts backend explicitement reconnus |
| BrowserStack AI Agents | Documentation vivante au 13/09/2026 ; pas de version globale ; SDKs séparés | Selenium/WebDriver, Playwright/Cypress, Appium, REST, SDK/CI, MCP local ou distant | Cloud mature ; agents et sélection ont des périmètres/bêtas/éditions distincts |
| Momentic | Migration Web 3.0 et Mobile 1.0 documentées ; patch courant non fixé ; API analytics documentée | CLI local, YAML, MCP stdio, HTTPS/API ; inférence et services hébergés | Runtime documenté en détail ; changements récents, recovery/agents avec fonctions bêta |
| Diffblue Cover | Release 2026.08.01 inspectée ; pas affirmation que c'est la dernière disponible | CLI local/CI, Maven/Gradle, artefacts JSON/JUnit/JaCoCo ; license/telemetry réseau selon plan | Spécialiste Java établi ; release avec known issue critique pour notre policy |
| Internal QA | Non implémenté ; socle au SHA roadmap ci-dessus | Adaptive worker + QualityGateRunner + stores/Git existants | Fondations existantes ; assurance QA à construire et mesurer |
| Hybrid / multi-engine | Architectures proposées, pas produits livrés | Même contrat QA + composition explicite de moteurs | Plus de capacités potentielles, plus de qualification et d'exploitation |

Sources : [T1–T9](#sources-testsprite), [B1–B14](#sources-browserstack), [M1–M13](#sources-momentic), [D1–D7](#sources-diffblue). Aucun OTHER_RELEVANT_CANDIDATE ajouté : aucune preuve suffisante justifiant d'élargir le catalogue. Diffblue Agents et Momentic Mo ne sont pas confondus avec les runtimes Cover/Momentic étudiés.

## 5. TestSprite

### Forces vérifiables

Le MCP décrit un chemin diff → résumé du code → plan → génération/exécution. Le CLI public apporte runId, statuts, récupération d'artefacts par run et requêtes idempotentes côté client. L'authoring CLI possède aussi une génération de plan côté serveur : il n'est pas nécessaire de placer obligatoirement un QA LLM interne devant ce produit. Le client ouvert ne rend pas le moteur SaaS ouvert. [T1](https://docs.testsprite.com/mcp/core/tools), [T2](https://docs.testsprite.com/mcp/core/create-tests-new-feature), [T6](https://github.com/TestSprite/testsprite-cli/tree/125872fd1b19e948528177b6f6a2ac25c83dfd89).

### Contradictions qui changent la décision

Le code épinglé contient des avertissements explicites V3 : target URL non appliquée à certains runs frontend, replay ne visant pas nécessairement la dernière version sauvegardée, et demande de désactivation du healing non honorée. Le client avertit sans nécessairement bloquer. Notre adaptateur devrait refuser ces cas ; ajouter simplement --no-auto-heal ne constitue pas une garantie. [T7](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/v3-advisory.ts), [T8](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/runs.types.ts), [T9](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/commands/test.ts).

L'import d'anciens tests est décrit comme plan puis régénération : ce n'est pas une preuve d'exécution inchangée de nos tests Jest/pytest. Le backend CLI accepte du Python requests/pytest-style ; cela ne signifie pas un runner arbitraire du repository. Le MCP GitHub documenté réexécute des tests préparés : il ne prouve pas, à lui seul, une génération autonome par PR. [T3](https://docs.testsprite.com/mcp/maintenance/migration-from-other-platforms), [T4](https://docs.testsprite.com/mcp/integrations/github-integration), [T5](https://docs.testsprite.com/cli/reference/whats-included).

Verdict : PARTIAL pour notre QA globale ; UNSAFE_FOR_FINAL_GATE sur les chemins V3 concernés ; candidat direct externe web/API à requalifier. L'autonomie est réelle dans le contrat disponible, la conformité finale ne l'est pas encore.

## 6. BrowserStack AI Agents

Comparer séparément Automate/App Automate, Low Code, Test Management, Test Reporting & Analytics et Percy. Acheter un de ces produits n'implique pas disposer de toutes les fonctions des autres.

La génération de cas à partir de requirements n'est pas la génération de tests exécutables arbitraires. L'infrastructure supporte des frameworks existants ; le runner CI et les assertions restent souvent chez nous. Smart Test Selection utilise changement/historique et apprentissage ; ce n'est pas une preuve de compréhension sémantique exhaustive. [B1](https://www.browserstack.com/docs/test-management/browserstack-ai/ai-generated-test-cases), [B2](https://www.browserstack.com/docs/automate/selenium/smart-test-selection).

Il existe bien un chemin plus autonome : Low Code Agentic Testing génère, automatise et valide des scénarios dans un vrai navigateur. La documentation le classe Alpha, en essai limité Pro/Ultimate, avec healing fondé sur l'intention. Ce n'est donc pas seulement du test-case management. L'API Low Code permet de déclencher une suite, récupérer build_id puis son statut et ses compteurs. L'API de génération autonome de bout en bout, l'arrêt et la protection sémantique de cette variante restent à qualifier ; ne pas transférer les garanties des scripts Selenium immuables au low-code. [B20](https://www.browserstack.com/docs/low-code-automation/test-recording/browserstack-ai/agentic-testing), [B21](https://www.browserstack.com/docs/low-code-automation/cicd-integrations/run-tests-api).

L'API fournit des identités et des preuves de sessions ; son statut peut également être renseigné par le client. Le normalizer doit lire les résultats du framework, pas prendre un statut libre « passed » pour une preuve indépendante. La guérison de locators Automate est distincte d'une réparation de code par un agent IDE. [B3](https://www.browserstack.com/docs/automate/api-reference/selenium/session), [B4](https://www.browserstack.com/docs/automate/selenium/self-healing).

Verdict : SAFE_WITH_POLICY pour l'exécution de suites standard immuables, healing désactivé, assertion/manifest contrôlés. Meilleur moteur complémentaire browser/mobile ; PARTIAL comme moteur QA général autonome. Le cloud de devices ne remplace pas notre connaissance des invariants métier ni notre gate.

## 7. Momentic

Il faut corriger deux raccourcis : les tests peuvent vivre dans le repo et le produit documente maintenant une analyse de diff. La sélection AI accepte une base et fournit une sortie JSON ; l'index de code sait suivre les imports JS/TS. Il ne faut pas extrapoler ce graphe aux langages non pris en charge. [M1](https://momentic.ai/docs/ai/select).

Le runtime CLI exécute les YAML locaux. Les outils MCP peuvent author/run/triage ; un runner de step asynchrone expose un identifiant de polling. L'API analytics publie un gitCommitSha exact, mais seulement les runs terminés : elle ne devient pas pour autant une API générale trigger/status des jobs en cours. [M2](https://momentic.ai/docs/cli-reference/momentic/commands/run), [M3](https://momentic.ai/docs/coding-agents/mcp-server), [M4](https://momentic.ai/docs/api-reference/overview), [M5](https://momentic.ai/docs/api-reference/analytics/list-runs).

Les décisions de classification, recovery et quarantine peuvent influer sur le succès opérationnel. Une assertion LLM exécutée n'est pas équivalente à un oracle métier déterministe. Les vérifications critiques doivent conserver des attentes explicites. [M6](https://momentic.ai/docs/configuration/ai), [M7](https://momentic.ai/docs/reliability/auto-maintenance).

Verdict : candidat sérieux repo-centric web ; PARTIAL globalement, SAFE_WITH_POLICY envisageable sur un sous-ensemble verrouillé et testé. Pas une preuve de conformité par défaut, ni un remplacement natif de pytest. Mobile = simulateurs/émulateurs, pas appareils physiques.

## 8. Diffblue Cover

Cover génère et valide des tests Java/Kotlin compilés, dans l'écosystème JUnit/TestNG et Maven/Gradle. Il découvre des comportements du code ; il ne détermine pas si un comportement actuel est la bonne spécification. Créer un test qui attend 80 à partir d'un programme incorrect retournant 80 ne prouve pas l'absence de régression face à un invariant versionné de 100. [D1](https://cover-docs.diffblue.com/get-started/get-started/get-started-cover-cli).

La portée --patch-only vise les classes modifiées et dépendantes. Les options de mocking, Spring integration et rapports aident l'authoring. L'ancienne --validation-command est dépréciée et concerne Maven/Gradle, non un exécuteur universel. Une reprise par module existe ; ce n'est pas la restauration arbitraire d'un calcul interrompu. [D2](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments).

Le mode merge est optionnel. La release étudiée signale TG-24166, suppression possible de tests existants : pas de --merge sur des actifs protégés pour notre qualification. Même les fichiers générés séparément peuvent être remplacés lors d'une régénération ; une baseline générée acceptée devient protégée. [D3](https://cover-docs.diffblue.com/features/cover-cli/writing-tests/merge-mode), [D4](https://cover-docs.diffblue.com/updates-and-upgrades/release-archive/2026-08-01).

Verdict : ADOPT_DIFFBLUE_FOR_JAVA comme option spécialisée future, pas décision primaire du projet. SAFE_WITH_POLICY pour authoring en sortie séparée contrôlée ; UNSAFE_FOR_FINAL_GATE si la génération/merge-mode s'exécute pendant le final. La vérification finale des JUnit appartient au runner déterministe.

## 9. Internal QA

Le candidat est un moteur encore à construire, non une fonctionnalité déployée. Il réutilise le routing pour une tâche QA distincte, les exécutions gouvernées pour l'authoring, QualityGateRunner pour les commandes, les stores pour les liens d'audit et GitGovernance pour les SHA.

Sa valeur propre serait : choisir/expliquer le périmètre, préserver les régressions et réunir des preuves. Le LLM propose des tests et classe des échecs ; il n'accorde pas un PASS en l'absence de tests. Une entrée sans changement ou ne nécessitant pas d'authoring peut être traitée sans appel LLM QA.

| Lot | Effort relatif | Réutilisation / travail réellement nouveau |
| --- | --- | --- |
| A. Impact analysis | MODERATE ; EXPENSIVE pour graphe multilangage précis | Diff Git simple peu coûteux ; mapping tests/comportements et dépendances transitives nouveaux |
| B. Prompt/agent | CHEAP_TO_BUILD pour prototype, MODERATE à fiabiliser | Routing/profils existants ; séparation requirements/code non fiable et budget à préciser |
| C. Test generation | MODERATE pour pytest ciblé ; EXPENSIVE toutes stacks | Coding workers réutilisés ; fiabilité de l'oracle et fixtures à démontrer |
| D. Command runner | CHEAP_TO_BUILD pour raccordement ; MODERATE pour durcissement | Réutiliser QGR ; manifests, artefacts, isolation, arbres de processus à compléter |
| E. Failure classification | MODERATE | Taxonomie stable, preuve structurée, incertitude ; pas de LLM override du verdict |
| F. Persistence | MODERATE | Étendre identité/store/audit ; journal de lancement et statut QA spécifiques |
| G. Knowledge base | MODERATE | Schémas YAML, provenance, migration, lint et propositions de mise à jour |
| H. Review/rework integration | MODERATE à EXPENSIVE | Tous les chemins fresh/wait/recovery et invalidation SHA |
| I. E2E/browser simple | MODERATE ; EXPENSIVE pour robustesse large | Adopter frameworks existants ; ne pas écrire un moteur browser |
| J. Visual/mobile | SPECIALIZED | Adopter infrastructure spécialisée si besoin ; device cloud interne non recommandé |

Ce n'est pas un chiffrage en jours : absence de mesures de volume, de benchmark de génération et d'exigences device. Coût dominant de long terme : faux négatifs et entretien, pas le prompt initial. « Interne » ne signifie pas « offline » : les workers Claude/Codex peuvent envoyer du contexte à leurs fournisseurs.

## 10. Capability matrix

### Vue synthétique — capacités, pas verdicts de certification

| Capacité | INTERNAL prévu | TESTSPRITE | BROWSERSTACK | MOMENTIC | DIFFBLUE |
| --- | --- | --- | --- | --- | --- |
| Impact base/head complet | BUILD_AROUND | PARTIAL | PARTIAL | PARTIAL, natif JS/TS ciblé | PARTIAL, natif patch Java |
| Génération de tests du repo | BUILD_AROUND via workers | PARTIAL, import/régénération | PARTIAL selon agent/produit | PARTIAL, format YAML | NATIVE Java |
| Exécuter suites arbitraires | NATIVE QGR configuré, QA autour à faire | NOT_FOUND | BUILD_AROUND via CI | NOT_AVAILABLE comme runtime général | NOT_AVAILABLE hors JVM |
| Tests réellement exécutés | CODE_CONFIRMED QGR | DOC_CONFIRMED ; client CODE_CONFIRMED | DOC_CONFIRMED | DOC_CONFIRMED | DOC_CONFIRMED |
| Protection baseline | BUILD_AROUND | PARTIAL ; final V3 bloqué | SAFE_WITH_POLICY | SAFE_WITH_POLICY à valider | SAFE_WITH_POLICY hors génération finale |
| SHA exact + déploiement attesté | BUILD_AROUND sur socle SHA | BUILD_AROUND | BUILD_AROUND | PARTIAL metadata ; attestation autour | BUILD_AROUND |
| Final read-only garanti | BUILD_AROUND | UNKNOWN / incompatible V3 cité | BUILD_AROUND | BUILD_AROUND | Tests seuls via runner ; pas create |
| Reprise durable job QA | BUILD_AROUND | PARTIAL, run distant pollable | PARTIAL, sessions + CI | PARTIAL, local + analytics terminées | PARTIAL, reprise module |
| Connaissance .qa native | BUILD_AROUND | NOT_FOUND | NOT_FOUND | NOT_FOUND pour ce format | NOT_FOUND |

HYBRID n'hérite pas automatiquement de la meilleure cellule : il exige que chaque obligation soit satisfaite par son moteur assigné. MULTI_ENGINE_BY_STACK doit maintenir une qualification et un adaptateur par configuration, pas un seul booléen « supporté ».

### Couverture des stacks

Légende : « cible HTTP/UI » signifie langage de l'application indifférent, pas génération de tests unitaires de ce langage.

| Stack | Interne proposé | TestSprite | BrowserStack | Momentic | Diffblue Cover |
| --- | --- | --- | --- | --- | --- |
| Python | Première cible, pytest existant | API Python de test / cible HTTP ; pas suite pytest repo démontrée | SDK et browser depuis Python ; pytest local peut piloter | Cible HTTP/UI ; pas unité Python native | NOT_AVAILABLE |
| TypeScript / JavaScript | Jest/Vitest/Playwright configurables | Web/API ; pas runner Jest inchangé démontré | Playwright/Cypress/Selenium ; runners en CI | YAML web, code index JS/TS | NOT_AVAILABLE |
| Java | Maven/Gradle via QGR ; authoring à qualifier | Cible HTTP | Clients Java/Appium/Selenium | Cible HTTP/UI | NATIVE Java et support Kotlin |
| .NET | Commandes configurables, génération non qualifiée | Cible HTTP/UI | Selenium .NET ; pas unit generator général démontré | Cible HTTP/UI | NOT_AVAILABLE |
| Go | Commandes configurables, génération non qualifiée | Cible HTTP | Cible web/API ; client selon framework, pas unité Go | Cible HTTP/UI | NOT_AVAILABLE |
| Rust | Commandes configurables, génération non qualifiée | Cible HTTP | Cible UI via framework supporté ; pas unité Rust | Cible HTTP/UI | NOT_AVAILABLE |
| Mobile | BUILD_AROUND, pas de ferme interne | Mobile natif NOT_FOUND | Appareils réels, Appium et produits App | iOS simulator / Android emulator | NOT_AVAILABLE |
| Web frontend | BUILD_AROUND sur framework | Domaine principal | Domaine principal, matrice large | Domaine principal, Chromium | NOT_AVAILABLE |
| API/backend | Domaine principal initial | Domaine principal HTTP | PARTIAL selon produit ; browser cloud ≠ API runner universel | PARTIAL via flows/steps API | Java unités et Spring ciblé |

Bases documentaires : outils/CLI TestSprite [T1,T5], BrowserStack self-hosted et MCP [B8,B12], Momentic MCP et selection [M1,M3], Cover CLI [D1,D2]. Aucune supposition de qualité égale entre toutes les stacks.

### Web / mobile / visual / accessibility — comparaison explicite

| Dimension | TestSprite | BrowserStack | Momentic | Interne / Diffblue |
| --- | --- | --- | --- | --- |
| Browser réellement exécuté | DOC_CONFIRMED, SaaS | DOC_CONFIRMED, Automate/Low Code | DOC_CONFIRMED, Chromium | Interne BUILD_AROUND framework ; Cover NOT_AVAILABLE |
| Matrice navigateurs | Promesse cross-browser ; matrice/version précise UNKNOWN pour le contrat étudié | DOC_CONFIRMED, versions/OS selon produit ; pas tous les frameworks sur tout device | Chromium documenté ; matrice Firefox/Safari NOT_FOUND | Interne à intégrer ; Cover NOT_AVAILABLE |
| Mobile natif / device cloud | NOT_FOUND dans les contrats qualifiés | DOC_CONFIRMED appareils réels et produits App | DOC_CONFIRMED iOS simulator / Android emulator ; pas appareils physiques | Interne SPECIALIZED ; Cover NOT_AVAILABLE |
| Visual regression | MARKETING_ONLY pour comparateur/baseline détaillés dans les sources trouvées ; captures documentées | DOC_CONFIRMED Percy, baseline/approval distincts du fonctionnel | DOC_CONFIRMED visualDiff et golden files ; assertions AI visuelles distinctes | Interne comparateur à adopter ; Cover NOT_AVAILABLE |
| Accessibilité | MARKETING_ONLY dans page use-case ; contrat vérifiable NOT_FOUND | DOC_CONFIRMED produit Accessibility intégré aux suites supportées | Moteur d'audit d'accessibilité dédié NOT_FOUND ; lire accessibility tree ne suffit pas | Interne BUILD_AROUND outils standards ; Cover NOT_AVAILABLE |
| Screenshots / vidéo | DOC_CONFIRMED selon run | DOC_CONFIRMED selon produit/config | DOC_CONFIRMED, vidéo selon configuration/version | Interne selon framework adopté ; Cover logs JVM seulement |
| Network logs / traces | Logs et steps oui ; contrat réseau complet UNKNOWN | DOC_CONFIRMED, collecte/config selon session | DOC_CONFIRMED traces DOM/network/console selon run | Interne framework à intégrer ; Cover pas traces browser |

Sources complémentaires consultées le 14/09 : [B19](https://www.browserstack.com/docs/accessibility/automated-tests/get-started), [M17](https://momentic.ai/docs/guides/visual-testing/golden-files), [T18](https://www.testsprite.com/use-cases/en/ai-visual-testing-tool), [T19](https://www.testsprite.com/use-cases/en/ai-cross-browser-testing-tool). Preuves d'exécution/artifacts : [B3,B9,B10,M3,M9,T1,T5]. HYBRID et MULTI_ENGINE_BY_STACK peuvent déléguer ces domaines au spécialiste admis, pas annoncer cette couverture avant intégration.

Attention aux baselines visuelles : Momentic documente qu'un premier golden mobile est créé avec succès et que l'option d'update fait passer un snapshot différent. Notre final doit donc exiger une baseline déjà approuvée, présente et scellée ; ni première génération ni mise à jour automatique pendant la vérification.

## 11. Test impact analysis

| Entrée / sortie | TestSprite | BrowserStack | Momentic | Diffblue | Interne / hybride |
| --- | --- | --- | --- | --- | --- |
| base_sha + head_sha explicites | NOT_FOUND dans bootstrap ; diff récent documenté | Refs/metadata, paire immuable à construire | Base configurable et checkout Git ; paire immuable à sceller | Patch fourni, paire scellée par appelant | BUILD_AROUND Git exact |
| Fichiers changés | DOC_CONFIRMED | DOC_CONFIRMED metadata | DOC_CONFIRMED | DOC_CONFIRMED patch | Simple diff déterministe |
| Symboles/modules/dépendances | PARTIAL résumé | PARTIAL historique/path | PARTIAL imports JS/TS et graphe applicatif | PARTIAL classes liées | MODERATE ; graphe exhaustif non acquis |
| Comportements concernés | PARTIAL, synthèse générée | NOT_FOUND comme contrat complet | PARTIAL sélection sémantique | PARTIAL comportement du bytecode | Raisonnement fondé sur specs et .qa à construire |
| Tests existants associés | PARTIAL plans/imports | PARTIAL apprentissage | NATIVE sélection des YAML indexés | PARTIAL couverture et classes | Mapping versionné à construire |
| Tests manquants | DOC_CONFIRMED proposition de plan | PARTIAL test-case generator séparé | PARTIAL authoring distinct de selection | PARTIAL couverture incrémentale | Proposition LLM + contrôle humain/review |
| Regression scope auditable | BUILD_AROUND | BUILD_AROUND | JSON utile ; contrôle obligatoire autour | Patch et reports ; contrôle autour | Manifest explicite commun |

Aucun candidat ne démontre l'intégralité de cette sortie pour toutes les stacks. Les sources qui permettent cette conclusion sont [T2](https://docs.testsprite.com/mcp/core/create-tests-new-feature), [B2](https://www.browserstack.com/docs/automate/selenium/smart-test-selection), [M1](https://momentic.ai/docs/ai/select), [D2](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments).

Chez Momentic, un changement de module partagé n'oblige pas nécessairement tous ses consommateurs à être sélectionnés. Chez BrowserStack, relevantOnly peut écarter des tests et la sélection n'a pas de liste must-run native documentée sur la page étudiée. Pour la première qualification : sélection en mode observation, suite obligatoire exécutée au complet. Les économies de temps restent à mesurer.

## 12. Test generation

| Type | TestSprite | BrowserStack AI | Momentic | Diffblue | Interne proposé |
| --- | --- | --- | --- | --- | --- |
| Unit tests | Pas généraliste natif démontré | Cas textuels ≠ unités exécutables | Hors runtime natif | Java/JUnit/TestNG | Via worker ; pytest en premier |
| Integration | HTTP et scénarios applicatifs | Selon framework/agent, à intégrer | Flows applicatifs | Spring ciblé, pas toute intégration | Via framework existant |
| API | DOC_CONFIRMED | PARTIAL, distinguer produit API et cloud browser | PARTIAL, étapes/flows ; pas runner backend général | Pas API black-box général | HTTP fixtures/outils existants |
| Contract | Plans schema/contract documentés ; qualité non mesurée | NOT_FOUND comme générateur dédié ici | NOT_FOUND comme générateur dédié ici | NOT_FOUND comme générateur dédié | BUILD_AROUND, ne pas inventer contrats |
| Browser E2E | DOC_CONFIRMED | DOC_CONFIRMED selon produit/agent | DOC_CONFIRMED YAML/MCP | NOT_AVAILABLE | Framework standard à adopter |
| Mobile | NOT_FOUND natif | DOC_CONFIRMED, produits App | DOC_CONFIRMED simulé | NOT_AVAILABLE | SPECIALIZED |
| Visual regression | Captures oui ; comparaison/baseline forte à qualifier | Percy, produit distinct | Golden files / assertions visuelles | NOT_AVAILABLE | Adopter un comparateur, pas le reconstruire |
| Fixtures / mocks | PARTIAL données/auth/API | PARTIAL via code/framework | PARTIAL données/environnement | DOC_CONFIRMED mocking | Via worker et conventions repo |
| Property / boundary | Cas boundary revendiqués ; property-based engine NOT_FOUND | NOT_FOUND natif transversal | NOT_FOUND property-based général | Exploration branches/valeurs, pas garantie property-based | Hypothèses/oracles explicites ; génération à qualifier |
| Regression reproduction | Possible à partir bug/requirements ; pas recall prouvé | Possible selon scénario/outillage | Possible scénario déterminé | Peut figer comportement actuel, pas inférer intention | Test qui échoue au bug et passe au correctif exigé |

Sources : [T1,T2,T5], [B1,B9,B10], [M3,M7,M8], [D1,D2,D5]. Les cellules « possible » sont des inférences d'architecture, pas des résultats expérimentaux. Un plan de tests, un test compilant et un test révélant le bon défaut sont trois livrables différents.

## 13. Test execution

| Moteur | Générés | Existants inchangés | Suites/commandes/custom CI |
| --- | --- | --- | --- |
| Interne | QGR peut lancer leurs commandes | Oui si la commande les inclut ; sélection/protection QA à développer | argv arbitraires configurés ; environnements/dépendances restent à fournir |
| TestSprite | Réellement exécutés selon docs | TestSprite sauvegardés oui ; import d'autres frameworks par régénération, non équivalence prouvée | Backend Python contrôlé et runs propres ; pytest/Jest/Vitest/Maven arbitraires NOT_FOUND |
| BrowserStack | Réellement exécutés si l'agent produit un scénario exécutable supporté | Oui pour suites browser/device compatibles ; intégration SDK/framework nécessaire | CI lance pytest/Jest/Maven etc. ; le service prend les sessions browser/device, pas tous les processus |
| Momentic | YAML exécutés via CLI | YAML existants oui ; Cypress/Playwright repo sans conversion non | Commande --start démarre l'app, ne transforme pas Momentic en gate universel |
| Diffblue | Génération avec validation réelle | Suite JVM validable via build ; génération n'autorise pas l'écrasement des actifs | Maven/Gradle ; dcover validate distinct de create ; aucun support pytest/Jest |

Le runner peut exécuter une commande qui ne collecte aucun test, ou un client peut marquer une session réussie : la QA doit vérifier la sémantique des compteurs et du manifest. Réutiliser QualityGateRunner ne dispense pas de cette couche. [Code validation](../src/orchestrator/validation.py), [T5](https://docs.testsprite.com/cli/reference/whats-included), [B3](https://www.browserstack.com/docs/automate/api-reference/selenium/session), [M2](https://momentic.ai/docs/cli-reference/momentic/commands/run), [D2](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments).

## 14. Existing-test protection

| Candidat | Ce qui peut changer | Contrôle envisagé | Classement |
| --- | --- | --- | --- |
| Internal | Un worker peut changer tout fichier accessible, y compris assertions/config | Allowlist d'authoring, baseline hash, revue des diff ; sandbox finale | SAFE_WITH_POLICY proposé, absent aujourd'hui |
| TestSprite | Plans/code via API, régénération/import, healing ; frontière sémantique publique insuffisante | Refuser healing non désactivable ; protéger versions exportées ; ne pas remplacer la suite locale | PARTIAL ; UNSAFE_FOR_FINAL_GATE pour V3 concerné |
| BrowserStack | Locators runtime ; agents IDE peuvent proposer/appliquer du code ; Percy baseline approuvable | selfHeal désactivé ; pas d'outil de réparation final ; pas d'auto-approval de baseline | SAFE_WITH_POLICY pour suites standard, PARTIAL low-code global |
| Momentic | YAML/modules par triage, caches de steps, golden files ; recovery non persistante possible | Aucun triage, update-golden, cache persistant, override verdict ou quarantine ignorée | SAFE_WITH_POLICY à qualifier ; default insuffisant |
| Diffblue | Fichiers générés ; mode merge peut toucher classes existantes ; bug publié | Sortie neuve contrôlée, pas merge ; aucun create final | SAFE_WITH_POLICY phase 1 ; génération finale UNSAFE_FOR_FINAL_GATE |
| Hybrid | Union des surfaces d'écriture | Même baseline et même policy imposées à tous ; aucune auto-importation des réparations | SAFE_WITH_POLICY seulement si chaque adaptateur conforme |

Sources des mécanismes : [T3,T8,T9], [B4,B9], [M6,M7,M8], [D3,D4,D5]. Les politiques proposées ne sont pas des fonctionnalités déjà présentes dans l'orchestrateur.

Une analyse de diff peut détecter une modification, pas prouver automatiquement qu'elle préserve la sémantique. Toute modification d'un test accepté exige une justification versionnée indépendante du simple désir de rendre le build vert. Étendre la protection aux hooks, fixtures, snapshots, mocks, filtres, marqueurs, seuils et fichiers de configuration ; sinon supprimer une assertion n'est pas nécessaire pour neutraliser un test.

## 15. Self-healing

TECHNICAL_SELF_HEALING : changement de moyen pour atteindre le même élément fonctionnel, sans changer l'oracle. Même ici, un locator approximatif peut viser le mauvais élément ; le changement doit être observable.

SEMANTIC_SELF_HEALING : changement de l'attendu, suppression d'une assertion, acceptation d'un nouveau snapshot ou modification du chemin au point de ne plus vérifier le même invariant. Interdit sans décision fonctionnelle versionnée.

| Produit | Distinction praticable | Risque à traiter |
| --- | --- | --- |
| TestSprite | Auto-heal documenté ; frontière exacte des changements UNKNOWN | Flag non honoré dans les chemins V3 ; un run « healed passed » n'est pas recevable comme final |
| BrowserStack | Automate documente surtout les locators ; autres agents plus larges | Ne pas assimiler code repair ou Percy approval à un simple locator heal |
| Momentic | Locator, temporary recovery, réparation persistante et classification sont séparés | Une récupération non persistante peut tout de même contourner une étape métier |
| Diffblue | Pas besoin du terme healing : génération/maintenance peut remplacer les attentes | Figer le comportement erroné comme nouvelle baseline ; known issue merge |
| Interne/hybride | Séparation par contrat à construire | Le prompt « ne change pas les tests » ne constitue pas une barrière |

Références : [T10](https://docs.testsprite.com/cli/core/rerun-and-auto-heal), [B4](https://www.browserstack.com/docs/automate/selenium/self-healing), [M7](https://momentic.ai/docs/reliability/auto-maintenance), [D3](https://cover-docs.diffblue.com/features/cover-cli/writing-tests/merge-mode).

## 16. Failure analysis

Taxonomie commune proposée : REGRESSION, EXPECTED_CHANGE, TEST_DEFECT, FLAKY_TEST, ENVIRONMENT_FAILURE, UNKNOWN. Une classification est accompagnée d'evidence_refs et d'une justification ; elle ne change pas seule le résultat du test.

| Candidat | Sortie / preuves | Normalisation des six classes |
| --- | --- | --- |
| TestSprite | run/test/steps, logs, captures, bundle failure, propositions | MEDIUM : certaines catégories fournisseur ; mapping complet à construire |
| BrowserStack | Session/build IDs, assertions framework, screenshots, vidéo et logs selon produit/config | MEDIUM : analytics/root-cause aide ; résultat client à corroborer |
| Momentic | Résultats locaux structurés, traces, historique/recovery, classification | MEDIUM : distinguer failure brute et réinterprétation ; ne pas activer override |
| Diffblue | Codes d'environnement/génération, report JSON, résultats JVM et coverage | MEDIUM pour adapter ; HIGH pour classifier automatiquement l'intention métier |
| Interne | Exit/logs QGR actuels ; ajout artefacts et classif par worker | MEDIUM ; richesse future, pas preuve déjà disponible |

Un FAIL déterministe reproductible reste FAIL tant que le code/test n'est pas corrigé et revérifié. EXPECTED_CHANGE exige une décision approuvée, jamais une déduction depuis le seul output. Un crash d'environnement ou preuve manquante peut produire INCONCLUSIVE plutôt que REGRESSION ; les deux bloquent une obligation.

Pour reproduire : même SHA, test hash, outil/version, image/dependencies, seed, clock/data fixtures, commande, environnement et identités de déploiement. Vidéo et rapport narratif seuls ne suffisent pas. Sources : [T5,T11], [B3,B5,B6], [M6,M9], [D1,D2].

## 17. Flakiness

| Candidat | Détection / rerun / historique | Quarantine et faux succès |
| --- | --- | --- |
| TestSprite | CLI repeat/flaky et comparaison de runs documentés | Ne pas transformer dernier rerun vert en effacement des échecs antérieurs |
| BrowserStack | Analytics et tentatives, triage historique | Sélection/muting peuvent cacher des tests ; conserver toutes les tentatives et obligations |
| Momentic | Retries, flake history, quarantine et agents | Quarantined failures n'affectent pas l'exit code par défaut ; --ignore-quarantine requis pour notre final |
| Diffblue | Validation des tests générés, filtres de qualité | Ce n'est pas un service d'historique flaky multi-stack ; tests partiels/nondéterministes non recevables |
| Interne/hybride | Historique et retry policy à ajouter aux stores | Ne pas construire « rerun until green » ; borne et cause documentées |

Sources : [T5](https://docs.testsprite.com/cli/reference/whats-included), [B5](https://www.browserstack.com/docs/test-reporting-and-analytics/features/insights), [M2](https://momentic.ai/docs/cli-reference/momentic/commands/run), [M10](https://momentic.ai/docs/quarantine), [D2](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments).

Policy proposée : known flaky != ignored. Un test obligatoire connu flaky reste exécuté et visible. Une série contradictoire devient INCONCLUSIVE sauf règle explicite préalablement approuvée, versionnée et limitée. Pas de masquage automatique d'une obligation. La durée et l'historique aident à diagnostiquer, pas à autoriser un merge.

## 18. Git / SHA / audit

| Moteur | Repository / branch | base/head/diff | Résultat SHA exact |
| --- | --- | --- | --- |
| TestSprite | Projet local MCP, GitHub/preview | Diff récent ; paire immuable NOT_FOUND dans contrat étudié | runId et codeVersion ; codeVersion est le code de TEST, pas HEAD du repo |
| BrowserStack | Metadata SDK/CI, Git integration possible | Branches/changements pour sélection | Commit metadata documentée ; à attester et rapprocher de la session et du déploiement |
| Momentic | Repo local et metadata GitHub/GitLab | Base configurable, diff et YAML Git | gitCommitSha et filtre exact documentés ; déploiement/dirty state restent à vérifier |
| Diffblue | Checkout de l'appelant | Patch explicite | Contexte Git possible dans reports ; wrapper doit certifier SHA et test outputs |
| Interne | Gouvernance existante | base/head existants | Liaison code existante, fenêtre de drift à fermer |
| Hybrid/multi | Tous les IDs locaux et externes | Même QARequest immuable | Aucun vote majoritaire ; chaque preuve requise doit viser le même HEAD |

Sources : [T8](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/runs.types.ts), [B5](https://www.browserstack.com/docs/test-reporting-and-analytics/features/insights), [M5](https://momentic.ai/docs/api-reference/analytics/list-runs), [D2](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments), [Git code](../src/orchestrator/git_governance.py).

Un champ SHA renseigné par l'appelant est nécessaire mais insuffisant : la preview peut pointer sur une autre révision. Exiger build provenance, digest de l'image/application, attestation de déploiement ou endpoint de version vérifiable. Rejeter « latest branch », URL seulement, SHA tronqué et résultats obtenus pour une autre version des tests.

## 19. Read-only verification

La propriété recherchée concerne production, tests, configuration et Git, pas l'absence de fichiers de logs. Un runner doit pouvoir écrire ses résultats, caches temporaires ou fichiers de build dans une zone externe isolée.

| Moteur | Configuration finale envisagée | Garantie aujourd'hui |
| --- | --- | --- |
| Interne | Checkout exact read-only, .git sans écriture, outputs externes, réseau/outils bornés, contrôle du ref | BUILD_AROUND ; QGR seul ne fournit pas cette sandbox |
| TestSprite | Réexécution d'un run/test versionné sans healing et bonne target attestée | Non garantie V3 ; bloquant jusqu'à preuve corrigée |
| BrowserStack | Scripts repo immuables, healing et repair désactivés ; sessions cloud sur build attesté | Réalisable avec policy/CI ; pas certification native globale |
| Momentic | Aucun triage/fix, failureRecovery=false, overrideExitCode=false, quarantine prise en compte ; pas golden update ; cache non persistant | Réalisable sous réserves ; directives et isolation à qualifier |
| Diffblue | Maven/Gradle exécutent les tests acceptés ; pas dcover create/merge/clean final | Réalisable via runner existant, pas via génération |
| Hybrid | Chaque composant reste dans le même périmètre de droits | Le plus faible contrôle obligatoire bloque le PASS |

Pour Momentic, --disable-cache supprime l'usage et l'écriture de caches mais réintroduit de l'inférence dynamique ; ce n'est pas une preuve de déterminisme sémantique. Désactiver aussi mémoire/recovery lorsque non scellées et employer des assertions explicites pour les invariants. Les snapshot replays documentés peuvent améliorer l'isolation, à qualifier sans supposer qu'ils remplacent un test du déploiement courant. [M2](https://momentic.ai/docs/cli-reference/momentic/commands/run), [M6](https://momentic.ai/docs/configuration/ai).

## 20. Autonomous / headless integration

| Produit | API / CLI / MCP / CI / webhook | Trigger / poll / timeout / cancel / retry / artifacts |
| --- | --- | --- |
| TestSprite | CLI JSON + MCP + GitHub ; REST visible dans client ouvert | Trigger et runId ; wait/status ; timeout client ≠ fin serveur ; cancel ; idempotency/retry côté client ; artifact get(runId) |
| BrowserStack | SDK/framework CI + REST ; MCP local clé, distant OAuth ; produits séparés | Sessions créées par framework, listes/status REST ; timeout et fin de session ; retry CI ; logs/video. Suppression d'un record API ≠ cancellation |
| Momentic | CLI web/mobile, MCP stdio ; analytics GET ; CI GitHub/GitLab documenté | CLI local et progress.json ; timeout flush ; stepRunnerId MCP pollable ; process/session termination. API analytics ne pilote pas un job en cours |
| Diffblue | CLI batch/CI, Maven/Gradle ; reports | Process local ; timeout build/CLI à encadrer ; signal/kill par superviseur ; reprise module, report files. API SaaS job général NOT_FOUND |
| Interne | Exécution et workers existants | Lifecycle QA à raccorder ; ne pas supposer qu'un subprocess survit à un restart |

Sources : [T11](https://docs.testsprite.com/cli/core/running-tests), [T12](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/http.ts), [B3,B7,B8], [M3,M4,M9], [D2]. BrowserStack Low Code ajoute un trigger REST de suite et un polling de build sans driver local à préserver ; cancellation, version immuable de suite et génération par API ne sont pas établies par cette seule page [B21](https://www.browserstack.com/docs/low-code-automation/cicd-integrations/run-tests-api).

SDK dédié QA autonome ou webhook universel : NOT_FOUND lorsque non explicitement décrit ci-dessus. La présence de MCP n'est ni nécessaire ni suffisante pour l'autonomie ; un orchestrateur déterministe peut appeler un CLI sans boucle de chat. Aucune de ces interfaces n'a été appelée sur un compte dans cette étude.

## 21. Persistence / recovery et output structuré

| Incident | TestSprite | BrowserStack | Momentic | Diffblue / interne |
| --- | --- | --- | --- | --- |
| Orchestrateur redémarre, moteur distant continue | Persist runId puis poll ; possible selon contrat | Reprendre status/artefacts via session/build + CI job ; pas reconstituer automatiquement le driver | CLI survivant à superviser ; analytics seulement après upload/fin | Process survivant à identifier ; pas supposer reprise magique |
| Réseau interrompu | INCONCLUSIVE/WAIT jusqu'au terminal ; idempotency évite doublons dans limite du contrat | Vérifier session/CI ; ne pas lancer doublon non corrélé | Fichiers locaux utiles ; cloud inference peut échouer ; upload ultérieur possible | Cover peut nécessiter licence réseau ; mode offline selon plan |
| Process local mort | Tunnel possédé peut être annulé ; nouveau run si nécessaire | Driver perdu : nouvelle tentative généralement nécessaire | progress.json ≠ checkpoint d'exécution ; nouvelle tentative si processus perdu | Cover reprise par module ; QGR état partiel ≠ run intégral terminé |
| Artifacts expirés | Retention exacte UNKNOWN ; exporter tôt | Rétentions par produit ; archiver la preuve utilisée | Résultats self-service 30 jours selon pricing ; exporter | Conserver outputs locaux sous IDs immuables |

Sources : [T11,T13], [B3,B11], [M4,M9,M12], [D2,D6,D7]. Tout nouveau lancement après perte est une nouvelle tentative, non la réécriture d'un ancien succès. Une annulation reste distincte d'un échec fonctionnel.

### Contrats conceptuels à figer en Slice 22 — aucune classe créée ici

QARequest :

- Identités : request_id, project_id, mvp_id, work_item_id, phase, attempt_id.
- Révision : repository identity, branch, base_sha, head_sha complets, diff_digest.
- Autorité : policy_version/digest, engine requirements, permissions, timeout, retry budget, data policy.
- Oracles : requirements_refs et approval_refs, protected_tests_manifest/digest, knowledge_snapshot/digest.
- Exécution : tests manifest, commands/gate references, environment/build/deployment attestations.
- Authoring : zones d'ajout permises ; mutation de tests acceptés seulement avec justification approuvée.

QAResult contient tous les champs demandés :

| Champ | Règle de normalisation |
| --- | --- |
| verdict | PASS / FAIL / INCONCLUSIVE ; calcul de policy, jamais copie aveugle d'un statut marketing |
| change_scope, risks | Faits du diff + hypothèses explicitement séparées |
| tests_selected, tests_added, tests_executed | IDs stables, fichiers/version/digests ; ajout n'implique pas exécution |
| passed, failed, skipped | Compteurs et identités ; inconnus = null/UNKNOWN, jamais 0 inventé |
| failure_classifications, regressions | Classes communes avec evidence_refs et degré d'incertitude |
| coverage_gaps | Ce qui n'a pas été vérifié, pas un pourcentage inventé |
| recommended_actions, requires_coding_agent | Recommandations ; aucune autorité automatique pour modifier les tests |
| artifacts | Références exportées, taille/type/digest, rétention et accès |
| external_run_id | ID fournisseur nullable ; conserver aussi engine/version/project/test-code version |

Ajouter obligatoirement request_id, head_sha, policy_digest, manifest_digest, observed_deployment, timestamps, raw_result_ref, attempts, completion_reason et read_only_attestation. Les listes non supportées ne sont pas fabriquées à partir de prose.

| Adapter | Difficulté de normalisation |
| --- | --- |
| INTERNAL | MEDIUM : commandes existantes, mais compteurs/parsers et QAResult nouveaux |
| TESTSPRITE | MEDIUM pour shape ; HIGH pour combler les garanties target/healing |
| BROWSERSTACK | MEDIUM pour une famille de sessions ; HIGH pour agréger plusieurs produits |
| MOMENTIC | MEDIUM : JSON/JUnit + Git metadata, réconcilier quarantine/recovery |
| DIFFBLUE | MEDIUM : report de génération + résultats JVM distincts |
| HYBRID / MULTI_ENGINE_BY_STACK | HIGH pour composition, obligations, conflits et recovery multi-provider |

QAEngine conceptuel : describe_capabilities, prepare/author (phase 1), start_verification, get_status, cancel, fetch_artifacts, normalize_result. Un adaptateur synchrone peut représenter son process local comme job ; il doit déclarer explicitement les méthodes non supportées. Le contrat n'impose ni MCP ni LLM interne.

Journaliser l'intention avant le lancement, puis run_id dès réception, statut et artefacts avant le verdict final. Si le crash se produit entre lancement externe et persistance, utiliser idempotency/correlation lorsqu'elles sont disponibles ; sinon réconciliation explicite et état incertain, jamais lancement infini ou PASS.

## 22. Security / privacy

Classement de risque résiduel AVANT contractualisation, pour un dépôt privé et des credentials de test : LOW / MEDIUM / HIGH. Ce n'est pas un audit de sécurité indépendant des fournisseurs.

| Candidat | Source, données et mode privé | Retention / résidence / sous-traitants | Risque |
| --- | --- | --- | --- |
| TestSprite | SaaS d'exécution ; contexte MCP et données de l'application ; ne pas déduire « aucun code lu » de son blog alors que le workflow fait un résumé du code | Sandboxes éphémères et masking documentés ; rétention exacte prompts/artefacts, résidence et liste complète des sous-traitants UNKNOWN dans les sources qualifiées | HIGH tant que données/target/healing non bornés |
| BrowserStack | Scripts peuvent rester en CI ; apps, DOM/assets, captures et credentials de session exposés au service ; Local = tunnel, pas mode offline | AI one-shot/no-training documenté ; logs persistent séparément. GRR Enterprise avec exclusions metadata et périmètres par produit ; liste publique AWS/Google et autres | MEDIUM avec produit/policy adaptés ; HIGH si besoin strict incompatible |
| Momentic | Tests YAML locaux mais contexte de page et inférence sortants ; upload résultats optionnel n'annule pas l'egress AI | Logs techniques 14 jours et traces AI Langfuse indéfinies par défaut selon data-use ; résultats self-service 30 jours selon pricing ; conditions Enterprise spécifiques | HIGH en self-service sensible ; qualification contractuelle requise |
| Diffblue Cover | Génération locale ; licence et télémétrie réseau selon configuration/plan | Offline Enterprise optionnel ; external telemetry désactivable sous Enterprise ; détail/contrat à confirmer | LOW en environnement offline qualifié, MEDIUM configuration connectée |
| Internal | Tests locaux, choix des permissions ; workers distants restent des sous-traitants | Retention/exfiltration des providers existants et logs à maîtriser ; pas de confidentialité magique | MEDIUM ; LOW seulement profil réellement isolé/local adapté |
| Hybrid / multi | Union des flux de données nécessaires | Plusieurs contrats, rétentions et régions ; ne pas propager automatiquement tout le contexte | HIGH avant qualification ; réduction par minimisation et moteur par data policy |

Sources : [T14](https://docs.testsprite.com/mcp/maintenance/security-compliance), [T15](https://www.testsprite.com/blog/is-testsprite-safe-to-use-with-private-codebases-or-internal-applications), [B12](https://www.browserstack.com/docs/automate-self-hosted), [B13](https://www.browserstack.com/support/faq/browserstack-ai/data-handling-privacy/how-is-my-data-handled-when-using-browserstacks-ai-features), [B14](https://www.browserstack.com/docs/enterprise/security/geo-region-restriction), [B15](https://www.browserstack.com/sub-processors), [M11](https://momentic.ai/docs/account/ai-data-use), [M14](https://momentic.ai/docs/account/security), [D6](https://cover-docs.diffblue.com/get-started/licensing/licensing-offline), [D7](https://cover-docs.diffblue.com/features/cover-cli/cover-cli-admin/telemetry).

### Permissions, secrets et contrôle d'accès

- Aucun SaaS n'a reçu l'accès au repository. Pour le futur : ne demander un GitHub App que si nécessaire ; préférer une exécution depuis un checkout avec token éphémère sans droit push.
- TestSprite GitHub App : intégration documentée, liste exhaustive des permissions effectives de l'installation non vérifiée ici, donc UNKNOWN. Les privilèges exacts doivent être lus avant autorisation.
- BrowserStack MCP local utilise username/access key ; distant OAuth. Les clés n'expirent pas automatiquement selon la FAQ : rotation et coffre nécessaires. Les scopes précis de l'App GitHub sélectionnée restent à qualifier. [B8](https://www.browserstack.com/docs/browserstack-mcp-server/faqs).
- Momentic CLI/analytics utilise API key ; plugins d'authoring peuvent modifier le repo. Ne pas donner accès Git write/PR au processus final. Scopes fins et résidence de toutes les traces : UNKNOWN pour notre contrat.
- Cover nécessite accès au code/build local et peut exécuter l'application analysée : traiter le build comme code non fiable, même sans SaaS.
- Injecter credentials synthétiques via coffre/env contrôlé, masquer avant export, ne jamais sauvegarder de token dans .qa, YAML versionné, rapport ou capture. Les logs réseau et vidéos peuvent contenir des données sensibles.
- Le changelog TestSprite 0.11 décrit un tunnel TLS sans downgrade après échec TLS, mais une compatibilité plaintext demeure si le serveur n'annonce pas TLS. Une policy stricte doit refuser cette compatibilité, pas accepter un avertissement. [T13](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/CHANGELOG.md).

BrowserStack Automate Self-Hosted existe pour navigateurs desktop : ne pas déclarer toute la famille « SaaS obligatoire ». Cela ne prouve pas que tous ses agents, mobiles, analytics et sous-traitants deviennent offline. Pour un projet sans aucune sortie de données permise : INTERNAL_QA_REQUIRED, avec profil réellement compatible, ou Cover offline pour le seul sous-périmètre Java.

## 23. Cost

Prix publics consultés le 13/09/2026, USD affichés, hors taxes/remises/engagement contractuel. Aucune souscription. Les credits fournisseurs ci-dessous ne sont pas les reset credits de l'orchestrateur ; aucun reset credit consommé.

| Candidat | Informations publiques fiables | Ce qui reste inconnu |
| --- | --- | --- |
| TestSprite | Free 150 crédits/mois ; Starter 19 USD/mois après offre premier mois ; Standard 69 USD/mois ; Enterprise sur devis | Coût total selon génération/healing/reruns ; API générique et droits exacts par plan à vérifier |
| BrowserStack | Automate facturé selon parallélisme, utilisateurs non limités selon FAQ ; produits/Pro/AI/Percy distincts | Prix effectif de notre bundle browser+mobile+AI+résidence : UNKNOWN ; ne pas additionner des prix marketing « à partir de » |
| Momentic | Free 2 000 crédits/mois ; PAYG 125 USD/mois / 10 000 crédits ; overage 0,01875 USD/crédit ; sans sièges | Enterprise, résidence/retention/conditions spécifiques : UNKNOWN |
| Diffblue Cover | Options Enterprise offline/telemetry confirmées ; ne pas confondre pricing Diffblue Agents avec Cover | Prix Cover/CI/offline courant : UNKNOWN |
| Internal | Réutilisation d'actifs ; consommation workers + CI + stockage + maintenance | Pas de coût absolu sans temps d'exécution, volumes et benchmark |
| Hybrid / multi | Coûts internes + moteurs activés, devices et artefacts | TCO non mesuré ; contrats/reruns/intégration peuvent dominer |

Sources : [T16](https://www.testsprite.com/pricing), [B16](https://www.browserstack.com/support/faq/plans-pricing/plans/i-work-in-a-team-do-i-need-to-buy-licenses-for-each-user), [M12](https://momentic.ai/pricing), [D6,D7].

Momentic facture aussi séparément la consommation AI et l'infrastructure : step normal 1 crédit, step AI/recovery 2, navigateur hébergé 1/min, Android 8/min, iOS 15/min ; classification 100, triage 500 et sélection AI 300 par run selon la grille consultée. Pour de petites suites, la sélection peut coûter plus que les tests évités. Les chiffres de « tests mensuels » sont des exemples commerciaux dépendants de leur longueur. [Grille détaillée](https://momentic.ai/pricing.md).

TestSprite documente des tarifs de rerun différents selon frontend/backend et chemins anciens ; on ne peut pas convertir uniformément un crédit en test complet. Les API du CLI sont visibles dans son code, mais cela ne prouve pas un droit contractuel illimité à toutes les API publiques. [T10](https://docs.testsprite.com/cli/core/rerun-and-auto-heal), [T16](https://www.testsprite.com/pricing).

Formule de comparaison proposée : TCO = intégration + maintenance + compute local + appels AI + crédits QA + parallèles/devices + stockage/export + temps de triage. Pas de coût fictif « interne gratuit ».

## 24. Vendor lock-in

| Moteur | Tests / formats exportables | Quitter le service en conservant la QA |
| --- | --- | --- |
| Internal | Tests et connaissance dans Git, frameworks standards | Forte indépendance si protocoles/profils restent abstraits ; dépendances LLM/CI subsistent |
| TestSprite | Code récupérable, plans/artefacts ; client CLI Apache-2.0 | PARTIAL : tester hors service les fixtures/auth/dépendances ; export de code ≠ moteur SaaS portable |
| BrowserStack | Selenium/Playwright/Cypress/Appium standard lorsque choisis | LOW/MEDIUM lock-in infrastructure ; plus élevé pour low-code, analytics, baselines Percy |
| Momentic | YAML repo et formats de résultats standards | HIGH pour runtime AI ; pas convertisseur automatique général, sortie Playwright Enterprise ponctuelle documentée |
| Diffblue | Tests Java/JUnit/TestNG standards | Faible pour exécuter tests conservés ; génération continue dépend du produit |
| Hybrid / multi | Contrat neutre et artefacts communs | Dépend de formats choisis, pas du nombre de fournisseurs ; multi peut multiplier le lock-in |

Sources : [T5,T6], [B12], [M8](https://momentic.ai/docs/get-started/test-portability), [D1]. Ne pas remplacer les suites protégées par des conversions non démontrées équivalentes. Garder les versions source et justifications dans Git ; cloud dashboard n'est jamais l'unique source de vérité.

## 25. Regression knowledge base

Structure envisagée seulement, non créée :

~~~text
.qa/
  regression-map.yaml
  invariants.yaml
  critical-paths.yaml
  known-flaky.yaml
  qa-history/
~~~

| Engine | Lire / recevoir contexte | Produire mise à jour | Sans compréhension native |
| --- | --- | --- | --- |
| Internal | Parser validé + références sélectionnées dans prompt | Proposition patch, preuve et review | Déterministe : manifest dérivé, LLM facultatif |
| TestSprite | Contexte/plan/PRD ; parser .qa natif NOT_FOUND | Résultats/risques normalisés puis patch interne proposé | Mapper les invariants à test IDs et assertions ; ne pas régénérer la spec depuis le bug |
| BrowserStack | Données de test, labels et sélection CI | Artefacts/history → propositions | Contrôle des obligations par l'orchestrateur, sans plugin .qa |
| Momentic | Knowledge dashboard avec versioning/bulk import ; contexte agent | Suggestions/usage ; pas écritures .qa garanties | Exporter une projection contrôlée, garder le digest Git |
| Diffblue | Contexte surtout code/build ; langage métier .qa non natif | Couverture/codes et nouveaux tests | Adapter sélection/attentes en amont et vérifier en aval |
| Hybrid / multi | Snapshot unique par request | Une seule proposition réconciliée | Pas de duplication de vérité entre fournisseurs |

La knowledge base Momentic a sa propre priorité de confiance, y compris du savoir système fournisseur non désactivable : ce n'est pas une policy de sécurité imposable par Git. Les règles bloquantes doivent être appliquées dans l'orchestrateur, hors prompt. [M13](https://momentic.ai/docs/ai/knowledge-base).

Schémas futurs : IDs stables, exigences/owners, evidence refs, valid_from_sha, tests associés, expiration des exceptions flaky. Les mises à jour issues d'un FAIL ne sont jamais auto-approuvées. Éviter la boucle où l'outil transforme chaque défaut observé en nouvelle vérité.

qa-history peut contenir des résumés sanitaires versionnés et des manifestes avec digests ; les gros artefacts et secrets ne doivent pas remplir Git. Séparer les faits d'audit opérationnel des connaissances métier. Aucun fichier opérationnel .qa n'a été créé ici.

## 26. Architecture options

### A — INTERNAL

~~~text
WorkItem → Adaptive QA Worker (si nécessaire)
         → impact + authoring contrôlé
         → QualityGateRunner + independent review
         → final verification isolée + normalizer → QAVerdict
~~~

Avantage : bonne adéquation aux régressions Python et réutilisation élevée. Risque : qualité de l'impact/authoring à prouver, pas de cloud mobile natif. Coût : minimum spécifique raisonnable, assurance long terme non triviale.

### B — DIRECT EXTERNAL

~~~text
WorkItem → External QA Adapter → fournisseur (authoring/exécution)
         → preuves + QAResult normalisé → QAVerdict
~~~

NO internal QA LLM is required si le fournisseur suffit. Le control plane interne continue d'imposer SHA, policy, protection, audit, gates existants et merge ; cela ne transforme pas automatiquement l'option en HYBRID. Direct external signifie externalisation de l'intelligence/moteur QA, pas externalisation de l'autorité Git.

Avantage : génération/exploration spécialisée rapide. Risque : périmètre incomplet des suites existantes, dérive de déploiement, permissions et verrouillage. TestSprite est le meilleur candidat fonctionnel à cette option web/API, mais pas un moteur final éligible aujourd'hui sur les chemins bloquants. Momentic devient un concurrent sérieux si le besoin est d'abord repo-centric browser et la policy durcie est démontrée.

### C — HYBRID

~~~text
Internal QA → fast unit/integration + obligations repo
External QA → obligations browser/API/E2E spécialisées
           → composition des preuves du même SHA → verdict
~~~

Les obligations s'additionnent. Un externe requis indisponible bloque, même si l'interne est vert. Un moteur explicitement advisory peut produire des observations sans donner un QA PASS pour un périmètre non testé. Une constatation de régression crédible ne doit pas être ignorée sous prétexte que son moteur était facultatif : escalade policy.

Avantage : couverture supérieure lorsque le projet a effectivement ces besoins. Coût : deux qualifications, récupération durable et diagnostic croisé. SECOND_BEST_OPTION, évolution naturelle mais non investissement obligatoire immédiat.

### D — MULTI_ENGINE_BY_STACK

~~~text
Policy projet/stack
  Python backend → Internal + pytest
  Web frontend   → candidat web qualifié + suites existantes
  Mobile/matrix  → BrowserStack
  Java           → Diffblue authoring + JVM deterministic tests
~~~

Différence : HYBRID compose plusieurs moteurs pour le même WorkItem ; MULTI_ENGINE_BY_STACK route d'abord selon le projet/stack. Les deux peuvent coexister, mais augmenter les adaptateurs avant d'avoir les projets correspondants serait un coût spéculatif. Aucun moteur inférieur ne peut être substitué silencieusement.

## 27. Minimum custom differentiator

Même en adoptant directement un externe, six fonctions restent sous notre responsabilité :

| Fonction commune | Réutiliser | Ajouter, sans duplication |
| --- | --- | --- |
| Contrat et manifest | Project/MVP/WorkItem, Git record | QARequest/QAResult, obligations et capabilities versionnées |
| Intégrité des tests | Git diff + gouvernance de branche | Baseline hashes et autorisation des changements sémantiques |
| Preuve d'exécution | QualityGateRunner et artefacts framework | Parsers, compteurs, résultats manquants/skip, normalisation fail-closed |
| SHA / readonly / merge | SHA-bound review/gates, ff-only | Déploiement attesté, isolation, final ref check et exclusion des mutations concurrentes |
| Audit / recovery | ExecutionStore, HandoffStore, rapports | Journal QA et corrélation run externe ; tentative ≠ résultat remplacé |
| Knowledge / policy | Repo + approval workflow | Schémas .qa, provenance, propositions de mises à jour |

L'interne ajoute surtout impact analysis et authoring/classification via le routing existant. Le direct externe remplace ces fonctions cognitives, pas les six contrôles. Le coût différentiel à comparer n'est donc pas « tout développer » contre « zéro code ».

Ne pas créer un second subprocess framework, scheduler de quotas, système de branches, store de handoff ou moteur de rapports. Si les limites du runner nécessitent une extension, la rendre ciblée et justifiée par le contrat QA.

## 28. Scorecard /100

Méthode : points attribués directement dans chaque maximum pondéré, somme exacte. Plus élevé = meilleure adéquation au périmètre de ce dépôt avec intégration réaliste, pas meilleure marque universelle. Les capacités UNKNOWN n'obtiennent pas un crédit positif fort ; coûts bundle non publics reçoivent 1/5. Les architectures non implémentées sont explicitement des projections d'intégration, pas des PASS de qualification.

| Critère | Poids max | INTERNAL | TESTSPRITE | BROWSERSTACK | MOMENTIC | DIFFBLUE | HYBRID | MULTI_ENGINE_BY_STACK |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Autonomous/headless integration | 15 | 13 | 12 | 12 | 12 | 12 | 11 | 10 |
| Test quality / coverage | 15 | 11 | 9 | 9 | 10 | 9 | 12 | 12 |
| Existing-test protection | 10 | 8 | 2 | 7 | 6 | 5 | 8 | 7 |
| SHA/auditability | 10 | 8 | 3 | 6 | 7 | 7 | 8 | 7 |
| Execution evidence | 10 | 9 | 8 | 9 | 8 | 8 | 9 | 9 |
| Languages/stacks | 10 | 8 | 6 | 7 | 5 | 2 | 9 | 10 |
| E2E/browser/mobile | 10 | 2 | 5 | 10 | 6 | 0 | 8 | 9 |
| Security/privacy | 8 | 6 | 2 | 5 | 2 | 6 | 4 | 3 |
| Cost | 5 | 4 | 3 | 1 | 3 | 1 | 2 | 1 |
| Maintainability | 4 | 3 | 3 | 3 | 2 | 3 | 1 | 1 |
| Vendor independence | 3 | 3 | 2 | 2 | 1 | 3 | 2 | 1 |
| TOTAL | 100 | 75 | 55 | 71 | 62 | 56 | 74 | 70 |

Justifications et limites :

- INTERNAL : crédit pour composants observés et standards, pénalité browser/mobile et travail QA non fait. Protection/SHA sont une projection après durcissement, pas 10/10 promis ; les faux négatifs ne sont pas mesurés.
- TESTSPRITE : forte headless, mais target/healing pénalisent deux contrôles critiques. Le score ne masque pas son meilleur potentiel d'externalisation cognitive web/API.
- BROWSERSTACK : meilleure infrastructure de preuve/matrice ; génération générale, prix total et produits multiples limitent le score.
- MOMENTIC : diff/SHA/CLI renforcent le candidat ; privacy self-service, runtime YAML et garde-fous désactivables réduisent la note.
- DIFFBLUE : score global bas car périmètre Python/polyglotte ; cela ne contredit pas sa première place dans le sous-problème Java unit authoring.
- HYBRID : couverture augmentée mais maturité, coûts de composition et double exploitation pénalisés aujourd'hui.
- MULTI_ENGINE_BY_STACK : diversité sans besoin actuel démontré = investissement anticipé ; chaque adaptateur doit être maintenu.

Sensibilité : si browser/mobile devient obligatoire, INTERNAL ne peut plus couvrir seul la policy et perd l'arbitrage, indépendamment de son score global. Si un fournisseur démontre le gate final et un TCO meilleur, l'option DIRECT EXTERNAL peut gagner. Aucun écart de quelques points ne remplace le POC. Ne pas appliquer une moyenne pour compenser un critère éliminatoire.

## 29. Gating requirements

Un moteur final obligatoire doit satisfaire TOUS les critères applicables :

| Gate | Preuve attendue avant admission |
| --- | --- |
| G1 Exact SHA | SHA complet + test/config/policy digests + déploiement effectivement testé |
| G2 Actual execution | Manifest non vide, tests réellement terminés et assertions, résultats complets |
| G3 Auditable structured result | IDs stables, artefacts exportables, compteurs et erreurs vérifiables |
| G4 Final immutable | Aucun code/test/config/commit changé ; droits effectivement bornés |
| G5 Semantic protection | Healing/repair ne peuvent neutraliser les attentes ; justification versionnée pour modification |
| G6 Autonomous lifecycle | Trigger, terminal detection, timeout, cancellation/arrêt borné, retry contrôlé |
| G7 Private/data acceptance | Permissions, rétention, sortie de données et contrat conformes au projet |
| G8 Recovery fail-closed | Reprise/corrélation ou nouvelle tentative sûre ; aucun PASS perdu/reconstitué par intuition |
| G9 Mandatory coverage | Obligations exécutées, skipped/quarantine/selection non masquants |
| G10 Merge freshness | Ref réel inchangé sous coordination au merge ; A ne peut autoriser B |

| Candidat | Éligibilité finale aujourd'hui sur preuves de cette étude | Usage qui reste possible |
| --- | --- | --- |
| TestSprite V3 concerné | NON : G1/G4/G5 bloqués ; G7 à qualifier | Authoring/exploration advisory ; pas seul gate obligatoire |
| BrowserStack | NON comme QA général complet ; admission d'un sous-ensemble standard conditionnelle | Exécution browser/mobile sous contrôle CI/adapter, après qualification |
| Momentic | NON certifié ; G4/G5/G7/G9 exigent policy et preuve réelle | Candidat browser repo-centric avec verrouillage et assertions explicites |
| Diffblue Cover | NON comme final génératif/global ; G5 et périmètre | Phase 1 Java séparée, tests standards en final |
| Internal | NON livré : G4/G5/G8/G10 manquants comme couche QA | Socle de construction recommandé |
| Hybrid / multi | NON livré ; pas d'héritage magique des gates | Architecture future après admission de chaque obligation |

INCONCLUSIVE, provider unavailable, timeout, empty mandatory suite, artifacts manquants, target inconnue et mismatch SHA bloquent. Une régression établie reste FAIL même si une autre obligation est INCONCLUSIVE ; dans les deux cas merge interdit. La classification d'un flaky ne crée pas une exception implicite.

## 30. POCs — minimum nécessaire, proposés seulement

### POC A — Internal baseline et harness de qualification

Un seul dépôt jetable contrôlé, créé ultérieurement après autorisation : petite API Python avec pytest et un parcours web minimal. Deux SHA connus, bug métier volontaire « total attendu 100, obtenu 80 », test de régression existant protégé, changement technique de selector, bug transitif hors fichier directement modifié et exception flaky versionnée.

Évaluer exactement le workflow cible :

1. Donner base/head/diff, exigences approuvées et manifest protégé.
2. Produire impact et proposer un test manquant ; prouver qu'il échoue sur la variante bug et passe sur le correctif.
3. Vérifier qu'aucune assertion/test/config n'a été neutralisée.
4. Si authoring change le HEAD, rerun gates et review sur le nouveau SHA.
5. Final read-only, résultats structurés, zéro modification du périmètre protégé.
6. Injecter SHA drift, zéro test sélectionné, timeout, perte de process, résultat incomplet et restart ; tous doivent bloquer le faux PASS.
7. Sauvegarder mêmes inputs et critères pour le challenger, sans données privées.

Mesures : défauts détectés/manqués, faux PASS (objectif zéro sur les cas injectés), conservation de baseline (100 %), nouveaux tests utiles vs tautologiques, artefacts, reprise, latence et coût observé. Un petit POC ne démontre pas un taux universel de détection.

### POC B — TestSprite direct challenger, CONDITIONNEL

Même dépôt, mêmes commits, bugs, règles et mesures. Aucun accès au vrai repo. L'intelligence QA peut être entièrement externe ; l'adapter garde uniquement les contrôles communs de section 27.

Précondition AVANT de dépenser/exposer : documentation/version ou attestation fournisseur vérifiable levant les incompatibilités V3 target/healing, accès headless du plan retenu et acceptation des données synthétiques. Ne pas lancer aujourd'hui un spike pour redécouvrir un blocage déjà documenté.

Comparer génération, exécution de régression, preuve de la bonne preview, final sans healing, reprise via runId et coût. Toute demande de régénérer les attentes à partir du bug ou de remplacer la suite existante est un échec de qualification.

Pas de troisième POC demandé. Si un besoin mobile/Java devient prioritaire, remplacer le challenger par le spécialiste correspondant lors de l'arbitrage ; ne pas multiplier les spikes par défaut. Aucun POC n'est préautorisé par ce rapport.

## 31. Recommended architecture

PRIMARY = BUILD_INTERNAL, sous forme de QA mince, piloté par policy et réutilisant les composants existants. Architecture ouverte à l'adoption externe, pas stratégie de construction de tous les outils QA.

Workflow conceptuel obligatoire :

~~~text
WorkItem → Development → HEAD H1
         → QA Phase 1 : impact / test authoring
         → HEAD H2 si tests modifiés
         → Quality Gates(H2)
         → Independent Review(H2)
         → QA Phase 2 : Final Verification READ-ONLY(H2)
         → PASS / FAIL / INCONCLUSIVE(H2)
         → Merge Eligibility(H2 + policy + preuves)
         → contrôle ref réel / exclusion des mutations → ff-only Merge(H2)
~~~

Si une correction est nécessaire après le final, revenir au cycle approprié et invalider les preuves de H2 lorsque le HEAD devient H3. Même si le résultat fournisseur arrive tard, il reste une preuve pour son SHA initial, pas pour le HEAD actuel.

Le final peut comporter des exécutions supplémentaires, mais ne doit pas aveuglément relancer toutes les mêmes commandes si leurs preuves récentes sont réutilisables : cette réutilisation nécessite un contrat explicite d'identité du SHA, tests, policy, environnement et immutabilité. Sans preuve suffisante, réexécuter ; jamais inférer PASS.

L'independent review n'est pas remplacée par un self-review QA. La décision de merge appartient toujours à GitGovernance. Les anciens release/activity/realization reports restent les projections officielles.

## 32. Slice 22 implications

Sans modifier ses objectifs actuels, proposer lors de l'arbitrage :

- Formaliser les contrats de section 21, les capacités/obligations par moteur, les preuves versionnées et les états INCONCLUSIVE.
- Définir QARun/QAVerdict/QAFinding et la persistence QA comme extension du socle, avec policy snapshot et journal de lancement.
- Définir les schémas knowledge/protected-tests et la gouvernance des changements d'attentes ; pas de gestion de secrets dans Git.
- Inscrire comme critères d'acceptation le contrôle du ref réel, la protection du filesystem final, zéro suite obligatoire vide et absence de fallback silencieux.
- Garder API/CLI/MCP derrière des capacités déclarées, sans schéma supposant TestSprite ou un QA LLM obligatoire.

Ces points sont des implications architecturales documentaires, pas des changements de roadmap approuvés. QA persistence runtime, .qa opérationnel et QA merge gate ne sont pas implémentés.

## 33. Slice 23 recommended option — PENDING USER APPROVAL

Option recommandée : InternalQAEngine MVP mince, première qualification Python/pytest, routing adaptatif existant, pas de nouveau runner et pas de dépendance fournisseur QA obligatoire.

La décision reste ouverte à TestSpriteQAEngine ou autre engine si le challenger satisfait les gates et justifie son coût. Ce rapport n'attribue pas la Slice 23 à un fournisseur. Les objectifs des Slices 22–25 n'ont pas été modifiés.

Le browser/device cloud et la génération Java spécialisée attendent un besoin réel. Slice 24 demeure le lieu d'intégration du cycle QA/rework/review/merge ; Slice 25 reste conditionnelle. Aucune QA loop n'a été créée dans cette étude.

## 34. Risks / limitations / WHAT_WOULD_CHANGE_MY_MIND

| Risque | Conséquence / réponse |
| --- | --- |
| Documentation changeante, versions SaaS non publiées | Épingler SDK/CLI, enregistrer capabilities observées, requalifier les mises à jour |
| Promesses fournisseur non testées | POC sur mêmes mutants ; scores documentaires non assimilés à recall |
| Biais interne | Compter le vrai travail restant ; permettre direct external sans LLM maison |
| Biais couverture | Un test qui passe peut figer un bug ; partir des exigences et baselines |
| Déploiement différent du SHA déclaré | Attestation build et contrôle target ; refuser latest |
| Écriture indirecte / configuration | Isolation et contrôle de tous les actifs, pas seulement tests/ |
| Scope réduit par sélection ou quarantine | Manifest obligatoire et mode shadow initial |
| Concurrence / crash | Journal durable, idempotency, ref checks et nouvelle tentative identifiée |
| Données en captures / traces | Synthétique, minimisation, masquage, retention et export contractuels |
| Coût variable | Mesurer crédits, retries, triage, minutes devices et temps humain |
| Interne encore incomplet | Ne pas annoncer une QA disponible ou des garanties acquises |

WHAT_WOULD_CHANGE_MY_MIND :

1. Un moteur externe démontre sur le même harness les dix gates, détecte les défauts imposés sans affaiblir les tests, lie l'exécution au bon build et reste réellement read-only.
2. Son TCO mesuré et sa maintenance sont meilleurs que le minimum interne, sans compromis privacy/récupération. Cela peut faire gagner ADOPT_TESTSPRITE ou ADOPT_MOMENTIC selon le périmètre.
3. Une obligation browser/mobile réelle apparaît : HYBRID devient primaire ; BrowserStack premier choix matrice/appareils.
4. Le portefeuille devient majoritairement Java : DIFFBLUE_FOR_JAVA ou MULTI_ENGINE_BY_STACK deviennent plus pertinents.
5. Le POC interne échoue sur l'impact/authoring et un challenger le réussit : ne pas s'obstiner à construire.
6. Une policy sans sortie de données interdit les SaaS : interne réellement isolé, ou composant Java offline qualifié.

Limites de lecture : documentation officielle consultée et code client TestSprite ciblé, pas audit complet de tous les SDKs ni inspection du code fermé. Les attestations SOC ne sont pas des rapports d'audit que nous avons obtenus. Aucune précision inventée sur permissions d'installation, résidence complète, prix Enterprise ou version SaaS.

## 35. Final decision

Décision proposée, non arbitrage utilisateur :

| Élément | Conclusion |
| --- | --- |
| PRIMARY | BUILD_INTERNAL |
| SECOND_BEST_OPTION | HYBRID |
| Direct external possible architecturalement | OUI, sans QA LLM interne obligatoire ; aucun candidat approuvé comme final universel aujourd'hui |
| Meilleur candidat direct autonome web/API | TestSprite, conditionnel et actuellement bloqué en final V3 |
| Meilleur candidat repo-centric web à considérer | Momentic, policy/privacité et périmètre à qualifier |
| Meilleur browser/mobile réel | BrowserStack |
| Meilleur Java unit authoring | Diffblue Cover |
| Choix Slice 23 recommandé | InternalQAEngine MVP mince — PENDING USER APPROVAL |
| Services externes en production | DEFER jusqu'à besoin et qualification ; ceci n'est pas une seconde recommandation primaire |
| Nombre de POCs | 1 baseline ; 1 challenger conditionnel au maximum |

Livrables : ce Markdown et sa version HTML autonome complète dans docs/reports/qa-build-vs-adopt-report.html. Mise à jour additive de docs/status.md ; aucun changement des objectifs ROADMAP/Slices 22–25.

Aucun code fonctionnel, test, config runtime ou fichier opérationnel .qa modifié/créé. Aucun service QA externe exécuté, compte créé, App connectée, MCP QA appelé ou dépendance installée. Pas de tests produit relancés. Aucun reset credit consommé. Aucun commit de l'étude, aucun push des rapports.

## Annexe — registre des sources primaires

Sources consultées le 13/09/2026, sauf T18–T19, B19–B21 et M17–M18 ajoutées le 14/09/2026. Les pages web vivantes n'ont pas de version immuable sauf indication. Les liens de code TestSprite sont épinglés au commit inspecté ; CODE_CONFIRMED s'applique au client, pas au comportement réel du SaaS.

### Sources TestSprite

- T1 — [MCP tools](https://docs.testsprite.com/mcp/core/tools) — DOC_CONFIRMED, schémas et workflow.
- T2 — [Diff/new change](https://docs.testsprite.com/mcp/core/create-tests-new-feature) — DOC_CONFIRMED, portée du diff et résumé.
- T3 — [Import existing tests](https://docs.testsprite.com/mcp/maintenance/migration-from-other-platforms) — DOC_CONFIRMED, import par plan/régénération.
- T4 — [MCP GitHub integration](https://docs.testsprite.com/mcp/integrations/github-integration) — DOC_CONFIRMED, tests préparés et PR/preview.
- T5 — [CLI capabilities](https://docs.testsprite.com/cli/reference/whats-included) — DOC_CONFIRMED, formats, backend Python, artefacts.
- T6 — [Source et package CLI 0.11.0](https://github.com/TestSprite/testsprite-cli/tree/125872fd1b19e948528177b6f6a2ac25c83dfd89) — CODE_CONFIRMED pour les fichiers inspectés.
- T7 — [V3 advisory implementation](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/v3-advisory.ts) — CODE_CONFIRMED, avertissement target/replay.
- T8 — [Run types](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/runs.types.ts) — CODE_CONFIRMED, IDs/statuts/advisories ; limites décrites en commentaires.
- T9 — [Commands implementation](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/commands/test.ts) — CODE_CONFIRMED, génération plan, local tunnel, opt-out et avertissements.
- T10 — [Rerun and auto-heal](https://docs.testsprite.com/cli/core/rerun-and-auto-heal) — DOC_CONFIRMED, distinction plans/backend/frontend.
- T11 — [Running tests](https://docs.testsprite.com/cli/core/running-tests) — DOC_CONFIRMED, lifecycle CLI et timeout.
- T12 — [HTTP client](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/src/lib/http.ts) — CODE_CONFIRMED, routes/retry/idempotency côté client.
- T13 — [Changelog](https://github.com/TestSprite/testsprite-cli/blob/125872fd1b19e948528177b6f6a2ac25c83dfd89/CHANGELOG.md) — DOC_CONFIRMED, release/tunnel/lifecycle ; certaines docs plus anciennes divergent.
- T14 — [Security / compliance](https://docs.testsprite.com/mcp/maintenance/security-compliance) — DOC_CONFIRMED, sandbox/credentials ; portée contractuelle non certifiée.
- T15 — [Private codebases blog](https://www.testsprite.com/blog/is-testsprite-safe-to-use-with-private-codebases-or-internal-applications) — MARKETING_ONLY pour les absolus « aucun code ».
- T16 — [Pricing](https://www.testsprite.com/pricing) — DOC_CONFIRMED, grille publique à la date.
- T17 — [Overview](https://docs.testsprite.com/mcp/getting-started/overview) — DOC_CONFIRMED pour domaines de tests ; gains commerciaux non retenus comme mesures.
- T18 — [Visual testing use-case](https://www.testsprite.com/use-cases/en/ai-visual-testing-tool) — MARKETING_ONLY, baseline/comparaison sans contrat détaillé qualifié.
- T19 — [Cross-browser use-case](https://www.testsprite.com/use-cases/en/ai-cross-browser-testing-tool) — MARKETING_ONLY, matrice/accessibilité à qualifier.

### Sources BrowserStack

- B1 — [AI-generated test cases](https://www.browserstack.com/docs/test-management/browserstack-ai/ai-generated-test-cases) — DOC_CONFIRMED, cas textuels/requirements.
- B2 — [Smart Test Selection](https://www.browserstack.com/docs/automate/selenium/smart-test-selection) — DOC_CONFIRMED, learning/metadata et limites.
- B3 — [Session REST API](https://www.browserstack.com/docs/automate/api-reference/selenium/session) — DOC_CONFIRMED, status/artefacts et statut client.
- B4 — [Selenium self-healing](https://www.browserstack.com/docs/automate/selenium/self-healing) — DOC_CONFIRMED, locators/opt-in.
- B5 — [Analytics insights](https://www.browserstack.com/docs/test-reporting-and-analytics/features/insights) — DOC_CONFIRMED, historique/metadata.
- B6 — [Debug builds](https://www.browserstack.com/docs/test-reporting-and-analytics/features/debug-your-builds) — DOC_CONFIRMED, analyse et preuves.
- B7 — [Automate API](https://www.browserstack.com/docs/automate/api-reference/selenium/automate-api) — DOC_CONFIRMED, familles d'endpoints.
- B8 — [MCP FAQ](https://www.browserstack.com/docs/browserstack-mcp-server/faqs) et [local MCP](https://www.browserstack.com/docs/browserstack-mcp-server/get-started/local-mcp) — DOC_CONFIRMED, auth, clés et périmètre.
- B9 — [Percy visual testing](https://www.browserstack.com/docs/percy/overview/visual-testing-basics) — DOC_CONFIRMED, baseline/approval et auto-approval main.
- B10 — [Visual testing on real mobile devices](https://www.browserstack.com/docs/test-companion/visual-analysis/app) — DOC_CONFIRMED, app/devices/screenshots.
- B11 — [Analytics retention](https://www.browserstack.com/docs/test-reporting-and-analytics/references/data-retention) — DOC_CONFIRMED, rétention spécifique à ce produit.
- B12 — [Automate Self-Hosted](https://www.browserstack.com/docs/automate-self-hosted) — DOC_CONFIRMED, desktop et frameworks ; pas preuve d'airgap global.
- B13 — [AI data handling](https://www.browserstack.com/support/faq/browserstack-ai/data-handling-privacy/how-is-my-data-handled-when-using-browserstacks-ai-features) — DOC_CONFIRMED, engagements AI distincts des artefacts.
- B14 — [Geo Region Restriction](https://www.browserstack.com/docs/enterprise/security/geo-region-restriction) — DOC_CONFIRMED, Enterprise et exclusions.
- B15 — [Sub-processors](https://www.browserstack.com/sub-processors) — DOC_CONFIRMED, liste publiée ; pas validation contractuelle exhaustive.
- B16 — [Automation licensing](https://www.browserstack.com/support/faq/plans-pricing/plans/i-work-in-a-team-do-i-need-to-buy-licenses-for-each-user) — DOC_CONFIRMED, utilisateurs/parallèles.
- B17 — [Test Companion failure repair](https://www.browserstack.com/docs/test-companion/web-testing/test-lifecycle/fix-failed-tests) — DOC_CONFIRMED, assistant IDE ; pas API générale autonome démontrée.
- B18 — [Percy project types](https://www.browserstack.com/docs/percy/overview/project-type) — DOC_CONFIRMED, séparation licences/frameworks.
- B19 — [Accessibility automation](https://www.browserstack.com/docs/accessibility/automated-tests/get-started) — DOC_CONFIRMED, intégration aux frameworks supportés.
- B20 — [Low Code Agentic Testing](https://www.browserstack.com/docs/low-code-automation/test-recording/browserstack-ai/agentic-testing) — DOC_CONFIRMED, génération/validation/healing, Alpha limitée.
- B21 — [Low Code execution REST API](https://www.browserstack.com/docs/low-code-automation/cicd-integrations/run-tests-api) — DOC_CONFIRMED, trigger/build/status/compteurs ; pas toutes les garanties lifecycle.

### Sources Momentic

- M1 — [AI selection](https://momentic.ai/docs/ai/select) — DOC_CONFIRMED, base/diff/index JS/TS/JSON.
- M2 — [CLI run](https://momentic.ai/docs/cli-reference/momentic/commands/run) — DOC_CONFIRMED, flags et lifecycle.
- M3 — [MCP](https://momentic.ai/docs/coding-agents/mcp-server) — DOC_CONFIRMED, outils, polling et limites web/mobile.
- M4 — [Analytics API overview](https://momentic.ai/docs/api-reference/overview) — DOC_CONFIRMED, lectures de runs terminés uniquement.
- M5 — [List runs schema](https://momentic.ai/docs/api-reference/analytics/list-runs) — DOC_CONFIRMED, gitCommitSha/IDs/filters.
- M6 — [AI configuration](https://momentic.ai/docs/configuration/ai) — DOC_CONFIRMED, recovery/memory/classification/overrideExitCode.
- M7 — [Auto-maintenance](https://momentic.ai/docs/reliability/auto-maintenance) — DOC_CONFIRMED, locator/recovery/repair.
- M8 — [Test portability](https://momentic.ai/docs/get-started/test-portability) — DOC_CONFIRMED, YAML/export limites.
- M9 — [Results and reporting](https://momentic.ai/docs/running-tests/results) — DOC_CONFIRMED, progress et artefacts.
- M10 — [Quarantine](https://momentic.ai/docs/quarantine) — DOC_CONFIRMED, politique/historique.
- M11 — [AI data use](https://momentic.ai/docs/account/ai-data-use) — DOC_CONFIRMED, modèle de données/rétention/providers.
- M12 — [Pricing](https://momentic.ai/pricing) et [grille texte](https://momentic.ai/pricing.md) — DOC_CONFIRMED, page mise à jour le 09/09/2026.
- M13 — [Knowledge base](https://momentic.ai/docs/ai/knowledge-base) — DOC_CONFIRMED, import/versioning/priorités.
- M14 — [Security](https://momentic.ai/docs/account/security) — DOC_CONFIRMED, sous-traitants/engagements ; rapport SOC non obtenu.
- M15 — [Upgrade to Web v3](https://momentic.ai/docs/get-started/upgrade-to-v3) — DOC_CONFIRMED, version majeure ; patch actuel UNKNOWN.
- M16 — [Cloud deprecation](https://momentic.ai/docs/get-started/cloud-deprecation) — DOC_CONFIRMED, migration vers workflow local ; ne pas confondre avec absence de toute infra hébergée.
- M17 — [Golden files](https://momentic.ai/docs/guides/visual-testing/golden-files) — DOC_CONFIRMED, stockage et création/update des baselines.
- M18 — [Upgrade mobile v1](https://momentic.ai/docs/get-started/upgrade-to-mobile-v1) — DOC_CONFIRMED, version majeure et changement de vidéo.

### Sources Diffblue

- D1 — [Cover CLI getting started](https://cover-docs.diffblue.com/get-started/get-started/get-started-cover-cli) — DOC_CONFIRMED, compilation/tests/frameworks.
- D2 — [CLI commands and arguments](https://cover-docs.diffblue.com/features/cover-cli/commands-and-arguments) — DOC_CONFIRMED, patch/validation/reports/reprise par module.
- D3 — [Merge mode](https://cover-docs.diffblue.com/features/cover-cli/writing-tests/merge-mode) — DOC_CONFIRMED, portée des changements.
- D4 — [Release 2026.08.01](https://cover-docs.diffblue.com/updates-and-upgrades/release-archive/2026-08-01) — DOC_CONFIRMED, known issue TG-24166.
- D5 — [Coverage optimizations](https://cover-docs.diffblue.com/features/cover-cli/writing-tests/test-coverage-optimizations) — DOC_CONFIRMED, génération incrémentale et limites de couverture.
- D6 — [Offline licensing](https://cover-docs.diffblue.com/get-started/licensing/licensing-offline) — DOC_CONFIRMED, option Enterprise.
- D7 — [Telemetry](https://cover-docs.diffblue.com/features/cover-cli/cover-cli-admin/telemetry) — DOC_CONFIRMED, external telemetry et restriction de désactivation.
