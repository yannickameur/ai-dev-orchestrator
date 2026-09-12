# SPIKE Ralph — Phase 0.5 — Résultats

Date : septembre 2026  
Dépôt de test : `~/projects/ralph-spike` (jetable)  
Ralph version testée : `2.10.1` via `@ralph-orchestrator/ralph-cli`

## Résumé exécutif

Ralph Orchestrator est **mature pour réutilisation directe** comme moteur
d'exécution sous-jacent d'AI Dev Orchestrator. Nos briques différenciantes
sont **complémentaires**, pas en concurrence.

## REUSE — ce qu'on ne doit PAS reconstruire

### Boucle d'exécution (VALIDATED)

Ralph fournit immédiatement :
- Itération complète avec max_iterations, max_runtime_seconds
- Completion promises (signaux de fin structurés)
- Gestion de subprocess et timeouts
- Métriques par itération, event history, handoff
- Hats/rôles avec événements structurés entre hats
- Backends multiples, sélectionnables par hat
- Auto-commit Git sur landing (comportement observable)

**Décision** : `REUSE` as execution/workflow engine. Ne pas recoder l'event loop.

### Workflows pré-construits (VALIDATED)

Deux workflows prêts à l'usage :

#### builtin:code-assist

Hats :
- **Planner** : crée context.md, plan.md, progress.md ; utilise step-wave pour ne matérialiser que la vague courante
- **Builder** : RED → GREEN → REFACTOR ; travaille une tâche à la fois ; exécute tests/build/lint/typecheck natifs
- **Fresh-Eyes Critic** : explicitly "not the builder" ; refait les vérifications ; cherche bugs/régressions/scope/over-engineering ; teste via harness réel quand possible ; peut rejeter et renvoyer au Builder
- **Finalizer** : vérifie l'ensemble du prompt et pas seulement le dernier diff ; refait tests/build/lint/harness ; décide du flux suivant

Workflow:
```
build.start → Planner → tasks.ready → Builder → review.ready → Fresh-Eyes Critic
                                                                   ├─ review.rejected → Builder
                                                                   └─ review.passed → Finalizer
                                                                      ├─ queue.advance → Planner
                                                                      ├─ finalization.failed → Builder
                                                                      └─ LOOP_COMPLETE
```

#### builtin:review

Hats :
- **Code Reviewer** : revue adversariale initiale en profondeur limitée
- **Deep Analyzer** : analyse spécialisée des zones à risque identifiées
- **Review Closer** : synthèse finale et approbation/rejet global

**Décision** : `REUSE` pour TDD, task queue, review cycle, completion gates.  
Ne pas recoder Planner, Builder, Critic, Finalizer, review workflow.

### Backends par hat (VALIDATED)

Chaque hat peut déclarer :
```yaml
backend:
  type: codex  # override du backend global
```

Test réel effectué :
- Global backend : Claude
- Hat codex_probe : configuré avec Codex
- Quand Codex était en quota : itération silencieuse en ~1-3s, 0 token, 0 turn
- Itération suivante : Claude global a repris le prompt

**Décision** : `REUSE` pour la sélection par hat.  
Limitation observée : pas de fallback intelligent intégré si un backend échoue
(Ralph continue avec un autre hat, pas un fallback de même rôle à backend différent).

### Configuration model + reasoning_effort par hat (VALIDATED)

Un hat peut spécifier, en plus du backend, le modèle et l'effort de raisonnement :
```yaml
backend:
  type: codex
  model: gpt-5.6-terra
  reasoning_effort: low
```

Test réel effectué :
- Hat configuré avec `model=gpt-5.6-terra`, `reasoning_effort=low`
- La session Codex réelle (pas seulement la config Ralph) a confirmé
  `model=gpt-5.6-terra`, `effort=low`

**Conclusion** : `model` et `reasoning_effort` ne sont pas des détails internes
du backend — ils font partie de l'**identité du Worker**. Deux exécutions avec
le même provider/backend mais des `reasoning_effort` différents ne sont pas
interchangeables pour notre gouvernance (coût, latence, fiabilité de review).

**Décision** : `BUILD` — le modèle `Worker`/`Execution` doit porter `model` et
`reasoning_effort` (quand applicable) comme champs de premier ordre, pas
seulement dans la config Ralph.

## ⚠️ CRITICAL — Limitation détectée

**Comportement dangereux observé** : quand un hat échoue/ne produit pas ses événements attendus, Ralph continue simplement la boucle sans signaler l'erreur.

Exemple :
1. Hat Codex en quota → aucun événement métier
2. Ralph continue itération suivante
3. Utilise le backend global (Claude) de manière implicite

