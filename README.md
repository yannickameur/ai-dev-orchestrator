# AI Dev Orchestrator

Orchestrateur IA de développement logiciel piloté par MVP.

Objectifs principaux :
- orchestrer plusieurs agents de développement ;
- privilégier les modèles locaux/gratuits puis les quotas inclus ;
- gérer les quotas et leurs resets ;
- spécialiser les agents selon les tâches ;
- imposer une revue indépendante du code ;
- piloter Git et GitHub par branches et Pull Requests ;
- conserver l'état d'avancement des MVP et des tâches.

## État du projet

Voir [`docs/status.md`](docs/status.md) pour l'état factuel courant (dernière
décision produit, prochaine étape) et [ROADMAP.md](ROADMAP.md) pour le détail
complet (phases, slices terminées, vision cible, découpage incrémental).

Phase 0/0.5/1 terminées — **MVP 0.1 : `DONE`** (clôturé 2026-09-17,
contrat d'acceptation dans `MVP_SPEC.yaml` v3). Gouvernance Git/PR/merge,
QA gouvernée, et le workflow par défaut ci-dessous sont implémentés et
testés (voir `docs/status.md` pour le détail exact, section par section,
plutôt qu'un résumé qui se périmerait vite ici).

### Workflow par défaut : `LEAN_FEATURE_FLOW`

Chaque WorkItem passe par un chemin nominal court — exactement **2
exécutions LLM** sur le chemin heureux (DEV A, DEV B). La QA n'est pas un
agent IA : elle est déterministe, non-LLM, ne consomme donc aucun worker
IA supplémentaire.

```
FEATURE
  ↓
DEV A — AI
  ↓
DEV B — AI corrective review  (2e développeur indépendant, pas en
  ↓                             lecture seule — il corrige et committe)
QA — deterministic             (unique, lecture seule, non-LLM)
  ↓
PASS
  ↓
MERGE + TAG + DONE

QA FAIL
  ↓
DEV FIX — AI
  ↓
QA — deterministic
  ↓ (jusqu'à 3 tentatives QA au total)
HUMAN_REVIEW_REQUIRED     (BLOCKED + TODO ajouté à la roadmap du projet
                            cible ; les autres WorkItems continuent)
```

Pas d'estimation de complexité obligatoire, pas de phase QA Test Authoring
séparée, pas de Final QA séparée — volontairement KISS/YAGNI. L'ancien
pipeline plus lourd (estimation adaptative avant chaque phase, QA/Review
isolées) existe toujours sous le nom `GOVERNED_FULL` : il reste
sélectionnable explicitement mais est **`DEPRECATED`/`REMOVAL_CANDIDATE`**
— seules ses régressions critiques sont corrigées, aucune nouvelle
capacité ne lui est ajoutée. Voir `ROADMAP.md`, section « Chemin nominal
actuel », pour le détail et les invariants exacts.

### Sélection des workers et fallback provider

Un `Worker` (`config/workers.yaml`) est une identité d'exécution avec un
`provider`/`backend` **fixes** et un ou plusieurs `ExecutionProfile`
(`model`/`reasoning_effort`/`quality_tier`/`cost_rank`) — le modèle
concret vit sur le profil choisi, jamais figé sur l'identité elle-même.
`Worker != Provider` : plusieurs workers peuvent partager le même
provider (par exemple deux workers Anthropic distincts) sans jamais créer
plusieurs quotas — `QuotaManager` continue d'observer un seul état par nom
de provider, ces workers ne sont que des identités d'exécution
indépendantes partageant le même quota sous-jacent.

C'est précisément ce qui permet la continuité produit visée : **un quota
provider épuisé ne doit jamais forcer une attente (`WAITING`) tant qu'un
autre worker compatible, sur un provider disponible, peut continuer.**
`DEV B.worker_id != DEV A.worker_id` est **obligatoire** ; un provider
différent est **préféré** mais jamais requis par défaut — un repli sur un
second worker du même provider reste toujours valide. Le pool actuel
(`config/workers.yaml`) déclare au moins 2 workers indépendants par
provider participant (`alice`/`bob` sur anthropic, `victor`/`oscar` sur
openai) pour garantir ce repli sans jamais dépendre de la disponibilité
de l'autre provider. La continuité est obtenue par la taille de ce pool,
pas par une mutation dynamique du provider d'un `Worker` — un
déplacement de `provider`/`backend` vers `ExecutionProfile` a été
envisagé puis explicitement écarté (YAGNI, aucun besoin concret non
couvert par ce pool).

### Architecture décidée

AI Dev Orchestrator = couche mince de gouvernance et sélection au-dessus de Ralph.

```
Project (workspace, roadmap, MVP courant)
  ↓
MVPManager (WorkItems, dépendances, handoff durable, LEAN_FEATURE_FLOW)
  ↓
WorkerSelector (capability > governance > provider availability > priority)
  ↓
RalphExecutionEngine (execution, hats, TDD, review Ralph)
  ↓
ExecutionRecord + HandoffRecord (audit + reprise durable)
```

En dessous de `WorkerSelector`, la sélection s'appuie sur :

```
Provider Adapters (probe() → ProviderState)
        ↓
    QuotaManager (multi-fenêtres, fraîcheur)
```

Voir « Vision cible du produit » dans `ROADMAP.md` pour le cycle long
terme complet (roadmap → MVP → WorkItems → exécution → review → release →
synthèse multi-agent → approbation optimiste 20 min → MVP suivant), et
« Chemin nominal actuel » (même fichier) pour ce qui s'exécute réellement
aujourd'hui par WorkItem.

### Documentation

- [ROADMAP.md](ROADMAP.md) — phases, architecture, vision cible, source de vérité
- [docs/status.md](docs/status.md) — état factuel courant, court
- [MVP_SPEC.yaml](MVP_SPEC.yaml) — critères d'acceptation mesurables du MVP 0.1
- [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md) — étude des projets comparables
- [docs/SPIKE_RALPH.md](docs/SPIKE_RALPH.md) — **résultats du spike Phase 0.5**

Prérequis :
- Ralph CLI installé (`npm install -g @ralph-orchestrator/ralph-cli`)
- Claude Code CLI et Codex CLI authentifiés
- Python 3.10+ avec pytest
