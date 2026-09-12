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
slice publiée, prochaine étape) et [ROADMAP.md](ROADMAP.md) pour le détail
complet (phases, slices terminées, vision cible, découpage incrémental).

Résumé : Phase 0/0.5 terminées ; Phase 1 (MVP 0.1) en cours, Slices 0 à 7
terminées (contrats providers, ClaudeCodeAdapter, CodexAdapter,
QuotaManager, WorkerSelector, execution audit persistence,
RalphExecutionEngine, Project/MVP orchestration core + durable handoff).

### Architecture décidée

AI Dev Orchestrator = couche mince de gouvernance et sélection au-dessus de Ralph.

```
Project (workspace, roadmap, MVP courant)
  ↓
MVPManager (WorkItems, dépendances, handoff durable)
  ↓
WorkerSelector (capability > governance > quota > cost)
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

Voir « Vision cible du produit » dans `ROADMAP.md` pour le cycle complet
(roadmap → MVP → WorkItems → exécution → review → release → synthèse
multi-agent → approbation optimiste 20 min → MVP suivant), dont seule une
partie est construite à ce stade.

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
