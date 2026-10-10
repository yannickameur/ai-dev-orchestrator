# ROADMAP — AI Dev Orchestrator

Source de vérité fonctionnelle courante : ce que le produit est, ses
principes, l'état des jalons et les travaux futurs. L'historique détaillé
(slices, runs réels, incidents, rapports) vit dans Git
(`git log -p -- ROADMAP.md`), pas ici.

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
au-dessus de Ralph Orchestrator (le CLI `ralph`, qui fait l'exécution
fine — itérations, hats, TDD). Cette couche décide *qui* fait *quoi*,
*quand c'est vraiment terminé* et *comment c'est intégré à Git* — elle ne
réimplémente jamais l'exécution elle-même.

Le moteur est headless. AIDO Code (`~/projects/aido-code`) est l'unique
frontend produit et l'unique propriétaire de la commande `aido` ; il
consomme exclusivement la façade `orchestrator.engine.OrchestratorEngine`.

## 2. Principes structurants

- `LLM IS NOT ORACLE` — un développeur IA seul n'est jamais l'autorité
  finale ; un second développeur indépendant (DEV B) corrige, une QA
  déterministe non-LLM décide seule PASS/FAIL.
- `DEV_B.worker_id != DEV_A.worker_id` — requis, jamais désactivable ;
  provider distinct préféré, jamais requis.
- QA exécutable et déterministe requise pour `COMPLETED` ; preuve liée au
  SHA exact ; tests protégés ; absence de preuve ≠ succès.
- Un quota épuisé ne produit jamais `WAITING` tant qu'un autre worker
  éligible sur un provider disponible existe.
- Pas de retry aveugle : une exécution interrompue est reprise par une
  **nouvelle** exécution (`RECOVERY_REQUIRED`), jamais rejouée.
- Responsabilité unique par composant (voir §3) ; le moteur calcule, le
  frontend affiche.
- Sorties publiques sûres : seule l'observation publique (stdout/stderr
  filtrés, diagnostics typés) traverse la frontière moteur ; aucun bloc
  de raisonnement privé n'est exposé.
- **REUSE FIRST**, **KISS/YAGNI** : le plus petit changement qui
  satisfait un besoin prouvé. Ordre pour toute simplification : DELETE →
  STDLIB → primitive existante → dépendance existante → package mature →
  BUILD. Un audit externe est une entrée, jamais une autorité.

## 3. Architecture actuelle

```
OrchestratorEngine (façade publique, snapshots typés, on_event)
        ↓
ProjectRuntime (composition aido.yaml → stores → services)
        ↓
MVPManager ─ WorkerSelector ─ QuotaManager/ProviderAdapters
        ↓
RalphExecutionEngine (DEV A → DEV B → DEV FIX)
        ↓
QA déterministe (InternalQAEngine / evaluate_qa_verdict)
        ↓
GitGovernanceService (merge fast-forward, tag)
```

- **`MVPManager`** orchestre les WorkItems ; ne sélectionne jamais un
  worker, ne mute jamais Git, ne juge jamais la qualité.
- **`WorkerSelector`** possède la sélection : capacités/exclusions/
  qualité > disponibilité provider > indépendance de review > pression
  quota observée (bande 10 points) > priorité > `worker_id`.
- **`QuotaManager`/`ProviderAdapter`** possèdent la vérité provider (probe
  réel, jamais un reset simulé).
- **`RalphExecutionEngine`** lance le vrai `ralph` et applique
  `ExecutionPermissionMode` (`standard`/`unrestricted`) à la frontière.
- **`GitGovernanceService`** : branche `work/<work-item-id>` idempotente,
  fast-forward uniquement, jamais de rebase/reset/force automatisé.
- **QA déterministe** : verdict PASS/FAIL/INCONCLUSIVE lié au SHA et à
  l'environnement observé, en lecture seule.
- État runtime en SQLite dédiées (`ProjectStateStore`, `ExecutionStore`,
  `WaitStore`, `GitWorkItemStore`, `QARunStore`, …), hors `/tmp` pour
  toute reprise qui peut survivre à un redémarrage
  (`~/.local/state/ai-dev-orchestrator/projects/<project-id>/`). Jamais
  reconstruit ni modifié à la main.

