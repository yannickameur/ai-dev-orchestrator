# AI Dev Orchestrator

Une couche mince de gouvernance/quota/sélection au-dessus de Ralph
Orchestrator (le CLI `ralph`) qui fait travailler plusieurs agents IA
développeurs (Claude Code, Codex CLI, Mistral Vibe) sur un vrai dépôt
Git, sous revue indépendante et QA déterministe obligatoire — jamais sur
la seule parole d'un LLM.

## Ce que ça fait

- décompose un projet en `WorkItems` gouvernés (dépendances, statut,
  reprise durable après interruption) ;
- sélectionne les workers réels (`WorkerSelector`) selon capability →
  gouvernance (DEV B ≠ DEV A) → disponibilité provider (quota réel) →
  priorité — jamais un provider forcé ;
- fait développer un WorkItem par un premier agent (DEV A), le fait
  relire et corriger par un second agent réellement indépendant (DEV
  B), puis exige un verdict QA déterministe (tests réels exécutés,
  jamais un LLM qui affirme que "ça marche") avant tout merge ;
- gouverne Git lui-même : branche dédiée par WorkItem, merge
  fast-forward-only, tag, jamais de réécriture d'historique, jamais de
  `--force`.

## État du projet

Voir [`docs/status.md`](docs/status.md) pour l'état factuel courant et
[`ROADMAP.md`](ROADMAP.md) pour le détail complet (vision, état actuel
du produit, architecture, WorkItem Flow, invariants, validations réelles
et propositions à voter — la source de vérité fonctionnelle de ce
projet).

**MVP 0.1 : `DONE`** (contrat d'acceptation dans
[`MVP_SPEC.yaml`](MVP_SPEC.yaml)). Gouvernance Git/merge/tag, QA gouvernée,
et le workflow ci-dessous sont implémentés, testés, et validés par une
exécution réelle de bout en bout sur un projet externe (voir « Exemple
réel » plus bas).

## Comment ça marche

### WorkItem Flow

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
séparée, pas de Final QA séparée — volontairement KISS/YAGNI. C'est le
**seul** workflow d'exécution de WorkItem que ce projet implémente : un
ancien pipeline plus lourd (`GOVERNED_FULL` — Reviewer indépendant en
lecture seule, QA Test Authoring isolée, Final QA séparée) a été retiré
avant la première release publique. Voir `ROADMAP.md`, section « WorkItem
Flow », pour le détail complet.

### Sélection des workers et fallback provider

Un `Worker` (`config/workers.yaml`) est une identité d'exécution avec un
`provider`/`backend` **fixes** et un ou plusieurs `ExecutionProfile`
(`model`/`reasoning_effort`/`quality_tier`/`cost_rank`). `Worker !=
Provider` : plusieurs workers peuvent partager le même provider sans
jamais créer plusieurs quotas — `QuotaManager` observe un seul état par
provider.

**Un quota provider épuisé ne doit jamais forcer une attente (`WAITING`)
tant qu'un autre worker compatible, sur un provider disponible, peut
continuer.** `DEV_B.worker_id != DEV_A.worker_id` est **obligatoire** ;
un provider différent est **préféré** mais jamais requis — un repli sur
un second worker du même provider reste toujours valide. Le pool actuel
déclare au moins 2 workers indépendants par provider participant.

## Architecture

```
Project (workspace, roadmap, MVP courant)
  ↓
MVPManager (WorkItems, dépendances, handoff durable, WorkItem Flow)
  ↓
WorkerSelector (capability > governance > provider availability > priority)
  ↓
RalphExecutionEngine (execution, hats, TDD, review Ralph)
  ↓
ExecutionRecord + HandoffRecord (audit + reprise durable)
```

En dessous de `WorkerSelector` :

```
Provider Adapters (probe() → ProviderState)
        ↓
    QuotaManager (multi-fenêtres, fraîcheur)
```