**Implication** : AI Dev Orchestrator doit **gérer explicitement la disponibilité des workers** avant de les lancer. Ne jamais faire confiance à Ralph pour un fallback de worker — c'est notre responsabilité.

Architecture requise :
```
Task → AI Dev Orchestrator Governance
         ├─ Resolve provider/model
         ├─ Check availability/quota
         ├─ Check governance rules
         └─ Only then launch Ralph hat
              If provider unavailable →
                - WAITING_RESET
                - Explicit alternative worker selection
                - No implicit fallback
```

## BUILD / ADAPT — ce qui doit être complété ou adapté

### Provider Adapters — Rôle précis (VALIDATED)

Les Provider Adapters ne sont PAS un moteur d'exécution concurrent de Ralph.

Leur responsabilité unique est la **découverte et normalisation de l'état
provider**.

Contrat :
```python
class ProviderAdapter:
    def probe() -> ProviderState:
        # Retourne l'état actuel du provider, jamais un ExecutionResult
        pass
```

`ProviderState` contient :
- provider (name, version)
- account/plan (si disponible)
- availability (ProviderAvailability)
- observed_at (timestamp de fraîcheur)
- quota_windows (liste de QuotaWindow)
- reset_credits (liste de ResetCredit, si applicable)
- metadata utile

`ProviderAvailability` :
- available (booléen)
- reason (si indisponible : quota, erreur, auth, etc.)
- observed_at

`QuotaWindow` :
- window_type (e.g., "primary_5h", "secondary_7d")
- used_percent ou similar (dépend du provider)
- reset_at (ISO timestamp)
- source (where observed from)
- observed_at (when queried)

`ResetCredit` :
- title (ex. "Full reset (Weekly + 5 hr)")
- status (available/consumed)
- auto_consume (toujours `false` par défaut — jamais de consommation automatique)

Ne pas faire retourner ExecutionResult à probe(). ExecutionResult appartient
à RalphExecutionEngine / couche exécution, pas à la découverte de quota.

### Claude Code — Télémétrie (VALIDATED)

Headless cli :
```bash
claude -p --model haiku --output-format stream-json --verbose ...
```

Stream retourne directement :
```json
{
  "session_id": "...",
  "model": "claude-haiku-4-5",
  "provider": "anthropic",
  "input_tokens": 123,
  "output_tokens": 456,
  "cache_read_input_tokens": 0,
  "cache_creation_input_tokens": 789,
  "thinking_tokens": 0,
  "context_window": 8000,
  "cost_usd": 0.000123,
  "terminal_reason": "end_turn",
  "rateLimitType": "five_hour",
  "unifiedWindows": {
    "five_hour": {
      "utilization": 0.45,
      "resetsAt": "2026-09-12T15:30:00Z"
    },
    "seven_day": {
      "utilization": 0.23,
      "resetsAt": "2026-09-19T08:00:00Z"
    }
  }
}
```

**Décision** : `REUSE` le stream-json natif, ne pas scraper le terminal.  
**Note** : `cost_usd` ici est list-price/théorique, pas une dépense réelle avec un abonnement.  
**Confirmé** : le flux expose aussi `rate_limit_event`, `modelUsage`, et les
compteurs cache/thinking tokens — directement exploitables pour peupler
`ProviderState` sans transformation supplémentaire.

### Codex — Télémétrie via app-server (VALIDATED)

Endpoint réel :
```
account/rateLimits/read
```

Réponse structurée :
```json
{
  "ordinaryUsageAllowed": false,
  "planType": "plus",
  "rateLimitReachedType": "rate_limit_reached",
  "rateLimits": {
    "primary": {
      "usedPercent": 100,
      "windowDurationMins": 300,
      "resetsAt": "2026-09-12T15:45:00Z"
    },
    "secondary": {
      "usedPercent": 16,
      "windowDurationMins": 10080,
      "resetsAt": "2026-09-19T10:00:00Z"
    }
  },
  "rateLimitResetCredits": {
    "availableCount": 1,
    "credit": {
      "title": "Full reset (Weekly + 5 hr)",
      "status": "available"
    }
  },
  "credits": {
    "balance": "0"
  }
}
```

Signal clé : `ordinaryUsageAllowed` (booléen), pas une déduction de usedPercent.

API pour consommer un reset credit :
```
account/rateLimitResetCredit/consume
```

**Politique sur reset credits** : jamais consommer automatiquement par défaut.  
Configuration future possible : `reset_credits.auto_consume: false`.

**Décision** : `REUSE` app-server pour la télémétrie.  
Ne pas scraper `/status` ou parser des textes d'erreur non structurés.