Contrats détaillés : [`docs/PROJECT_CONFIG.md`](docs/PROJECT_CONFIG.md)
(`aido.yaml`, permissions, frontière moteur/librairie),
[`docs/GIT_GOVERNANCE.md`](docs/GIT_GOVERNANCE.md),
[`docs/QA_GOVERNANCE.md`](docs/QA_GOVERNANCE.md),
[`docs/ADAPTIVE_EXECUTION.md`](docs/ADAPTIVE_EXECUTION.md).

## 4. WorkItem Flow

Seul cycle de vie de WorkItem supporté :

```
WorkItem → WorkerSelector → DEV A → DEV B (worker distinct, correctif)
  → QA déterministe → PASS → merge fast-forward gouverné → tag → DONE

QA FAIL → DEV FIX → QA … (3 tentatives QA au total)
  → BLOCKED (HUMAN_REVIEW_REQUIRED)
```

- Nominal : exactement deux exécutions LLM ; la QA ne consomme aucun
  worker IA.
- `WAITING` : aucun worker éligible et un provider candidat
  `quota_exhausted` avec `reset_at` connu ; reprise pull-based par
  re-probe réel.
- `RECOVERY_REQUIRED` : exécution jamais terminée de façon fiable ;
  ré-orchestrable via une nouvelle exécution.
- Un WorkItem `BLOCKED`/`WAITING`/`FAILED` ne bloque jamais les
  WorkItems indépendants ; ses dépendants directs passent `BLOCKED`.
- Les workers implémentent, testent et corrigent avec une progression
  brève ; AIDO collecte les preuves et gère les transitions. Les
  rapports ne sont une tâche worker que si explicitement requis.
- Événements live publics (`on_event`) : phases DEV A/B/FIX/QA/Git,
  sorties worker progressives et bornées, heartbeat neutre, diagnostic
  d'échec typé ; interruption (Ctrl+C) propre avec arrêt de l'arbre de
  processus et recovery.

## 5. Providers et workers

Source de vérité : `config/workers.yaml`. 8 workers, 4 providers, tous
`VALIDATED`, 2 workers par provider.

| Workers | Provider | Backend | Capacités |
|---|---|---|---|
| Alice, Lydie | anthropic | claude_code | development, qa_testing, release_planning, roadmap_synthesis, complexity_estimation |
| Victor, Yannick | openai | codex | development, qa_testing, release_planning, roadmap_synthesis, complexity_estimation |
| Nathaniel, Juno | mistral | vibe | development uniquement |
| Arthur, Nora | gravity | agy | development uniquement |

Vibe expose une disponibilité `EXECUTION_PROBE_ONLY` (rapportée
`unknown`, jamais fabriquée). Gravity lit son quota via le probe
read-only `agy -p "/usage" --output-format json` ; un bucket épuisé rend
le provider indisponible. DeepSeek et Kimi ont été retirés faute de
preuve d'exécution réelle. Ollama n'est pas un provider actuel (P2).

## 6. Capacités optionnelles construites, non enchaînées

Disponibles mais jamais appelées automatiquement par le WorkItem Flow :
`PlanningCoordinator` (`planning.py`), `ApprovalCoordinator`
(`approval.py`, fenêtre optimiste de 20 minutes réservée aux
propositions de roadmap), `RoadmapApplicationService`
(`roadmap_application.py`), `ReleaseManager` (`release_manager.py`),
sélection adaptative / estimation de complexité (`adaptive_execution.py`,
`complexity_estimation.py`). Le CLI historique `orchestrator.cli` reste
importable (tests, développement dual-repo) mais n'est plus installé.

## 7. État des jalons

| Jalon | Statut |
|---|---|
| MVP 0.1, Phase 1, release v0.1.1 (open source) | `DONE` |
| P1 CLI publique, P1.1 bootstrap guidé, P12 `aido.yaml` + permissions | `DONE` |
| P13 découplage moteur / AIDO Code, P13.1–P13.7 | `DONE` |
| P17 routing quota-aware | `DONE` |
| P18 événements live et interruption gracieuse | `DONE` |
| P19/P20 Gravity intégré, providers limités aux validés | `DONE` |
| P21 observabilité live des workers | `DONE` |
| P21.1 sorties publiques sûres | `DONE` (acceptance gouvernée 2026-10-10) |
| P3 DeepSeek + Kimi, P4 étude Mammouth | `RETIRÉ` |
| P14 simplifié : temps d'exécution IA par fournisseur | `DONE` (2026-10-10, `execution_times()`) |
| P14 complet : tokens, coûts, tentatives | `APPROUVÉ`, non implémenté |
| P15 prompt optimization externe | `APPROUVÉ POUR ÉTUDE`, pas d'intégration |
| P16 revue YAGNI/REUSE FIRST | `APPROUVÉ POUR REVUE`, pas de refactor global |

