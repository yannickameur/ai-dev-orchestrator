# AI Dev Orchestrator — Technical Audit

Audit externe (méthode multi-agents, cinq analyses parallèles sur le
code réel — jamais la documentation seule), demandé par le mainteneur.
Ce document est la preuve factuelle complète derrière la remédiation
P13.4 (voir `ROADMAP.md`, §13). Les corrections effectivement apportées,
et leur statut, sont documentées dans `ROADMAP.md` — pas ici ; ce fichier
reste le rapport d'audit original, non réécrit après coup.

## Audit scope

- **Repository**: `yannickameur/ai-dev-orchestrator`
- **Branch**: `main`
- **Audited SHA**: `75b3ef5e5d77f2fb0e516e2b4722dfcc1b8c2fbd` (refresh confirmé : `git fetch` + `git pull --ff-only` avant audit, HEAD local = `origin/main`, working tree propre — identique au SHA attendu par la demande d'audit)
- **Date**: 2026-09-23
- **Fichiers réellement inspectés** (lecture de code, pas de la doc) : `worker_selector.py`, `worker_registry.py`, `quota_manager.py`, `adaptive_execution.py`, `complexity_estimation.py`, `providers/{adapter,contracts,claude_code_adapter,codex_adapter,deepseek_adapter,kimi_adapter,mistral_vibe_adapter}.py`, `config/workers.yaml`, `ralph_execution_engine.py`, `vibe_ralph_bridge.py`, `git_governance.py`, `execution_policy.py`, `recovery.py`, `qa.py`, `internal_qa_engine.py`, `qa_protection.py`, `validation.py`, `qa_knowledge.py`, `execution_store.py`, `project_state.py`, `project_runtime.py`, `wait.py`, `mvp_manager.py`, `release_manager.py`, `release.py`, `engine.py`, `cli.py`, `project_config.py`, `approval.py`, `handoff.py`, `pyproject.toml`, `resources/__init__.py`. Total `src/orchestrator/` : 19 664 lignes.
- **Documentation comparée au code** : README.md, ROADMAP.md, docs/status.md, docs/QA_STRATEGY.md, docs/PROJECT_CONFIG.md, docs/ADAPTIVE_EXECUTION.md, docs/SPIKE_RALPH.md, docs/VIBE_SPIKE.md.
- **Tests** : suite complète exécutée réellement (`python3 -m pytest -q`) → **1238 passed, 1 warning, 0 failed, 0 skipped** en 22.93s (41 fichiers de tests, baseline annoncée confirmée). Échantillonnage ciblé approfondi sur les tests des zones critiques (worker_selector, git_governance/attribution, qa/internal_qa_engine/qa_protection, recovery/wait, ralph_execution_engine, engine, cli).
- **Méthode** : 5 analyses parallèles couvrant chacune un sous-ensemble disjoint des 22 sections demandées, toutes en lecture seule (aucun fichier modifié, aucun `aido run`, aucun appel provider réel), suivies d'une synthèse et de vérifications croisées manuelles.

## Executive summary

Le moteur est structurellement sain : le flux WorkItem → WorkerSelector → DEV A → DEV B → QA déterministe → merge gouverné → état terminal est réellement implémenté tel que documenté, avec une exclusion DEV A/DEV B non contournable, un merge fast-forward-only sans opération destructive, une attribution Git worker à double défense avec audit fail-closed testé de façon adversariale, et une couche de quota provider-level jamais fabriquée. 1238/1238 tests passent réellement. Un défaut de gouvernance réel et non trivial a été trouvé : le mécanisme de protection des tests de régression (`qa_protection.py`) est entièrement implémenté et testé mais **jamais câblé** dans le chemin de production réel (`ProjectRuntime.bootstrap()`) — un worker peut aujourd'hui affaiblir un test existant sans être détecté. Trois défauts MEDIUM concrets s'ajoutent : perte silencieuse de fichiers non suivis supprimés par un worker (déjà observée en pilote réel), une capture d'exception trop large (`except Exception`) qui masque une corruption d'état réelle sous `NOT_INITIALIZED` (retrouvée indépendamment par deux analyses distinctes, donc à haute confiance), et une duplication de logique CLI/Engine sur `status`. Aucun défaut CRITICAL. Aucun défaut ne touche le périmètre M2 (sessions AIDO Code).

## Architecture verdict

Faits vérifiés par lecture directe du code (pas de la documentation) :

- Le flux gouverné réel est bien `WorkerSelector.select()` → exécution DEV A (`RalphExecutionEngine`) → sélection DEV B avec exclusion inconditionnelle de l'auteur → exécution DEV B → `evaluate_qa_verdict` (QA déterministe, `engine_reported_status` jamais lu comme source de vérité) → `GitGovernanceService` (fast-forward-only) → état terminal WorkItem.
- `WorkerSelectionPolicy.require_distinct_worker_for_review` est *pinné* à `True` dans `__post_init__` : une tentative de le désactiver lève `ValueError`. Ce n'est pas une valeur par défaut modifiable.
- Le quota est provider-level partout où c'est vérifiable (`QuotaManager`, `_diagnose_candidate_providers`), jamais dupliqué par worker.
- `RalphExecutionEngine` ne contient aucune logique de sélection de worker ni de scheduling ; le verdict SUCCEEDED/FAILED vient exclusivement d'événements matchés, jamais de l'exit code du sous-processus.
- `MVPManager` et `ReleaseManager` sont deux composants distincts : seul `ReleaseManager.attempt_release` fait transiter un MVP RUNNING→VALIDATING→RELEASED ; `MVPManager` ne le fait jamais. Cette séparation est explicite et documentée (ROADMAP.md §P13.3), confirmée correcte en code — ce n'est pas une ambiguïté d'API constatée dans le code lui-même.
- La façade publique `OrchestratorEngine` expose exactement les 7 méthodes attendues (`open/validate/workers/probe_workers/status/run/close`), avec DTOs `frozen`/`slots` sérialisables, sans fuite de Store/SQLite.
- Le mécanisme anti-faux-PASS (`ValidationEnvironmentEvidence`, introduit après le défaut réel documenté dans `docs/QA_STRATEGY.md` §8.3) est réel et actif pour les commandes Python, mais structurellement aveugle pour toute stack non-Python (voir AUD-5).
- Le mécanisme de protection des tests de régression (`qa_protection.py`) est réel, unitairement testé, mais **non actif** dans le chemin de production réel (voir AUD-1) — c'est l'écart le plus significatif entre ce que le code *peut* faire et ce qu'il *fait* réellement aujourd'hui en gouvernance.

## Findings

| ID | Severity | Area | Short description | M2 blocker |
|---|---|---|---|---|
| AUD-1 | HIGH | QA governance | `qa_protected_paths` jamais câblé dans le bootstrap réel : protection anti-affaiblissement des tests inerte en production | NO |
| AUD-2 | MEDIUM | Git / Ralph | Suppression de fichier non suivi par un worker : non détectée, non auditée | NO |
| AUD-3 | MEDIUM | Engine / CLI | `except Exception` masque une corruption réelle sous `NOT_INITIALIZED` (confirmé indépendamment par 2 analyses) | NO |
| AUD-4 | MEDIUM | Engine / CLI | `aido status` réimplémente la logique de `OrchestratorEngine.status()` au lieu de la réutiliser | NO |
| AUD-5 | MEDIUM | QA determinism | Capture d'environnement anti-faux-PASS limitée aux stacks Python ; régression possible pour un futur MVP non-Python | NO |
| AUD-6 | LOW | Providers | Statut inconnu Anthropic mappé à `QUOTA_EXHAUSTED` au lieu de `UNKNOWN` (diagnostic uniquement) | NO |
| AUD-7 | LOW | Config / security posture | `project.id` non restreint, influence la construction de `state_dir` | NO |
| AUD-8 | LOW | Tests | Kill/timeout réel de Ralph jamais exercé avec un vrai sous-processus | NO |
| AUD-9 | LOW | Documentation drift | `docs/status.md` : compte de tests et date obsolètes (1234 vs 1238 réels) | NO |
| AUD-10 | LOW | Persistence | `Project.current_mvp_id` persisté mais jamais lu par une décision (état mort) | NO |
| AUD-11 | LOW | Tests | Aucun test bout-en-bout committé pour la transition multi-MVP séquentielle dont dépend la planification M2 | NO |

### AUD-1 — `qa_protected_paths` jamais câblé dans le bootstrap réel

- **Severity**: HIGH · **Confidence**: HIGH
- **Area**: QA déterministe / gouvernance
- **Files**: `src/orchestrator/qa_protection.py` ; `src/orchestrator/mvp_manager.py:311,351,823` (`MVPManager.__init__`, `_capture_protected_baseline`) ; `src/orchestrator/project_runtime.py:251` (seul site réel de construction de `MVPManager`) ; `src/orchestrator/project_config.py` (absence totale du champ dans le schéma)
- **Observed behavior**: `qa_protection.py` implémente un invariant central documenté ("un test de régression existant est un actif protégé, ne peut être supprimé/affaibli/skip sans autorisation versionnée") via un hash SHA-256 comparé avant/après. Mais `MVPManager.__init__(qa_protected_paths: tuple[str, ...] = ())` — et `ProjectRuntime.bootstrap()`, seul site réel qui construit `MVPManager` pour `aido run`/`OrchestratorEngine.run()`, ne passe jamais ce paramètre. Il reste au défaut `()`. Aucun champ correspondant n'existe dans `aido.yaml`/`ProjectConfig`/`docs/PROJECT_CONFIG.md` : le mécanisme n'est aujourd'hui même pas configurable.
- **Why it matters**: viole directement l'invariant demandé en section 3 de cet audit ("un worker ne doit jamais décider lui-même que sa QA est valide"). Un DEV A ou un DEV FIX peut affaiblir/supprimer une assertion d'un test existant ; tant que le test modifié sort avec exit code 0, rien ne le détecte aujourd'hui dans un run gouverné réel.
- **Concrete reproduction scenario**: sur n'importe quel projet gouverné actuel, un DEV FIX qui édite `tests/test_x.py` pour retirer une assertion gênante passe QA sans FAIL — `qa_protection.compare_protected_test_baseline` ne s'exécute jamais car `qa_protected_paths` est vide.
- **Existing test coverage**: `tests/test_qa_protection.py` et `tests/test_mvp_manager_workitem_flow.py::TestProtectedTestIntegration` couvrent le mécanisme unitairement en passant `qa_protected_paths` explicitement — aucun test n'exerce le chemin réel `ProjectRuntime.bootstrap()` pour vérifier qu'il est actif en production.
- **Recommended correction**: ajouter un champ `qa_protected_paths` au schéma `aido.yaml`/`ProjectConfig` (ou dériver automatiquement la liste depuis les fichiers de test existants au `base_sha`), et le propager dans `ProjectRuntime.bootstrap()` jusqu'à `MVPManager`.
- **Is it blocking M2? NO** — défaut côté moteur, aucune dépendance M2 (sessions AIDO Code). À corriger avant d'onboarder tout futur MVP réel non trivial.

### AUD-2 — Suppression de fichier non suivi par un worker : non détectée, non auditée

- **Severity**: MEDIUM · **Confidence**: HIGH
- **Area**: Ralph execution / accès fichiers worker
- **Files**: `src/orchestrator/ralph_execution_engine.py` (classe `RalphExecutionEngine` entière) ; `src/orchestrator/git_governance.py:158-176` (`WorkingTreeStatus.untracked`) ; `:1088-1131` (`prepare_work_item`)
- **Observed behavior**: aucun mécanisme, ni dans `RalphExecutionEngine` ni dans `GitGovernanceService`, ne capture l'inventaire des fichiers non suivis préexistants avant l'exécution d'un worker, ni ne le recompare après. `WorkingTreeStatus.untracked` n'est calculé qu'au moment de `prepare_work_item` (vérification de propreté), jamais utilisé comme baseline de comparaison post-exécution. Git lui-même ne garde aucune trace d'un fichier non suivi supprimé.
- **Why it matters**: un tel événement a déjà été observé lors d'un pilote réel. Le risque n'est pas la gouvernance/le merge (les fichiers non suivis n'entrent jamais dans la branche gouvernée) mais la **perte silencieuse de données locales**, sans aucune trace dans `ExecutionRecord`, aucun événement, aucun avertissement.
- **Concrete reproduction scenario**: créer un fichier non suivi dans le workspace gouverné avant exécution ; faire exécuter un worker dont l'action le supprime sans le committer ; `ExecutionResult` ne signale rien d'anormal, `git status` après coup ne le montre plus jamais.
- **Existing test coverage**: aucune — ni `test_ralph_execution_engine.py` ni `test_git_governance.py` ne couvrent la disparition d'un fichier non suivi préexistant.
- **Recommended correction**: capturer un inventaire (chemins + hash) des fichiers non suivis juste avant l'exécution et comparer après ; exposer toute disparition comme fait observable dans `ExecutionResult`/logs — détection, pas nécessairement blocage automatique (un worker peut légitimement nettoyer ses propres artefacts temporaires). Ne pas transformer ceci en erreur bloquante sans arbitrage produit, cela romprait des flux légitimes.
- **Is it blocking M2? NO**.

### AUD-3 — `except Exception` masque une corruption réelle sous `NOT_INITIALIZED`

- **Severity**: MEDIUM · **Confidence**: HIGH (confirmé indépendamment par 2 analyses distinctes, lignes identiques)
- **Area**: Engine API / CLI status path
- **Files**: `src/orchestrator/engine.py:417-423` (`_read_status`) ; `src/orchestrator/cli.py:328-333` (`_print_project_status`)
- **Observed behavior**: les deux implémentations indépendantes du chemin "status" encapsulent `reader.project_store.get_project(project_id)` dans un `except Exception:` générique qui retourne systématiquement `NOT_INITIALIZED`/`initialized=False`. Or `get_project()` (`project_state.py:481-487`) ne lève réellement qu'un seul type typé : `UnknownProjectError`.
- **Why it matters**: `status()` est documenté "strictly read-only" et consommé tel quel par AIDO Code (`/status`). Toute autre exception réelle (corruption SQLite, erreur de décodage, disque plein, erreur de permission) est aujourd'hui silencieusement réinterprétée comme "projet jamais initialisé", invitant l'utilisateur à relancer `aido run` sur un état déjà corrompu au lieu de voir l'erreur réelle — contraire à la convention fail-closed appliquée ailleurs dans le projet (QA `ValidationEnvironmentEvidence`, `WorkerCommitIdentityMismatchError`).
- **Concrete reproduction scenario**: corrompre/tronquer le fichier SQLite du `state_dir` d'un projet déjà bootstrappé, puis lancer `aido status` ou appeler `OrchestratorEngine.status()` → `NOT_INITIALIZED` au lieu d'un diagnostic explicite.
- **Existing test coverage**: aucune — `tests/test_engine.py`/`tests/test_cli.py` ne testent que le cas "projet réellement absent" (`UnknownProjectError`), jamais une exception distincte simulant une corruption.
- **Recommended correction**: remplacer `except Exception:` par `except UnknownProjectError:` aux deux endroits ; laisser toute autre exception se propager (déjà enveloppée en `EngineConfigError`/`EngineError` plus haut dans la pile, cohérent avec `docs/ENGINE_CONTRACT.md`).
- **Is it blocking M2? NO** — pertinent si une future UX M2 d'AIDO Code affiche ce statut lors d'un resume sur un projet corrompu, mais n'est pas un bloqueur du contrat M2 lui-même.

### AUD-4 — `aido status` duplique la logique d'`OrchestratorEngine.status()`

- **Severity**: MEDIUM · **Confidence**: HIGH
- **Area**: CLI / Engine duplication
- **Files**: `src/orchestrator/cli.py:318-393` (`_print_project_status`) vs `src/orchestrator/engine.py:417-485` (`_read_status`/`_work_item_snapshot`)
- **Observed behavior**: `aido status` (CLI) n'utilise pas `OrchestratorEngine.status()` — il réimplémente indépendamment toute la traversée (project → mvp → work_items → executions → waits → résolution du display_name) directement sur `ProjectStatusReader`. C'est une seconde implémentation complète de la même logique que celle de la façade Engine.
- **Why it matters**: contredit le principe "REUSE FIRST" du docstring d'`engine.py`. La preuve de divergence existe déjà : AUD-3 est présent **identiquement** dans les deux copies (signe de copier-coller, pas de réutilisation). Un futur correctif appliqué à une seule des deux copies laisserait une divergence de comportement CLI vs Engine.
- **Concrete reproduction scenario**: corriger AUD-3 uniquement dans `engine.py` sans toucher `cli.py` (ou l'inverse) — `aido status` et un futur frontend basé sur `OrchestratorEngine.status()` afficheraient alors des diagnostics différents pour le même état corrompu.
- **Existing test coverage**: les deux chemins ont des tests séparés qui ne vérifient jamais l'équivalence des deux rendus.
- **Recommended correction**: faire de `_cmd_status` un simple consommateur de `OrchestratorEngine.status()` (comme `--probe` le fait déjà pour `probe_workers()`), avec juste une couche de rendu texte au-dessus.
- **Is it blocking M2? NO**.

### AUD-5 — Capture d'environnement anti-faux-PASS limitée aux stacks Python

- **Severity**: MEDIUM · **Confidence**: HIGH
- **Area**: QA déterministe — limite de couverture
- **Files**: `src/orchestrator/validation.py:685-786` (`_resolve_python_executable`, `_default_environment_probe`)
- **Observed behavior**: la capture d'environnement (correctif du défaut réel documenté dans `docs/QA_STRATEGY.md` §8.3) ne produit `python_version`/`packages_fingerprint` que si `argv[0]` est reconnu comme Python. Pour toute autre stack (`npm`, `cargo`, `go`, binaire quelconque), seuls `resolved_executable` + `PATH` brut sont capturés — aucune empreinte de dépendances.
- **Why it matters**: pour un futur MVP non-Python, le défaut exact que ce mécanisme a été construit pour détecter (même SHA + même commande + environnement différent → faux PASS) resterait totalement indétectable.
- **Concrete reproduction scenario**: projet Node avec `argv=("npm","test")` ; un `npm install` d'une dépendance cassée entre deux tentatives QA à SHA identique ne changerait aucun champ de `ValidationEnvironmentEvidence`.
- **Existing test coverage**: `tests/test_validation.py::TestEnvironmentEvidence::test_non_python_command_has_no_python_fields` — comportement testé donc délibéré, pas un bug caché, mais **non documenté comme limite** dans `docs/QA_STRATEGY.md`.
- **Recommended correction**: documenter explicitement cette limite ; envisager une empreinte générique optionnelle (hash d'un lockfile connu, sortie de `<argv0> --version`) avant d'onboarder un MVP non-Python réel.
- **Is it blocking M2? NO**.

### AUD-6 — Statut Anthropic inconnu mappé à `QUOTA_EXHAUSTED` au lieu d'`UNKNOWN`

- **Severity**: LOW · **Confidence**: MEDIUM
- **Area**: Providers / diagnostics
- **Files**: `src/orchestrator/providers/claude_code_adapter.py:249-262` (`_availability_from_status`)
- **Observed behavior**: seuls `"allowed"` et `"rejected"` sont des valeurs observées (spike + tests). Le code traite tout ce qui n'est pas exactement `"allowed"` comme `QUOTA_EXHAUSTED`, y compris une future valeur inconnue.
- **Why it matters**: le booléen `available=False` reste correct (fail-closed, aucun impact sur la sélection — `WorkerSelector` traite toute raison d'indisponibilité identiquement). L'impact est purement diagnostique : logs/erreurs afficheraient "quota_exhausted" pour une cause qui n'en est pas une.
- **Concrete reproduction scenario**: Anthropic introduit `rate_limit_info.status = "service_degraded"` → adapter renvoie `QUOTA_EXHAUSTED` au lieu de `UNKNOWN`.
- **Existing test coverage**: `"allowed"`/`"rejected"` couverts ; aucun test pour un 3e statut inconnu.
- **Recommended correction**: mapping explicite avec fallback `UNKNOWN`, cohérent avec le pattern déjà utilisé dans `mistral_vibe_adapter._classify_failure_reason` et `codex_adapter._RESET_CREDIT_STATUS_MAP`.
- **Is it blocking M2? NO**.

### AUD-7 — `project.id` non restreint, influence `state_dir`

- **Severity**: LOW · **Confidence**: MEDIUM
- **Area**: Config validation / posture de sécurité
- **Files**: `src/orchestrator/project_config.py:184-185` (`_default_state_dir`), `:391` (`_require_str` sur `project.id`)
- **Observed behavior**: `project.id` n'est validé que comme "chaîne non vide". Quand `state_dir` est omis, `_default_state_dir(project_id)` construit `~/.local/state/ai-dev-orchestrator/projects/<project_id>` sans échapper ni rejeter `/`, `..`.
- **Why it matters**: menace réaliste faible — `aido.yaml` est un fichier local que l'utilisateur écrit lui-même — mais un `aido.yaml` malveillant livré dans un dépôt tiers cloné puis exécuté sans relecture attentive pourrait faire sortir `state_dir` de l'arborescence prévue.
- **Concrete reproduction scenario**: `project.id: "../../../tmp/evil"`, `state_dir` omis, `aido run` → la base SQLite se crée hors de `projects/`.
- **Existing test coverage**: aucun test ne couvre un `project.id` contenant `/` ou `..`.
- **Recommended correction**: restreindre `project.id` à un motif sûr (alphanumérique + `-`/`_`), ou rejeter tout séparateur de chemin avant construction de `state_dir`. Dépend de la posture assumée sur `aido.yaml` (fichier de confiance strictement local, ou potentiellement issu d'un clone) — question de produit autant que de code.
- **Is it blocking M2? NO**.

### AUD-8 — Kill/timeout Ralph jamais exercé avec un vrai sous-processus

- **Severity**: LOW · **Confidence**: HIGH
- **Area**: Tests — Ralph execution engine
- **Files**: `tests/test_ralph_execution_engine.py:458,617` ; implémentation réelle `src/orchestrator/ralph_execution_engine.py:849-873` (`_default_subprocess_runner`)
- **Observed behavior**: le seul runner réellement utilisé en production implémente le timeout via `asyncio.wait_for` + `process.kill()`. Aucun test n'invoque cette fonction avec un vrai sous-processus lent et un timeout court pour vérifier que le kill se produit réellement — les 2 seuls tests sur `RalphTimeoutError` utilisent un faux runner qui lève directement l'exception, testant uniquement la réaction d'`execute()`, jamais le mécanisme de kill lui-même.
- **Why it matters**: une régression future sur ce mécanisme (suppression accidentelle de `process.kill()`, mauvais ordre d'`await`) laisserait un vrai process Ralph orphelin sans qu'aucun test ne le détecte.
- **Concrete reproduction scenario**: retirer `process.kill()` de `_default_subprocess_runner` — `pytest -q` reste vert à 100%.
- **Existing test coverage**: aucune sur ce chemin précis.
- **Recommended correction**: ajouter un test utilisant `_default_subprocess_runner(["sleep", "5"], ...)` avec un timeout court, vérifier la levée de `RalphTimeoutError` en temps borné et l'absence de process résiduel.
- **Is it blocking M2? NO**.

### AUD-9 — `docs/status.md` : compte de tests et date obsolètes

- **Severity**: LOW · **Confidence**: HIGH
- **Area**: Documentation drift
- **Files**: `docs/status.md:3,24`
- **Observed behavior**: le HEAD actuel (`75b3ef5`, "Update worker display names #13") a fait passer la suite à 1238 tests (confirmé par exécution réelle) sans mettre à jour `docs/status.md`, qui affiche encore "1234 PASS" et un en-tête "mis à jour le 2026-09-21" alors que le corps référence déjà des événements du 2026-09-23.
- **Why it matters**: `docs/status.md` se présente comme un "snapshot factuel" faisant foi ; un chiffre daté à un commit près est un signal mineur de dérive de processus (pas de mise à jour systématique à chaque commit fonctionnel/de tests).
- **Concrete reproduction scenario**: `python3 -m pytest -q` → 1238 passed ; `grep PASS docs/status.md` → 1234.
- **Existing test coverage**: N/A (documentation).
- **Recommended correction**: mettre à jour la ligne et la date, ou automatiser la vérification (hook/CI qui échoue si le compte documenté diverge du compte réel).
- **Is it blocking M2? NO**.

### AUD-10 — `Project.current_mvp_id` : état mort

- **Severity**: LOW · **Confidence**: HIGH
- **Area**: Persistence — dette
- **Files**: `src/orchestrator/project_state.py:204,494` ; `src/orchestrator/project_runtime.py:307`
- **Observed behavior**: `current_mvp_id` est écrit à chaque `bootstrap()` mais lu par aucune logique de décision — `engine.py` et `mvp_manager.py` utilisent tous `cfg.mvp.id` (la config chargée), jamais ce champ (grep exhaustif : zéro lecture en dehors de son propre accesseur/tests).
- **Why it matters**: confirme positivement l'absence d'un pointeur "MVP courant" séparé (bonne nouvelle pour la transition multi-MVP dont M2 dépend), mais ce champ persisté est un état mort qui pourrait induire en erreur un futur contributeur.
- **Concrete reproduction scenario**: N/A — pas un bug comportemental.
- **Existing test coverage**: `test_set_current_mvp` teste l'écriture uniquement.
- **Recommended correction**: soit l'utiliser réellement (ex. MVP par défaut pour une future commande sans `--mvp`), soit le documenter explicitement comme informatif/legacy.
- **Is it blocking M2? NO**.

### AUD-11 — Aucun test bout-en-bout pour la transition multi-MVP séquentielle

- **Severity**: LOW · **Confidence**: MEDIUM
- **Area**: Persistence — couverture de test
- **Files**: `tests/test_project_runtime.py`, `tests/test_project_state.py`
- **Observed behavior**: aucun test committé n'exerce le scénario exact "plusieurs MVP séquentiels sous le même `project_id`/`state_dir`" (mvp-0.1 → 0.1.1 → 0.1.2) dont dépend explicitement la planification M2 d'AIDO Code. Seules des primitives unitaires existent (rejet de MVP dupliqué, scoping de `list_work_items`), pas de test bout-en-bout du cycle multi-MVP.
- **Why it matters**: rien ne protège cette propriété contre une régression future (ex. un cache mal scopé par `project_id`).
- **Concrete reproduction scenario**: N/A — gap de couverture, pas un bug observé.
- **Existing test coverage**: partielle (primitives seulement).
- **Recommended correction**: ajouter un test d'intégration créant deux MVP successifs sous le même projet et vérifiant l'absence de fuite/duplication.
- **Is it blocking M2? NO** — mais M2 s'appuie sur cette garantie sans filet de test côté moteur.

## Security review

Aucun `shell=True`, `os.system`, `eval`/`exec`, `pickle`, ni `yaml.load` non sécurisé nulle part dans `src/` (vérifié par grep exhaustif). Tous les `subprocess.run` utilisent des listes argv. Aucun secret en dur ; clés API lues uniquement via variables d'environnement, jamais loggées (`DeepSeekConfigError`/`KimiConfigError` n'incluent jamais la valeur du secret). `WorkerRegistry._reject_secrets` bloque activement tout champ ressemblant à un secret dans `workers.yaml`. Le mode `UNRESTRICTED` est opt-in explicite, jamais la valeur par défaut, documenté avec avertissement dédié — politique délibérée et assumée, pas un bug. Deux points LOW : AUD-6 (diagnostic uniquement, pas de vulnérabilité) et AUD-7 (`project.id` non restreint, path-traversal-adjacent mais dépendant du modèle de menace assumé sur `aido.yaml`). Aucun SECURITY BUG confirmé au sens strict de la section 20.

## Determinism / governance review

Le flux DEV A → DEV B (exclusion inconditionnelle, non désactivable) → QA déterministe (`evaluate_qa_verdict` ne lit jamais `engine_reported_status`) → merge fast-forward-only est réellement implémenté et testé, y compris de façon adversariale pour l'attribution Git. Le point noir réel de cette section est **AUD-1** : le garde-fou anti-affaiblissement des tests de régression existe mais n'est pas actif en production — c'est la seule brèche trouvée dans l'invariant "un worker ne décide jamais lui-même de la validité de sa QA". AUD-5 documente une limite structurelle (pas un bug) du déterminisme QA pour de futures stacks non-Python.

## Persistence / recovery review

Migrations SQLite additive-only et idempotentes (vérifié identiquement dans `validation.py` et `execution_store.py` : anciennes lignes décodées honnêtement en `None`, jamais fabriquées). `RecoveryCoordinator` ne relance jamais et ne présume jamais `SUCCEEDED` pour une exécution orpheline ; passe toujours par `RECOVERY_REQUIRED`. `QARunStore` est insert-only (historique immuable). La coexistence multi-MVP sous le même `project_id`/`state_dir` est structurellement correcte en code (`bootstrap()` scope par `mvp_id`, aucun pointeur "MVP courant" consommé — voir AUD-10), mais non couverte par un test bout-en-bout committé (AUD-11). La séparation `MVPManager`/`ReleaseManager` pour RUNNING→VALIDATING→RELEASED est vérifiée correcte et non ambiguë en code.

## Provider / quota review

Quota strictement provider-level (jamais dupliqué par worker), `UNKNOWN` jamais coercé en valeur fabriquée, reset credits jamais auto-consommés (`auto_consume` pinné à `False` au niveau du contrat), single-flight sur les probes concurrentes. Mapping Victor/Oscar (gpt-6-luna/gpt-6-sol/gpt-6-astra + reasoning_effort) structurellement cohérent et validé. Seul défaut : AUD-6, purement diagnostique.

## Git / attribution review

Aucune opération destructrice dans `git_governance.py` (pas de `shell=True`, pas de force, pas de `reset --hard`, pas de `clean`/`stash` automatique). Merge fast-forward-only, preuves liées au SHA avec revérification de contenu (`_same_up_to_noise`), protection anti-pollution `.ralph/` fail-closed. Attribution Git worker à double défense (env vars + `git config --local` scopé, jamais global/system) avec audit post-exécution fail-closed testé y compris contre un scénario adversarial de nested commit. Point réel : AUD-2 (suppression de fichier non suivi non détectée, déjà observée en pilote). Point de vigilance documenté par le code lui-même : l'hypothèse "no parallelism" est vraie aujourd'hui (aucun appel concurrent constaté) mais deviendrait une race sur `git config --local` si du parallélisme était ajouté sans refonte — pas un défaut actuel.

## Test-quality review

1238/1238 tests passent réellement. Zéro usage de `Mock`/`@patch` dans les tests des zones critiques (worker_selector, git_governance, qa, ralph, recovery, wait) — injection via de vrais fakes contraints par les ABC réelles. Tests E2E réels avec vrai `git` et vrai sous-processus imbriqué pour l'attribution Git, y compris un cas adversarial. Test E2E cross-process réel pour crash/resume (fermeture/réouverture réelle de SQLite entre deux phases). Discipline sur les `except` larges : seulement 10 occurrences sur 19 664 lignes, la plupart annotées et justifiées — les 2 non annotées sont précisément AUD-3. Deux gaps réels : AUD-8 (timeout Ralph jamais exercé en conditions réelles) et AUD-11 (pas de test E2E multi-MVP).

## Documentation drift

Divergence factuelle confirmée : AUD-9 (`docs/status.md`, compte de tests et date). `docs/QA_STRATEGY.md` §8.3 et `docs/PROJECT_CONFIG.md` correspondent exactement au code malgré leur taille (575 et 199 lignes) — aucune autre divergence importante trouvée entre README.md/ROADMAP.md/docs/ADAPTIVE_EXECUTION.md/docs/SPIKE_RALPH.md/docs/VIBE_SPIKE.md et le code réel.

## Open questions

- **OQ-1**: aucune re-vérification de disponibilité provider n'a lieu entre `WorkerSelector.select()` (qui peut s'appuyer sur un état caché jusqu'à `QuotaPolicy.state_ttl`) et le lancement réel par `RalphExecutionEngine`. Le comportement exact en cas d'échec provider survenant pendant cette fenêtre n'a pas été tracé avec une preuve technique suffisante pour constituer un finding.
- **OQ-2**: comportement en cas de disparition d'un `worker_id` historique du registry alors que des enregistrements persistés y font référence — `WorkerRegistry.get()` lève proprement `UnknownWorkerError` à la relecture, mais l'interaction complète côté persistence/recovery/resume n'a pas été vérifiée de bout en bout.
- **OQ-3**: `_commits_introduced`/`_audit_worker_commit_identity` traite `git_sha_before is None` comme "dépôt vierge, tout l'historique est audité" — cas légitime testé. Mais `_git_head_sha` peut aussi retourner `None` sur un échec transitoire de `git rev-parse HEAD`, non distingué du premier cas. Dans un dépôt gouverné réel (qui a toujours un `base_sha`), ce chemin n'est atteignable que par un échec transitoire, non reproduit avec une preuve technique. Effet si occurrence : échec du côté sûr (faux `WorkerCommitIdentityMismatchError`), jamais un faux PASS.
- **OQ-4**: aucune exécution concurrente d'`execute()` sur un même workspace n'existe dans le code actuel, mais l'interaction précise entre une exception levée *à l'intérieur* de `MVPManager.run_next_work_item` (pendant DEV A/DEV B/QA/merge) et sa conversion garantie en état WorkItem (`BLOCKED`/`FAILED`) plutôt qu'une fuite brute n'a pas été vérifiée exhaustivement.
- **OQ-5**: `WorkItemStatus.REVIEWING` est un état mort documenté ("no current code path enters REVIEWING"), mais `RecoveryCoordinator.reconcile_work_item` ne teste que `RUNNING`, jamais `REVIEWING`, alors que `mark_work_item_recovery_required` documente encore accepter les deux. Risque dormant uniquement si un état persisté pré-suppression de REVIEWING existe encore quelque part — non démontré comme vivant.
- **OQ-6**: la reprise après interruption pendant la revue DEV B relance un cycle de développement complet plutôt que de reprendre juste la revue (comportement explicite, probablement intentionnel pour la simplicité/sûreté) ; son interaction exacte avec le SHA/commit déjà produit par DEV A n'a pas été tracée en détail.

## Positive controls

- Exclusion DEV A/DEV B non désactivable au niveau du contrat (`ValueError` si tentative de désactivation), tie-break de sélection déterministe et testé.
- Aucun hardcode provider dans `worker_selector.py` (confirmé par grep) ; `adaptive_execution.py`/`complexity_estimation.py` filtrent *avant* `WorkerSelector`, jamais en parallèle — pas de second scheduler implicite.
- Quota provider-level dédupliqué (un seul appel même si plusieurs workers partagent un provider), `UNKNOWN` jamais coercé, reset credits jamais auto-consommés, single-flight sur les probes concurrentes.
- Ralph : exit code jamais confondu avec le verdict métier ; timeout → `mark_interrupted`, aucun retry aveugle ; absence d'événement terminal reconnu → `FAILED` par défaut, jamais `SUCCEEDED`.
- Attribution Git worker à double défense (env + `git config --local` scopé, restauré même sur exception/timeout via `finally`), audit fail-closed testé avec un vrai sous-processus imbriqué et un scénario adversarial explicite.
- Git governance sans aucune opération destructrice ; fast-forward-only structurel ; preuves SHA revérifiées par contenu réel (`git diff`), pas par confiance aveugle en une étiquette.
- QA : `evaluate_qa_verdict` ne lit jamais `engine_reported_status` ; `_environment_drift_detail` compare uniquement les runs au même `head_sha` exact ; manifeste de policy snapshoté à la création du run, jamais recalculé rétroactivement.
- Persistence : migrations additive-only, `QARunStore` insert-only, `RecoveryCoordinator` toujours fail-closed vers `RECOVERY_REQUIRED`, jamais de présomption `SUCCEEDED`.
- Séparation `MVPManager`/`ReleaseManager` correcte et non ambiguë en code.
- Packaging : test réel construisant un wheel et vérifiant que `default_workers.yaml` y est byte-identique présent — pas une simple assertion statique.
- Sécurité : zéro `shell=True`/`eval`/`pickle`/YAML unsafe dans tout `src/` ; aucun secret en dur ; mode `UNRESTRICTED` opt-in explicite et documenté.
- Tests : zéro `Mock`/`@patch` dans les suites critiques, fakes contraints par de vraies ABC ; E2E réels (git imbriqué, crash/resume cross-process avec vraie fermeture/réouverture SQLite) ; discipline sur les `except` larges (10 occurrences sur 19 664 lignes, quasi toutes justifiées).

## M2 readiness

**NO CONFIRMED BLOCKER FOUND.**

Aucun finding CRITICAL. Le seul finding HIGH (AUD-1) est une brèche de gouvernance QA côté moteur (protection des tests de régression inactive en production), entièrement orthogonale au périmètre M2 : M2 ne touche jamais QA, sélection de worker, ni exécution — son seul contrat avec le moteur est `OrchestratorEngine.run()`/`.status()`/`.workers()`/`.probe_workers()` appelés tels quels depuis une UX de session. Les findings MEDIUM (AUD-2 à AUD-5) concernent Ralph, le CLI `status`, et la QA — aucun n'affecte la construction, la persistance ou la lecture d'une session AIDO Code, ni l'invariant "resume n'appelle jamais `.run()`". Les mécanismes dont M2 dépend explicitement (façade Engine stable et read-only pour `.status()`, coexistence multi-MVP sous le même `project_id`/`state_dir`) sont vérifiés structurellement corrects en code, avec une seule réserve de couverture de test (AUD-11, pas un bug observé). Recommandation : corriger AUD-1 avant tout futur MVP réel non trivial construit par le flux gouverné, indépendamment du calendrier M2.
