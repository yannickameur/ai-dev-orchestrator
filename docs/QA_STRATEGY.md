# QA_STRATEGY.md — QA / Regression Testing Governance architecture

Document d'étude (Phase 2, avant Slices 21-25). Écrit avant toute
implémentation : ce document fixe l'architecture cible, les contrats
conceptuels et les décisions déjà tranchées. Voir `ROADMAP.md`, section
« Découpage incrémental », pour le séquencement retenu (Slices 21-25).

**Statut d'implémentation (2026-09-15)** : Slice 21 (arbitrage
build-vs-adopt) et Slice 21.5 (evidence/SHA hardening) sont DONE ; Slice
22 (socle QA provider-independent — contrats/persistence/`.qa/`/baseline
de tests protégés, sans `InternalQAEngine` ni intégration `MVPManager`)
est DONE — voir `docs/QA_GOVERNANCE.md` pour la description factuelle du
comportement réellement implémenté (ce document-ci reste l'étude
d'architecture d'origine, non mise à jour rétroactivement à chaque
détail d'implémentation). Slices 23-25 restent non implémentées.

Contexte : depuis Slice 20, l'orchestrateur gouverne le cycle de vie Git
d'un WorkItem (branche, SHA, éligibilité au merge, review liée au SHA
exact, quality gates liés au SHA exact). Ce document répond à la question
suivante, restée ouverte : **comment décider qu'une modification est
correcte, suffisamment testée, et n'introduit pas de régression — et qui
(agent interne, solution externe, ou les deux) produit cette décision ?**

Écrit en session « revue de roadmap » (2026-09-13) : documentation et
architecture uniquement, aucun code implémenté, aucune dépendance
installée, aucun appel réseau/SaaS/MCP effectué.

## 0. Principe directeur — aucun biais maison

Même discipline que l'audit OmniRoute (`docs/OMNIROUTE_ARBITRATION.md`) :
**ne pas présumer qu'un agent QA interne complet doit être construit**
simplement parce que nous pouvons le construire. Les Slices déjà
développées ailleurs dans ce projet sont des coûts irrécupérables ; elles
ne doivent influencer aucune décision QA.

Question centrale à trancher factuellement en Slice 21, jamais supposée
ici : *« Si nous commencions la QA aujourd'hui, quelle combinaison offre
le meilleur rapport qualité / autonomie / coût / maintenance ? »*

Ce document garde donc explicitement trois modes ouverts (§2) et ne
sélectionne aucun fournisseur externe définitivement (§9) — l'objectif
de ce document est de documenter l'espace de décision et ses contraintes,
pas de décider à la place du futur spike factuel.

## 1. Principe fondamental — un agent QA ne cherche pas à faire passer le build

Un agent QA (interne ou externe) n'a jamais pour objectif *« make the
build green »*. Son objectif est de déterminer si la modification est
correcte, suffisamment testée, et n'introduit pas de régression non
désirée.

L'IA (interne ou intégrée à une solution externe) peut légitimement :