![Architecture d'orchestration IA multi-agents](docs/images/Architecture_orchestration_IA_multi-agents.png)

Voir `ROADMAP.md` — vision, état actuel du produit, architecture,
WorkItem Flow, invariants, validations réelles et propositions à voter —
pour la source de vérité fonctionnelle complète.

### Frontière moteur/librairie

`ai-dev-orchestrator` **est le moteur/librairie** ; le CLI `aido` décrit
ci-dessous en est la surface produit historique, désormais legacy/
transitoire, jamais le seul consommateur possible. Une application
embarquante (**AIDO**, ex. AIDO Code) pilote le même moteur via
`orchestrator.engine.OrchestratorEngine` et lui fournit son propre plan
de projet et son propre `WorkerRegistry` construit en Python — `aido.yaml`
n'est plus obligatoirement la source de configuration complète du produit
utilisateur. Voir [`docs/PROJECT_CONFIG.md`](docs/PROJECT_CONFIG.md),
« Engine/library boundary », et `ROADMAP.md` §13 (P13.5).

## Providers intégrés

| Provider | CLI/Backend | Statut |
|---|---|---|
| Anthropic | Claude Code | ✅ VALIDATED |
| OpenAI | Codex CLI | ✅ VALIDATED |
| Mistral | Vibe | ✅ VALIDATED — voir [`docs/VIBE_SPIKE.md`](docs/VIBE_SPIKE.md). Son signal de disponibilité reste `EXECUTION_PROBE_ONLY` (pas de fenêtre de quota observable), jamais fabriqué en pourcentage |
| DeepSeek | Claude Code, redirigé (`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`) | `IMPLEMENTED` — validation réelle `PENDING`, désactivé par défaut (`config/workers.yaml`, worker `dana`). Nécessite une vraie `DEEPSEEK_API_KEY` (facturé à la consommation, jamais requis pour les autres providers) |
| Kimi | Claude Code, redirigé (`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`) | `IMPLEMENTED` — validation réelle `PENDING`, désactivé par défaut (`config/workers.yaml`, worker `kai`). Nécessite une vraie `KIMI_API_KEY` (abonnement Kimi Code, jamais requis pour les autres providers) |

DeepSeek et Kimi réutilisent l'adaptateur Claude Code existant (même
binaire `claude`, redirigé vers leur point de terminaison compatible
Anthropic) plutôt qu'un second client HTTP indépendant : voir
`orchestrator.providers.deepseek_adapter`/`kimi_adapter` et `ROADMAP.md`
§7/§13. Les providers locaux, dont Ollama, font partie des propositions à
voter dans `ROADMAP.md` (section « Propositions à voter »), et ne sont ni
un travail approuvé, ni un travail en cours.

## Démarrage rapide

Prérequis :
- Python 3.10+ ;
- Ralph CLI (`ralph`) installé et sur le `PATH` ;
- Claude Code CLI et/ou Codex CLI et/ou Mistral Vibe CLI, authentifiés
  pour les providers que vous comptez utiliser réellement ;
- Pour DeepSeek/Kimi (optionnels, désactivés par défaut) : Claude Code CLI
  installé (même binaire, réutilisé) et une vraie clé (`DEEPSEEK_API_KEY`
  et/ou `KIMI_API_KEY`) exportée dans votre environnement, jamais dans un
  fichier du dépôt ;
- `git`.

Installation :

```bash
git clone https://github.com/yannickameur/ai-dev-orchestrator.git
cd ai-dev-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Tests offline (aucun appel provider réel, aucun quota consommé) :

```bash
pytest
```

### Utiliser la CLI `aido` (P1)

Une fois le package installé (ci-dessus), la commande `aido` est
disponible. Elle consomme le format de configuration public `aido.yaml`
(P12 — voir [`docs/PROJECT_CONFIG.md`](docs/PROJECT_CONFIG.md)) et
remplace le besoin d'un harnais Python pour l'usage normal.

```bash
cd ~/projects
aido init . roadmaplab
cd roadmaplab

