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

Phase actuelle :
- **Phase 0.5 — Reuse Spike : DONE**
- **Phase 1 — MVP 0.1 : READY TO START**

Phases complétées :
- **Phase 0** — Spécification et architecture
- **Phase 0.5** — Reuse Spike validant Ralph Orchestrator 2.10.1 comme moteur d'exécution

### Architecture décidée

AI Dev Orchestrator = couche mince de gouvernance et sélection au-dessus de Ralph.

```
Task → AI Dev Orchestrator (quotas, gouvernance, sélection)
        ↓
    Provider Adapters (probe() → ProviderState)
        ↓
    QuotaManager (multi-fenêtres, fraîcheur)
        ↓
    WorkerSelector (capability > governance > quota > cost)
        ↓
    Ralph Orchestrator (execution, hats, TDD, review)
        ↓
    Result → Execution record
```

### Documentation

- [ROADMAP.md](ROADMAP.md) — phases, architecture, source de vérité
- [MVP_SPEC.yaml](MVP_SPEC.yaml) — critères d'acceptation mesurables du MVP 0.1
- [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md) — étude des projets comparables
- [docs/SPIKE_RALPH.md](docs/SPIKE_RALPH.md) — **résultats du spike Phase 0.5**

### Démarrage Phase 1

Construire (dans cet ordre) :

**Slice 0 — Contrats normalisés (pré-requis)**
1. **ProviderState, ProviderAvailability, QuotaWindow, ResetCredit** — contrats de sérialisation
2. **Interface ProviderAdapter** — `probe() → ProviderState`
3. **ClaudeCodeAdapter** — implémentation Claude stream-json
4. **CodexAdapter** — implémentation Codex app-server
5. **Tests avec fixtures spike** — valider contrats avant intégration

**Slice 1+ — Après validation contrat**
6. **QuotaManager** — multi-fenêtres, politique de fraîcheur
7. **WorkerSelector** — sélection par capacité, gouvernance (Author ≠ Reviewer), quota, coût
8. **Persistence / execution audit** (MVP, Task, Worker, Execution, QuotaWindow) → SQLite
9. **RalphExecutionEngine** — wrapper léger autour de Ralph 2.10.1
10. **MVPManager** et **Task orchestration** complète

**Compléments MVP 0.1** (pas de rang fixe imposé par le spike) :
- **Abstraction Workspace** — LocalGitWorkspace
- **CLI** et **tests complets**

Prérequis :
- Ralph CLI installé (`npm install -g @ralph-orchestrator/ralph-cli`)
- Claude Code CLI et Codex CLI authentifiés
- Python 3.9+ avec pytest