### Quotas multi-fenêtres (VALIDATED)

Modèle observé chez Claude ET Codex : **plusieurs fenêtres de quota simultanées**.

Claude : `five_hour` + `seven_day`  
Codex : `primary` (5h rolling / 300 min) + `secondary` (7j / 10080 min)

**Modèle requis** :
```yaml
provider: codex
windows:
  - type: primary_5h
    used_percent: 100
    reset_at: "2026-09-12T15:45:00Z"
    
  - type: secondary_7d
    used_percent: 16
    reset_at: "2026-09-19T10:00:00Z"
```

Pas de simple `reset_at` pour le worker — il y en a plusieurs.

**Décision** : `BUILD` le modèle QuotaWindow multi-fenêtres.  
Ne pas supposer une seule fenêtre par provider.

### Worker selection order (VALIDATED via observation)

L'ordre d'évaluation observé chez Ralph suggère (sans surprise) : essayer
un hat, c'est tout. Pas de fallback intégré de worker.

AI Dev Orchestrator doit implémenter explicitement l'ordre :

```
1. Capability match (peut-il faire le job ?)
2. Governance rules (politique acceptée ?)
3. Quota availability (ressource disponible ?)
4. Cost tier (local > subscription > paid)
```

**Décision** : `BUILD` le WorkerSelector complet.

### Author ≠ Reviewer enforcement (CRITICAL)

Ralph permet de configurer des hats différents avec des backends différents.

Exemple :
- Builder → Claude
- Critic → Codex

Mais **rien n'empêche structurellement** une configuration :
- Builder → Claude
- Critic → Claude

La séparation est **logique** (le rôle d'un Critic indique "you are not the
builder"), pas **technique**.

Pour AI Dev Orchestrator : la politique doit être **appliquée**, pas seulement
suggérée.

**Décision** : `BUILD` une règle de gouvernance qui interdit à une même
`(provider, model)` de valider son propre travail.

```python
# Pseudo-code
if execution.author_provider == execution_reviewer.reviewer_provider \
   and execution.author_model == execution_reviewer.reviewer_model:
    raise GovernanceViolation("Author cannot be their own reviewer")
```

#### Test réel exécuté (VALIDATED)

Chaîne complète exécutée avec deux providers réellement différents :

**Alice — Auteur Claude**
- provider: anthropic / backend: Claude Code / model: claude-haiku-4-5

Alice produit un fichier contenant un bug volontaire :
```python
def add(a, b):
    return a - b
```
(la spécification imposait `a + b`). Alice publie l'événement `review.ready`.

**Victor — Reviewer Codex**
- provider: openai / backend: Codex / model: gpt-5.6-terra / reasoning_effort: high

Victor lit le fichier produit par Alice, détecte le bug, et publie :
```
review.rejected -> add(a,b) subtracts instead of adding
```
Victor n'a pas corrigé le fichier lui-même (pas d'auto-remédiation par le reviewer).

**Conclusion** : la règle Author != Reviewer avec des providers différents est
**techniquement validée** avec Ralph à l'exécution — un reviewer sur un
provider/modèle distinct détecte réellement un bug introduit par un auteur sur
un autre provider. Ceci valide le pattern d'exécution, pas encore notre
garantie de politique : Ralph ne l'impose toujours pas structurellement (voir
ci-dessus), c'est notre gouvernance qui doit l'appliquer.

### Handoff événementiel entre hats (VALIDATED)

Chaînage observé dans le test Author≠Reviewer ci-dessus :

```
work.start
  → Alice / Claude (auteur)
  → review.ready
  → Victor / Codex (reviewer)
  → review.rejected
```

Confirme un handoff propre entre hats à backends/providers différents,
déclenché par des événements applicatifs explicites — pas de polling ni
d'état partagé implicite.

### Noms lisibles des workers/hats (VALIDATED)

Ralph expose un champ `name` par hat/persona pour l'affichage ("Alice —
Auteur Claude", "Victor — Reviewer Codex").

**Décision pour notre modèle Worker** : séparer explicitement :
- `worker_id` : identifiant stable et technique (ex. `codex_reviewer_01`)
- `display_name` : nom lisible humain (ex. `Victor`), attribut d'affichage
  uniquement, jamais utilisé pour une décision de gouvernance

```yaml
worker_id: codex_reviewer_01
display_name: Victor
provider: openai
backend: codex
model: gpt-5.6-terra
reasoning_effort: high
role: reviewer
```

### Événements réservés Ralph (OBSERVED / VALIDATED)