P21.1 : `wi-acc-01` `COMPLETED`, QA gouvernée `PASS` sur `f1e28f8d`,
1 441 tests moteur et 311 tests AIDO Code `PASS`, merge et tag gouvernés,
Ctrl+C et reprise réelle `PASS` sur `p211-live7`. Aucun texte `thinking`
réel non vide n'a été observé ; la suppression est prouvée par tests
synthétiques déterministes. Les statuts historiques WI-P21.1-04
`BLOCKED` et WI-P21.1-05 `FAILED` restent inchangés dans le runtime.

## 8. Travaux futurs

### P14 — Observabilité de consommation (`APPROUVÉ`, version simplifiée livrée)

Enregistrer par exécution, quand réellement disponible : projet/MVP/
WorkItem, phase (DEV A/B/FIX, QA), worker, provider, backend, modèle/
profil, début/fin/durée, statut, tokens d'entrée/sortie/cache/
raisonnement, appels provider, tentatives, reprises, `permission_mode`,
coût observé ou estimé. Capacité exclusivement moteur ; AIDO Code
affiche sans recalculer.

Livré (P14 simplifié) : `OrchestratorEngine.execution_times()`, temps
d'exécution IA par fournisseur du MVP courant (DEV A/B/FIX, toutes
tentatives), lu en lecture seule depuis l'`ExecutionStore` ; affiché par
AIDO Code M3.4 avec les forfaits issus de `probe_workers()`.

Critères d'acceptation restants :
- une métrique non fournie par un backend reste `None`/`UNKNOWN`, jamais
  fabriquée ;
- aucune influence sur la sélection de worker ni sur le verdict QA ;
- exposée via un snapshot public typé de `OrchestratorEngine` ;
- tests offline couvrant au moins un backend avec et un sans tokens.

### P15 — Prompt optimization (`APPROUVÉ POUR ÉTUDE`)

Dépend de P14 (seule source de télémétrie). Séquence obligatoire :
MESURER → IDENTIFIER → DATASET → OPTIMISER → QA → COMPARER → DÉCISION
HUMAINE. Jamais de modification automatique d'un prompt en production ;
outil optionnel, externe, hors du WorkItem Flow.

### P16 — Revue de simplification (`APPROUVÉ POUR REVUE`)

Candidats connus, à traiter un par un avec preuve de gain net
(avant/après LOC, fichiers, dépendances) :
- `Project.current_mvp_id` — candidat DELETE (écrit, jamais lu),
  analyse de compatibilité SQLite/API requise ;
- fingerprint d'environnement QA non-Python — rejeté tant qu'aucun MVP
  non-Python réel n'en établit le besoin.

### Propositions `À VOTER`

| ID | Question |
|---|---|
| P2 | Un provider local (Ollama) justifie-t-il un adaptateur ? |
| P5 | Continuer la validation sur des projets externes plus complexes ? |
| P6 | Étendre la gouvernance Git locale à push/PR/statut CI distants ? |
| P7 | Une instance gérant plusieurs projets isolés ? |
| P8 | Exécution parallèle de WorkItems/projets/workers ? |
| P9 | QA avancée/externe, seulement quand une stack réelle l'exige ? |
| P10 | Isolation de la QA lecture seule (worktree/copie vs solution Ralph) ? |
| P11 | Productiser le cycle planning → approbation → application de roadmap ? |

Aucun WorkItem n'est créé pour une proposition `À VOTER` sans vote
explicite de l'utilisateur.

## 9. Prochain jalon à discuter

Aucun jalon moteur n'est engagé. Candidats naturels : le reste de P14
(tokens et coûts, prérequis de P15), puis P16 au fil de l'eau. AIDO Code M4 (mode non interactif) n'est pas commencé et ne
requiert aucune capacité moteur nouvelle connue.
