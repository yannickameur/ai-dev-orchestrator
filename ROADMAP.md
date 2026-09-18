# ROADMAP — AI Dev Orchestrator

Ce fichier est la **source de vérité fonctionnelle courante** du projet :
ce que le produit est, ce qui est implémenté aujourd'hui, comment ça
marche, et ce qui reste proposé (jamais approuvé) pour la suite.

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

- **v0.1.1** — candidate à la première release publique/open-source.
- **MVP 0.1** : `DONE` (contrat d'acceptation : `MVP_SPEC.yaml` v4).
- **Phase 1** : `DONE`.
- Suite de tests offline : **983 PASS** (voir §11 et docs/status.md pour
  le détail).
- Aucune slice/aucun développement actif en cours.

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

6 workers, 3 providers. Chaque provider participant a au moins 2 workers
indépendants (`DEV_B.worker_id != DEV_A.worker_id` reste toujours
satisfiable sans dépendre de l'autre provider).

Mistral/Vibe : capacité volontairement limitée à `development` (le spike
n'a produit de preuve d'exécution réelle que pour ce type de travail —
voir `docs/VIBE_SPIKE.md`) ; son signal de disponibilité est
`EXECUTION_PROBE_ONLY` (pas de fenêtre de quota observable), toujours
rapporté honnêtement comme `unknown`, jamais fabriqué en pourcentage.

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
- Préparation de la release open-source v0.1.1.

Chronologie détaillée : historique Git (`git log`) et docs techniques
(`docs/status.md`, `docs/QA_GOVERNANCE.md`, `docs/GIT_GOVERNANCE.md`,
`docs/ADAPTIVE_EXECUTION.md`, `docs/VIBE_SPIKE.md`).

## 13. Propositions à voter

**RIEN dans cette section n'est du travail approuvé.** Chaque ligne a le
statut `À VOTER` — aucun ordre n'implique une priorité, aucun MVP 0.2
n'est ouvert, aucun WorkItem n'est créé pour une proposition tant qu'elle
n'a pas été explicitement votée par l'utilisateur.

| ID | Proposition | Valeur / question à trancher | Statut |
|----|-------------|------------------------------|--------|
| P1 | CLI / productisation | Le projet doit-il exposer une CLI publique pour qu'un utilisateur n'ait plus besoin d'un harnais Python ? | À VOTER |
| P2 | Ollama / provider local | Un provider gratuit/local est-il assez utile pour justifier un adaptateur ? | À VOTER |
| P3 | Providers supplémentaires à coût marginal nul | Quels autres providers gratuits/par abonnement devraient rejoindre le pool ? | À VOTER |
| P4 | Étude build-vs-reuse Mammouth AI | Offre-t-il des capacités multi-provider utiles à réutiliser plutôt qu'à construire ? | À VOTER |
| P5 | Projets de validation externes progressifs | Continuer à valider sur des projets réels plus complexes ? | À VOTER |
| P6 | Workflow GitHub distant complet | Étendre la gouvernance Git locale actuelle à un vrai push/PR/statut CI distant ? | À VOTER |
| P7 | Orchestration multi-projets | Une instance d'orchestrateur gérant plusieurs projets isolés ? | À VOTER |
| P8 | Exécution parallèle | WorkItems/projets/workers en parallèle ? | À VOTER |
| P9 | QA avancée/externe | Candidats historiquement étudiés : BrowserStack, Momentic, TestSprite, Diffblue — adopter seulement quand un vrai projet/stack établit le besoin ? | À VOTER |
| P10 | Isolation d'exécution QA en lecture seule | Worktree/copie isolée vs. solution amont Ralph pour les commits de housekeeping ? | À VOTER |
| P11 | Productiser le cycle optionnel release/planning | `PlanningCoordinator` → `ApprovalCoordinator` → `RoadmapApplicationService` existent déjà (§10) — en faire un flux produit supporté de bout en bout ? | À VOTER |
| P12 | Format de configuration de projet public | Aucun format déclaratif stable n'existe actuellement pour onboarder un projet (harnais Python custom) — faut-il en supporter un ? | À VOTER |

Rien ci-dessus n'est planifié. La prochaine étape, si l'utilisateur le
décide, est un vote explicite proposition par proposition — pas une
sélection automatique par cette session ni une future session.