`task.start` et `task.resume` sont **réservés au coordinateur Ralph** — ne
jamais les utiliser comme triggers custom dans nos configurations de hats.

Utiliser des événements applicatifs dédiés à notre domaine, par exemple :
`work.start`, `review.ready`, `review.approved`, `review.rejected`, `probe.done`.

**Décision** : appliquer cette contrainte de nommage strictement dans nos
configs Ralph (hats YAML) — un événement custom nommé `task.*` peut entrer en
collision avec le coordinateur.

## Télémétrie et diagnostics

### Ralph metrics (PARTIALLY RELIABLE)

Deux problèmes de fiabilité distincts observés :

**1) Codex backend indisponible (quota épuisé)**
- iteration.summary showed 0 tokens, 0 turns, cost 0
- loop.terminate had reason "max_iterations" (misleading)
- Internal exit code was 2 (not standardized across providers)

**2) Codex backend disponible, exécution réelle (OBSERVED — cas distinct)**
- Ralph a enregistré `input_tokens=0`, `output_tokens=0`, `num_turns=0`,
  `cost_usd=0` pour des exécutions Codex réelles
- Les sessions Codex natives confirment une exécution réelle en parallèle
- La télémétrie Ralph pour Codex n'est donc **pas fiable même quand
  l'exécution réussit** — ce n'est pas limité au cas quota-épuisé

**Conclusion** : Ralph fournit des signaux d'exécution mais pas de diagnostic
de quota fiable, y compris en cas de succès. Le QuotaManager doit utiliser
les données natives provider (stream-json Claude, app-server Codex), jamais
les métriques Ralph comme source canonique.

**Décision** : `REUSE` Ralph pour les signaux d'exécution (events, handoff).  
`BUILD` un adapter léger par provider (Claude stream-json, Codex app-server)
pour la télémétrie de quota — ne jamais utiliser les métriques Ralph comme
source d'usage/coût.

### Completion promise — fiabilité limitée (OBSERVED)

Avec les probes Codex, l'événement métier `LOOP_COMPLETE` a bien été produit
par le workflow, mais Ralph a terminé la boucle avec :
```
reason=max_iterations
```
au lieu de refléter la complétion réelle signalée par l'événement.

**Conclusion** : ne pas considérer la completion promise textuelle/`reason`
de fin de boucle Ralph comme source de vérité métier pour notre gouvernance.
Les événements métier explicites que nous publions nous-mêmes
(`review.rejected`, `review.approved`, `probe.done`) sont plus fiables que
l'état de terminaison rapporté par Ralph.

**Décision** : `BUILD` notre propre lecture des événements métier applicatifs
plutôt que de dépendre du `reason` de fin de boucle Ralph.

### Permissions / sandbox Codex (NOT VALIDATED)

⚠️ Ne PAS conclure que Ralph ignore `--sandbox`.

L'environnement Codex local utilisé pour le spike était volontairement
configuré avec `approval_policy=never`, `sandbox_mode=danger-full-access`,
`permission_profile` désactivé. Cette configuration ne permet pas d'établir
si un override de sandbox par Ralph est effectivement appliqué ou non — le
test n'isole pas cette variable.

**Classification** : `NOT VALIDATED` — non bloquant pour la sortie de
Phase 0.5, mais à re-tester avec un environnement Codex à permissions
restreintes avant toute hypothèse de sécurité en Phase 1+.

### Command-level LLM consumption (OBSERVED)

Commandes comme :
```bash
ralph hats graph -H builtin:review
```

ont déclenché une invocation **réelle et coûteuse** de Claude Sonnet 5 pour
générer un graphe au format demandé.

D'autres commandes (`ralph hats list`, `ralph init --list-presets`) sont
statiques.

**Conclusion** : ne pas supposer qu'une commande de diagnostic est gratuite.

**Décision** : documenter explicitement quelles commandes Ralph consomment
des LLM (futures références à établir).

## Architecture résultante

```
AI Dev Orchestrator (governance, quotas, routing)
         ↓
  Provider Adapters (Claude stream-json, Codex app-server, Ollama)
         ↓
  Worker Selector (capability > governance > quota > cost)
         ↓
  Ralph Orchestrator (execution engine)
         ├─ builtin:code-assist (Planner→Builder→Critic→Finalizer)
         ├─ builtin:review (Reviewer→Analyzer→Closer)
         └─ per-hat backend selection
```

## Prochaines étapes

1. Créer des Provider Adapters légers pour Claude, Codex (potentiellement Ollama)
2. Implémenter NormalizedQuotaState multi-fenêtres
3. Implémenter Worker Selector avec ordre : capacity > governance > quota > cost
4. Intégrer Ralph comme `RalphExecutionEngine()`
5. Tester l'orchestration complète : task → availability check → adapter → Ralph → result