# Éditer README.md / ROADMAP.md / aido.yaml avant tout développement.
aido validate
git add README.md ROADMAP.md aido.yaml
git commit -m "Define initial project"
aido run
aido status
```

`aido init <parent-path> <project-name>` crée un projet local avec
`README.md`, `ROADMAP.md`, `aido.yaml` et `.gitignore`, puis un dépôt Git
sur `main` et le commit `Initialize AIDO project`. Le répertoire cible
doit être absent ou vide ; un répertoire non vide est refusé sans
modification. `~` est développé ; le nom doit être un simple nom de
répertoire, jamais un chemin. Aucun `--force`.

`init` n'appelle aucun provider, worker, Ralph ou probe, ne crée aucun
état métier et ne lance aucun développement. Il ne crée aucun remote et
ne pousse rien. Les CLI providers/Ralph ne sont nécessaires qu'au moment
d'une exécution explicite avec `aido run`.

Le README décrit le contexte humain ; ROADMAP porte la vision produit ;
`aido.yaml` définit le MVP exécutable (objectif, critères d'acceptation,
WorkItems, commandes QA). Les TODO sont à remplir par l'utilisateur :
aucune synchronisation ni extraction automatique depuis ROADMAP.
Le registry existant est référencé, pas copié dans le nouveau projet :
`--workers-registry` explicite, sinon `config/workers.yaml` du répertoire
courant s'il existe, sinon configuration utilisateur globale créée ou
réutilisée (`$XDG_CONFIG_HOME/ai-dev-orchestrator/workers.yaml`, par défaut
`~/.config/ai-dev-orchestrator/workers.yaml`).

**Git absent ou en échec** : les fichiers sont conservés, `init` retourne
un code non nul et affiche l'étape en échec ainsi que les commandes de
reprise. Après installation de Git (et configuration de votre identité
Git si le commit l'exige), terminer manuellement :

```bash
cd /chemin/absolu/vers/roadmaplab
git init -b main
git add .
git commit -m "Initialize AIDO project"
```

AIDO n'installe jamais Git et ne change pas sa configuration globale.
Reprendre ensuite l'édition des trois fichiers et `aido validate`.

**Compatibilité / dépôt existant** : zéro ou un argument positionnel
conserve la création historique du seul `aido.yaml`, sans initialisation
Git ni README/ROADMAP :

```bash
# Depuis un dépôt existant :
aido init
# Ou avec chemin de configuration et registry explicites :
aido init ./aido.yaml --workers-registry /chemin/vers/workers.yaml
```

Les options historiques restent disponibles dans ce mode. Avec deux
arguments positionnels, le parent et le nom définissent le workspace et
le nom humain ; `--workspace` et `--project-name` sont donc refusés.
`--project-id`, `--workers-registry` et `--permission-mode` restent utilisables.

`aido run` est à la fois le démarrage **et** la reprise : le relancer
plus tard, contre le même `aido.yaml`, rouvre le même état persistant
(`project.state_dir`) et reprend via les mécanismes `WAITING`/
`RECOVERY_REQUIRED` existants — il n'existe pas de commande `aido resume`
séparée.

`scripts/run_external_project_pilot.py` reste un exemple réel antérieur à
P1 (jamais lancé via `pytest` — voir son propre docstring), gardé comme
preuve d'implémentation, pas comme surface produit.

**Permissions d'exécution réelle** : les tests offline ci-dessus ne
nécessitent aucun accès provider. Une exécution réelle de worker exige en
revanche des CLI providers déjà authentifiées et capables d'une exécution
non interactive. Le mode de permission d'exécution des workers
(`standard`/`unrestricted`) est un contrat explicite et project-controlled
— `aido.yaml`'s `execution.permission_mode`, traduit en flags CLI réels et
vérifiés à la frontière d'exécution (`RalphExecutionEngine`) — plutôt que
de dépendre implicitement de la configuration locale de chaque CLI. Pour
`unrestricted`, `aido run` imprime un avertissement visible avant toute
exécution réelle, sans jamais demander de confirmation interactive
(l'exécution non surveillée est un besoin produit). Voir
[`docs/PROJECT_CONFIG.md`](docs/PROJECT_CONFIG.md) pour le contrat complet
et `CONTRIBUTING.md`, « Real worker execution and permissions », pour la
mise en garde de sécurité — vous avez toujours besoin des CLI providers
installées et authentifiées vous-même, AIDO ne les gère jamais.

Pour un cas réel complet, narré et honnête (y compris une régression
découverte et corrigée), voir l'exemple ci-dessous.

## Exemple réel — Morpion Web 3D

Un vrai jeu de morpion (tic-tac-toe) web a été construit, de bout en
bout, par ce projet : roadmap → WorkItems → DEV A → DEV B → QA
déterministe (pytest + tests Node + régression navigateur Playwright) →
merge → tag — avec routage multi-provider réel, repli same-provider, et
une régression bien réelle découverte après un premier acceptance
incomplet.

Voir [`examples/morpion-web-3d/README.md`](examples/morpion-web-3d/README.md)
pour le récit complet, y compris pourquoi la reprise d'un WorkItem
`WAITING` doit utiliser un état persistant hors `/tmp`.

## État de projet / persistance

Deux choses distinctes :

- **Configuration déclarative** (`config/workers.yaml`, roadmap/MVP
  d'un projet cible) : décrit *ce qui peut être fait*, versionné,
  relu par un humain.
- **État d'exécution** (`ProjectStateStore`, `WaitStore`,
  `ExecutionStore`, `GitWorkItemStore`, `QARunStore`, ... — des SQLite
  dédiées) : décrit *ce qui a réellement été fait*, jamais reconstruit
  à la main.

Un WorkItem peut légitimement rester `WAITING` (ex. quota provider) et
doit pouvoir reprendre plus tard, par un tout autre process. Quand ce
délai peut dépasser la durée de vie d'un process (redémarrage machine
inclus), l'état d'exécution doit vivre **hors `/tmp`** — `/tmp` est
généralement vidé au redémarrage, ce qui n'est *pas* la même garantie
qu'une simple survie inter-process. Un chemin persistant typique :
`~/.local/state/ai-dev-orchestrator/projects/<project-id>/`, passé
explicitement aux constructeurs de store existants (aucun nouveau
framework de persistance). Une copie de travail purement jetable
(checkout temporaire pour reproduire un bug) peut en revanche rester
sous `/tmp` sans problème. Voir l'exemple Morpion ci-dessus pour le cas
réel qui a établi cette leçon.

## Soutenir le projet

AI Dev Orchestrator est développé en open source. Le sponsoring via
[GitHub Sponsors](https://github.com/sponsors/yannickameur) aide à financer les
abonnements IA, les appels API, les tests multi-modèles et le temps consacré au
développement, à la maintenance et à la documentation.

Les paliers donnent un repère simple de ce que votre soutien rend possible :

| Palier | Idée |
|---|---|
| ☕ **5 €/mois — Keep it running** | Contribuer aux petits coûts récurrents : API, tests ponctuels et outils annexes. |
| 🛠️ **10 €/mois — Build supporter** | Permettre davantage d'expérimentations multi-agents et de tests avec plusieurs providers. |
| 🤖 **25 €/mois — AI Junior Developer** | Aider à financer chaque mois l'équivalent d'un agent IA "junior" pour coder, tester, documenter et corriger. |
| 🚀 **100 €/mois — AI Senior Developer** | Aider à financer l'usage régulier des modèles de dernière génération pour les tâches complexes : architecture, refactoring, investigation et revue indépendante. |

Le sponsoring soutient le projet sans donner de contrôle particulier sur sa
roadmap ou ses décisions techniques.

## Development

Voir [`CONTRIBUTING.md`](CONTRIBUTING.md) : configuration de
l'environnement, tests, principes d'architecture (REUSE FIRST,
KISS/YAGNI, `LLM IS NOT ORACLE`, ...), règles Git.

## Security

Voir [`SECURITY.md`](SECURITY.md) pour signaler une vulnérabilité
(jamais via une issue publique).

## Contributing

Voir [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).

## Documentation

- [ROADMAP.md](ROADMAP.md) — vision, architecture, WorkItem Flow, propositions à voter — source de vérité fonctionnelle
- [docs/status.md](docs/status.md) — état factuel courant, court
- [MVP_SPEC.yaml](MVP_SPEC.yaml) — critères d'acceptation mesurables du MVP 0.1
- [docs/PROJECT_CONFIG.md](docs/PROJECT_CONFIG.md) — format de configuration public `aido.yaml` (P12) et mode de permission d'exécution des workers
- [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md) — étude des projets comparables
- [docs/SPIKE_RALPH.md](docs/SPIKE_RALPH.md) — résultats du spike Phase 0.5
- [docs/VIBE_SPIKE.md](docs/VIBE_SPIKE.md) — étude de faisabilité Mistral Vibe
- [examples/morpion-web-3d/README.md](examples/morpion-web-3d/README.md) — exemple réel complet