- analyser le diff et son impact ;
- sélectionner les tests pertinents (Test Impact Analysis, §7) ;
- proposer ou créer de nouveaux tests (quand la policy l'autorise, §5) ;
- analyser et classifier les échecs (§6) ;
- recommander une correction.

Mais **un verdict PASS/FAIL doit toujours être fondé sur des preuves
exécutables** : pytest, Playwright, Jest/Vitest, mypy, Ruff, build, lint,
tests API/contract, E2E, etc. **Un verdict LLM seul n'est jamais une
preuve de PASS.** Ce principe est non négociable dans toute
implémentation future de ce document.

## 2. Trois modes d'architecture

### MODE 1 — INTERNAL_QA

L'orchestrateur possède un QA/Test Agent interne. Il analyse le WorkItem
et le diff, fait le Test Impact Analysis, inspecte les tests existants,
propose/crée des tests supplémentaires, lance les outils déterministes,
analyse les échecs, produit un `QAResult` structuré (§4).

### MODE 2 — EXTERNAL_QA

L'orchestrateur peut utiliser **directement** un produit QA externe comme
moteur QA principal — pas seulement comme un outil de plus utilisé par un
agent interne. Candidats étudiés en §9 : TestSprite, BrowserStack AI
Agents, Momentic, Diffblue Cover (liste non figée, l'architecture doit
permettre d'en ajouter d'autres).

Dans ce mode, `ai-dev-orchestrator` conserve toujours la gouvernance :
WorkItem, Git SHA (Slice 20), quality policy, audit, décision de release,
persistence, retries/recovery, merge eligibility — jamais déléguée à un
fournisseur externe. Il délègue tout ou partie de : test impact analysis,
génération de tests, exécution de tests, E2E, analyse des échecs.

### MODE 3 — HYBRID_QA

Exemple de composition (l'ordre peut être inversé si la technologie le
justifie — non figé ici) :

```
QA Agent interne
    -> unit / integration / contract / invariants
    -> PASS préliminaire
    -> TestSprite / BrowserStack / autre
    -> E2E / exploratoire / visuel / navigateur/device
    -> FINAL QA VERDICT
```

## 3. Abstraction cible — QAEngine

Contrat conceptuel à étudier en Slice 21 (nom exact — `QAEngine` vs
`TestAgent`/`QAProvider`/`VerificationEngine` — à choisir lors de
l'étude, pas figé ici) :

```
QARequest -> QAEngine -> QAResult
```

Implémentations envisageables (aucune codée) : `InternalQAEngine`,
`TestSpriteQAEngine`, `BrowserStackQAEngine`, `MomenticQAEngine`,
`DiffblueQAEngine`, et de futures solutions.

### 3.1 QARequest (intention, schéma non figé)

Le futur contrat devra probablement porter : `project_id`, `mvp_id`,
`work_item_id`, `workspace`, `base_sha`, `head_sha`, `objective`,
`acceptance_criteria`, `changed_files`, `existing_tests`,
`review_findings`, `risk`/`criticality`, `requested_test_levels`,
`technology_stack`.

### 3.2 QAResult (intention, schéma non figé)

Doit contenir au minimum un `verdict` (`PASS`/`FAIL`/`INCONCLUSIVE`,
§10), et : `change_scope`, `risks`, `tests_selected`, `tests_added`,
`tests_executed`, `passed`/`failed`/`skipped`, `failure_classifications`
(§6), `regressions`, `coverage_gaps`, `recommended_actions`,
`requires_coding_agent`, avec des références auditables (`command`,
`test`, `stack trace`, `source file`, `git SHA`).

### 3.3 Séparation AI worker selection / QA engine selection

Un agent QA interne, s'il est retenu, s'intègre au mécanisme adaptatif
déjà existant (Slice 16/17/19) : `role=qa_testing` -> pre-flight
complexité -> `minimum_quality_tier` -> `WorkerSelector` -> profil ->
`AdaptiveExecutionDecision` — réutilisation stricte, aucune seconde
implémentation.

Un outil externe spécialisé, en revanche, a une sélection basée sur
**capability/stack** plutôt que sur `QualityTier` LLM (TestSprite n'a pas
de "reasoning_effort" à choisir de la même façon qu'un worker LLM). Ces
deux notions — **« AI worker selection »** et **« QA engine selection »**
— doivent rester distinctes dans toute implémentation future, jamais
confondues dans un même mécanisme.

## 4. Classification des échecs

Aucun test en échec n'est automatiquement « réparé ». Classes à
distinguer (`FailureClassification`) :

| Classe | Signification |
|---|---|
| `REGRESSION` | Comportement précédemment correct maintenant cassé |
| `EXPECTED_CHANGE` | Le test échoue parce que le comportement attendu a changé légitimement (acceptance criteria/spec/décision tracée) |
| `TEST_DEFECT` | Le test lui-même est incorrect, indépendamment du code |
| `FLAKY_TEST` | Échec non déterministe, cause suspectée mais non confirmée |
| `ENVIRONMENT_FAILURE` | Échec dû à l'environnement d'exécution, pas au code/test |
| `UNKNOWN` | Classification impossible avec les preuves disponibles — jamais forcée dans une autre classe |

## 5. Protection des tests existants — invariant critique

**Un test de référence existant est un actif protégé.** Ni un agent QA
interne ni une solution externe ne peut, uniquement parce que le nouveau
code échoue :

- supprimer silencieusement une assertion ;
- affaiblir une assertion ;
- skip un test ;
- remplacer une valeur attendue ;
- « self-heal » sémantiquement un test.

Une modification d'un test existant doit être justifiée par un changement
attendu **traçable** : acceptance criteria, requirement, spécification,
décision de roadmap, décision d'architecture, ou décision humaine
versionnée.

### 5.1 Self-healing technique vs sémantique

Les solutions externes (notamment BrowserStack) proposent du
self-healing. La policy doit distinguer :

- **Self-healing technique (acceptable)** : ex. un sélecteur UI a changé
  sans changement fonctionnel — le test continue de vérifier la même
  intention.
- **Self-healing sémantique (interdit)** : ex. attendu = 100, nouveau
  résultat = 80 -> changer automatiquement le test pour accepter 80. Ce
  cas est interdit sans décision humaine versionnée.

## 6. Test Impact Analysis

Capacité à ajouter (Slice 22) : à partir de `base_sha`/`head_sha`/diff —

```
fichiers changés
    -> symboles/modules/config changés
    -> dépendances directes
    -> capacités potentiellement impactées
    -> tests existants
    -> tests manquants
    -> portée de régression recommandée
```

Objectif : ne pas exécuter systématiquement la suite de tests maximale
dès la première boucle. Stratégie en deux temps : **fast feedback**
(tests ciblés) puis **regression confidence** (suite globale pertinente
avant PASS final).

### 6.1 Pyramide de tests à considérer

Static checks, lint, type checking, unit, component, integration,
contract/API, E2E, regression, visuel/device si applicable. Ne pas
pousser automatiquement toute vérification vers de l'E2E.

## 7. Position dans le workflow — deux phases QA

Depuis Slice 20, les preuves (quality gate, review) sont liées au SHA
Git exact (`compute_merge_eligibility` invalide silencieusement toute
preuve liée à un SHA périmé). Si l'agent QA ajoute ou modifie des tests,
il modifie le HEAD — une review précédente devient alors potentiellement
obsolète. Ce document tranche cette question par une architecture en deux
phases explicitement distinctes :

### QA Phase 1 — Test Design / Test Authoring

Avant la review finale. Peut analyser l'impact, créer des tests, créer
des fixtures, compléter la couverture. **Ne modifie jamais le code de
production.** Si elle produit un commit de tests, le HEAD change — les
quality gates puis la review portent ensuite sur ce nouveau HEAD (même
mécanisme SHA-bound que Slice 20).

### QA Phase 2 — Final Verification

Après la review finale. **Read-only sur le code et les tests.** Exécute
les tests sélectionnés, la régression pertinente, et un QA externe
éventuel. Produit `PASS`/`FAIL`/`INCONCLUSIVE`. **Ne modifie jamais le
HEAD.** Si elle découvre qu'un test supplémentaire est nécessaire, elle
renvoie le WorkItem dans la boucle TEST_AUTHORING/REWORK — elle ne
modifie jamais silencieusement le SHA déjà reviewé.

### 7.1 Workflow cible complet

```
WorkItem
   -> Adaptive Development
   -> Test Impact / QA Test Authoring (Phase 1)
   -> deterministic gates
   -> Independent Code Review
   -> Final QA Verification (Phase 2)
   ->        +-------- PASS --------+
             |                      v
             |                Merge Eligibility (Slice 20)
             |                      v
             |                    Merge
             |
             +- FAIL
                  v
             Failure classification (§4)
                  v
             Coding/Rework Agent
                  v
             tests/gates/re-review/QA (nouveau HEAD)
                  v
                 ...
```

À chaque modification du HEAD, les preuves précédentes liées à un ancien
SHA sont invalidées par les mécanismes déjà en place depuis Slice 20 —
aucun mécanisme nouveau n'est requis pour cet aspect, uniquement son
extension au domaine QA (SHA-binding du `QAVerdict`, §8).

## 8. SHA-binding et éligibilité au merge

`MergeEligibility`/`ReleaseManager` (Slice 20, à étendre en Slice 24)
devront exiger `QAVerdict.PASS` sur le `head_sha` courant lorsque QA est
`required` par la policy — un `QAVerdict.PASS` obtenu sur un ancien SHA
n'autorise jamais le merge d'un nouveau SHA. Aucune décision LLM dans
l'éligibilité (cohérent avec `compute_merge_eligibility`, Slice 20, qui
reste une fonction pure déterministe).

### 8.1 Primitives d'evidence hardening déjà disponibles (Slice 21.5)

Avant même la Slice 22/23, trois primitives génériques nécessaires à une
future QA Final Verification obligatoire existent déjà dans
`src/orchestrator/validation.py`, ajoutées en Slice 21.5 suite à l'audit
Codex (rejoué et confirmé, pas simplement fait confiance) :

- `QualityGateRunner.run_gate(..., require_nonempty_mandatory_manifest=True)`
  — un manifest de checks obligatoires vide (ou entièrement optionnel) ne
  peut plus jamais produire `passed=True` quand ce flag est activé (reste
  `False` par défaut, rétrocompatible). Une future QA Final Verification
  obligatoire devra toujours passer ce flag à `True`.
- `ValidationStore.record_manifest`/`get_manifest_for_run` — un
  `validation_run_id` reste lié au manifest (quelle commande, `required`
  ou non) réellement appliqué au moment du run, jamais recalculé depuis la
  configuration *courante* du projet lors d'une relecture
  (`get_gate_result`). Un changement de policy après coup ne peut plus
  faire dériver silencieusement le sens d'un ancien verdict.
- `QualityGateRunner.run_gate(..., verify_repository_unchanged=True)` —
  un run déclaré read-only (le futur mode QA Phase 2, §7) lève
  `ReadOnlyValidationViolationError` si le HEAD change pendant l'exécution
  des commandes configurées, au lieu de renvoyer un résultat qui pourrait
  être confondu avec un PASS/FAIL légitime.

Ces trois primitives ne construisent pas `InternalQAEngine` (toujours
Slice 23) — elles sont le socle que Slice 22/23 réutiliseront sans
seconde implémentation.

### 8.2 Boucle QA FAIL

QA FAIL -> classification de l'échec -> coding/rework agent -> nouveau
HEAD -> tests/gates/re-review/QA. Les cycles doivent être bornés (même
principe que `ReviewPolicy.max_review_cycles`, Slice 9/17) ; au-delà :
`BLOCKED`/`HUMAN_ESCALATION` selon policy — jamais de boucle autonome
infinie.

## 9. INCONCLUSIVE — statut de premier ordre

`INCONCLUSIVE` n'est **jamais** équivalent à `PASS`. Exemples :
environnement indisponible, BrowserStack/TestSprite indisponible,
dépendance externe cassée, spécifications contradictoires, test
impossible à exécuter.

### 9.1 Indisponibilité d'un fournisseur externe

Si un moteur QA externe est `required` par la policy et indisponible :
jamais de remplacement silencieux par une vérification plus faible —
`WAITING`/`INCONCLUSIVE`/`BLOCKED` selon la policy explicite (même
discipline fail-closed que Slice 17/19/20). Si le moteur externe est
seulement `optional`, le système peut continuer selon une policy
explicite — jamais un fallback implicite.

## 10. Indépendance QA

Sujet à trancher avec preuve en Slice 23, pas figé ici. Politique cible
probable : `qa_worker_id != author_worker_id` obligatoire ; `qa_worker_id
!= reviewer_worker_id` préféré mais pas nécessairement obligatoire ;
diversité de provider configurable — par analogie avec la politique
cross-provider déjà existante pour la review (`WorkerSelectionPolicy`,
Slice 4/17/19), sans présumer qu'elle doit être identique.

## 11. Sélection du moteur QA — policy conceptuelle

Policy future (non codée) : `qa.mode` (`internal`/`external`/`hybrid`),
`qa.preferred_engine` (`testsprite`/`browserstack`/`momentic`/
`diffblue`/...), et une fallback policy explicite — **jamais un fallback
silencieux qui réduit le niveau de vérification requis** (cohérent avec
§9.1).

### 11.1 Critère d'usage direct d'une solution externe

Si une solution spécialisée répond suffisamment bien aux critères
suivants, `ai-dev-orchestrator` **peut l'utiliser directement** comme
`QAEngine`, sans développer un QA LLM interne équivalent :

- API/CLI/MCP automatisable ;
- fonctionne sans interaction humaine obligatoire ;
- accepte repository/diff/commit SHA ;
- produit des résultats structurés ;
- exécute réellement les tests (pas seulement un avis) ;
- résultats auditables ;
- intégrable CI/headless ;
- reprise/timeout raisonnables ;
- coût acceptable ;
- données/confidentialité acceptables (§13).

Dans ce cas, le code de ce projet se limite à : adapter, policy,
persistence, normalisation, audit, SHA-binding, release/merge
governance — jamais une réimplémentation de ce que la solution externe
fait déjà.

## 12. Intégration — MCP / API / CLI / plugin

Les intégrations externes peuvent arriver par MCP, API HTTP, CLI, ou
plugin/connector — **le cœur ne doit dépendre d'aucun protocole
spécifique**. Ce principe suit celui déjà appliqué pour Claude Code/Codex
(le cœur ne connaît que `Worker`/`ExecutionProfile`, jamais un provider
nommé en dur).

## 13. Sécurité / confidentialité

Le futur comparatif factuel (Slice 21) doit analyser, par candidat : code
source envoyé, prompts, credentials, données de test, PII, secrets, accès
au repository, rétention, SaaS vs exécution locale, dépôts privés,
résidence des données. Ce comparatif peut à lui seul rendre
`INTERNAL_QA` obligatoire pour certains projets cibles, indépendamment
des autres critères — cette possibilité doit rester ouverte, pas
tranchée ici.

## 14. Coûts et observabilité

À ajouter à la future étude : coût par run QA, coût par WorkItem, coût
E2E, matrice devices/browsers, tokens LLM, comparaison de coût
internal/external/hybrid.

Métriques à prévoir pour permettre une comparaison factuelle ultérieure
(Internal QA vs TestSprite vs BrowserStack vs Momentic, etc.) : durée QA,
tests exécutés, tests ajoutés, pass/fail, classes d'échec, nombre de
flaky, coverage gaps, coût fournisseur externe, nombre de retries.

Aucun apprentissage automatique de routage n'est développé maintenant —
le routage historique (ex. "backend Python -> Internal QA + pytest plus
efficace", "frontend browser-heavy -> moteur E2E externe plus performant
en couverture") reste une direction future à documenter, pas à
implémenter.

## 15. Base de connaissance de régression

Concept à ajouter (Slice 22) : une mémoire QA versionnée **dans le dépôt
cible** (pas dans les stores SQLite de l'orchestrateur) :

```
.qa/
  regression-map.yaml
  invariants.yaml
  critical-paths.yaml
  known-flaky.yaml
  qa-history/
```

### 15.1 Frontière Git vs SQLite

Règle de conception à respecter dans toute implémentation future : **Git
contient la connaissance durable qui doit voyager avec le projet cible**
(invariants, cartographie de régression, chemins critiques, flaky
connus). **SQLite contient l'état runtime/audit de l'orchestrateur**
(`QARun`, `QAVerdict`, exécutions, timestamps — données opérationnelles,
pas de la connaissance produit).

### 15.2 `.qa/invariants.yaml` — exemple conceptuel

Champs par invariant : `invariant_id`, `description`, `criticality`,
`source`, `introduced_by_work_item`, `related_tests`, `active`.

Exemples conceptuels (illustratifs, pas encore réels) :

- `ORCH-QA-001` — A mandatory reviewer failure prevents release.
- `ORCH-QA-002` — A quality gate for SHA A cannot authorize SHA B.
- `ORCH-QA-003` — An existing regression test cannot be weakened without
  a versioned expected-change decision.

### 15.3 `.qa/known-flaky.yaml`

Ne sert **jamais** de liste de tests ignorés. Documente : test, evidence,
cause suspectée, date, owner/status, retry policy éventuelle. Un flaky
critique reste visible — jamais masqué par sa présence dans ce fichier.

### 15.4 Bug -> mémoire de régression

Workflow cible : bug découvert -> test de reproduction -> bug corrigé ->
test enregistré comme protection -> `regression-map` mise à jour ->
releases futures protégées par ce test.

## 16. Extensions futures prévues (non implémentées)

- `RealizationReport` (Slice 18/19/20) : étendre pour inclure moteur QA,
  worker QA éventuel, test impact analysis, tests sélectionnés/ajoutés,
  exécutions QA, régressions, flaky, verdict, références au
  rapport/artefact QA externe — sans faire du HTML une source de vérité
  (même principe que Slice 18 : projection factuelle uniquement).
- `MergeEligibility`/`ReleaseManager` (Slice 20) : exigence
  `QAVerdict.PASS` sur le SHA courant quand QA est requis (§8).

## 17. Candidats externes — axes d'étude (Slice 21, pas de choix ici)

**Les informations actuellement disponibles ne suffisent pas pour choisir
définitivement.** Le tableau ci-dessous ne fixe aucune décision — il
cadre le futur audit/spike factuel de Slice 21.

| Axe | TestSprite | BrowserStack AI Agents | Momentic | Diffblue Cover | Internal QA Agent |
|---|---|---|---|---|---|
| Unit | à auditer | à auditer | à auditer | oui (Java, spécialisé) | oui (réutilise pytest/etc. existants) |
| Integration | à auditer | à auditer | à auditer | non (hors scope connu) | oui |
| API | à auditer | à auditer | probable | non | oui |
| Browser E2E | à auditer | oui (cœur de produit) | oui (cœur de produit) | non | à construire |
| Mobile | à auditer | oui (cœur de produit) | à auditer | non | à construire |
| Visuel | à auditer | oui | à auditer | non | à construire |
| Génération de tests | oui (mise en avant) | partiel | partiel | oui (cœur de produit, Java) | à construire |
| Maintenance de tests / self-healing | à auditer | oui (self-healing technique mis en avant) | à auditer | non applicable | à construire |
| Analyse d'échecs | à auditer | à auditer | à auditer | non applicable | à construire |
| Diff-aware | mis en avant | à auditer | à auditer | à auditer | oui (Test Impact Analysis, §6) |
| Full-codebase | mis en avant | non (E2E ciblé) | non (E2E ciblé) | oui (génération) | oui |
| MCP/API/CLI | MCP mis en avant | API/CLI | API/CLI probable | CLI/plugin build | interne |
| Headless/CI | à auditer | oui | oui | oui | oui |
| Langages | à auditer | multi (E2E web/mobile) | web/frontend | **Java uniquement** | ceux déjà supportés par le dépôt cible |
| Coût | à auditer | SaaS, à chiffrer | SaaS, à chiffrer | à auditer | coût de développement/maintenance interne |
| Exécution locale/privée | à auditer | non (SaaS) | à auditer | à auditer | oui |
| Données envoyées à l'extérieur | à auditer (§13) | oui (SaaS) | oui (SaaS) | à auditer | non |
| Auditabilité | à auditer | à auditer | à auditer | à auditer | totale (code interne) |
| Vendor lock-in | à auditer | modéré à élevé (device farm) | modéré | faible (Java only, spécialisé) | aucun |
| Adéquation orchestration autonome | positionnement le plus général déclaré — à vérifier | fort si matrice browser/device réelle | fort si frontend E2E | fort seulement si stack Java | dépend entièrement de l'effort investi |

Positionnement qualitatif (déclaratif, à confirmer par le spike, jamais
une sélection) :

- **TestSprite** semble actuellement le candidat le plus général pour un
  premier spike (diff/codebase scope, plan de tests, génération de
  tests, API/E2E, exécution, MCP) — non sélectionné définitivement sur
  cette seule base.
- **BrowserStack AI Agents** : candidat particulièrement intéressant si
  un projet cible a une matrice navigateur/mobile/visuelle réelle et un
  besoin de self-healing *technique*.
- **Momentic** : candidat orienté QA frontend/E2E.
- **Diffblue Cover** : spécialisé dans la génération de tests unitaires
  Java — probablement pas un moteur QA général, mais un provider
  spécialisé selon stack (cohérent avec `MULTI_ENGINE_BY_STACK`, §18).

## 18. Critère de décision de Slice 21

À la fin de Slice 21, choisir explicitement, avec preuve :
`BUILD_INTERNAL`, `ADOPT_TESTSPRITE`, `ADOPT_BROWSERSTACK`,
`ADOPT_MOMENTIC`, `ADOPT_DIFFBLUE_FOR_JAVA`, `HYBRID`,
`MULTI_ENGINE_BY_STACK`, ou `DEFER_EXTERNAL_QA`.

## 19. Ce que ce document ne décide pas

- Le nom exact du contrat (`QAEngine` vs alternatives).
- Le schéma final figé de `QARequest`/`QAResult`.
- Le fournisseur externe retenu, s'il y en a un.
- Si `qa_worker_id != reviewer_worker_id` est obligatoire ou préféré.
- Si Slice 25 (Advanced QA / External E2E) est un jour nécessaire.

Ces questions restent ouvertes intentionnellement — elles sont les
livrables des Slices 21 et suivantes, jamais du présent document.