## Décisions révisées pour docs/ECOSYSTEM.md

| Composant | Décision antérieure | Résultat spike | Décision révisée |
|---|---|---|---|
| Task scheduler / event loop | INSPIRE | Ralph validé complet | `USE` Ralph ; `ADAPT` pour gouvernance |
| Planner/Builder/Critic/Finalizer | BUILD | Ralph validé complet | `REUSE` complet (code-assist) |
| Review workflow | BUILD | Ralph validé (review preset) | `REUSE` complet (review preset) |
| Backend par hat | ADAPT | Validé fonctionnement | `REUSE` (pas besoin d'adaptation) |
| Quotas Claude/Codex | BUILD | APIs natives validées | `REUSE` stream-json + app-server |
| QuotaWindow multi-fenêtres | BUILD | Architecturé, multi-window validé | `BUILD` (Ralph ne le fait pas) |
| Author ≠ Reviewer | BUILD | Séparation logique Ralph, pas technique | `BUILD` (gouvernance stricte requise) |
| Worker selection | BUILD | Order not automated | `BUILD` (WorkerSelector) |
| Recovery après crash | INSPIRE | Partiel (Ralph handoff) | `ADAPT` (Ralph handoff + notre reconciliation) |
| Author ≠ Reviewer cross-provider | à valider | Chaîne réelle testée (Claude author + Codex reviewer), bug détecté et rejeté | Pattern d'exécution `VALIDATED` ; garantie de politique reste `BUILD` |
| Worker identity (model/reasoning_effort) | non prévu explicitement | Codex model+effort confirmés dans la session réelle | `BUILD` : champs de premier ordre sur Worker/Execution |
| Completion promise / loop reason | supposé fiable | LOOP_COMPLETE produit mais reason=max_iterations | `BUILD` : se fier aux events métier, pas au reason Ralph |
| Codex sandbox/permissions override | supposé sûr par défaut | Environnement dev non isolé, test non concluant | `NOT VALIDATED`, non bloquant Phase 0.5 |

## Résumé ultra-concis pour ROADMAP.md

Phase 0.5 DONE:
✓ Ralph 2.10.1 validé comme moteur d'exécution  
✓ Code-assist workflow opérationnel  
✓ Review workflow opérationnel  
✓ Claude/Codex télémétrie via APIs natives  
✓ Multi-window quotas architecturés  
✓ Author≠Reviewer = BUILD (gouvernance stricte)  
✓ Worker selection = BUILD (WorkerSelector)  
✓ Provider adapters = BUILD (légers, normalisés)  
✓ Author≠Reviewer cross-provider = VALIDATED à l'exécution (Claude author, Codex reviewer, bug détecté)  
✓ Worker identity = BUILD model + reasoning_effort comme champs de premier ordre  
✓ Événements réservés Ralph (task.start/task.resume) identifiés — utiliser events applicatifs custom  
⚠ Completion promise Ralph = OBSERVED non fiable seule — se fier aux events métier  
⚠ Télémétrie Codex dans Ralph = OBSERVED non fiable même en succès — QuotaManager doit lire le provider natif  
⚠ Permissions/sandbox Codex = NOT VALIDATED, non bloquant  

Prochaine phase : Phase 1 MVP 0.1 (Provider Adapters + Worker Selector +
QuotaManager + Ralph integration).

## Addendum — Slice 6 (RalphExecutionEngine), nouvelles observations réelles

Ces deux points n'étaient pas apparus lors du spike initial (Phase 0.5) et
ont été découverts en intégrant `ralph run` programmatiquement pour la
première fois (smoke test réel unique, backend Claude Haiku) :

- **`--no-tui` est incompatible avec `--autonomous`/`-a`** : `ralph run`
  refuse la combinaison (`error: the argument '--no-tui' cannot be used
  with '--autonomous'`). `-a` seul suffit à forcer le mode headless pour un
  subprocess dont le stdout n'est pas un TTY (Ralph bascule déjà tout seul
  en mode autonome dans ce cas, comme observé aussi dans les logs de
  diagnostic du spike initial : "Interactive mode requested but stdout is
  not a TTY, falling back to autonomous").
- **Un hat custom sans `description` est rejeté** par la validation de
  config Ralph (`Hat 'worker' is missing required 'description' field`).
  Les fichiers hats du spike (`hats-author-review.yml`,
  `hats-backend-spike.yml`) en avaient tous une ; ce champ doit être généré
  systématiquement pour toute config hats produite programmatiquement.
