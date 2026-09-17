# Status

Dernière mise à jour : Pilote d'acceptance réel Roman Numerals — PASS (2026-09-17).

## État actuel (résumé factuel)

- **MVP 0.1** : `DONE` (2026-09-17). Contrat d'acceptation :
  `MVP_SPEC.yaml` v3 (16 AC, réaligné sur le produit réel ; v2 original
  intégralement récupérable via `git log -p -- MVP_SPEC.yaml`).
- **Phase 1** : `DONE`.
- **Workflow** : `LEAN_FEATURE_FLOW` = `DEFAULT`.
- **Nominal AI executions** : 2 (DEV A, DEV B corrective review).
- **QA** : déterministe / non-LLM (`QAPhase.FINAL_VERIFICATION`, aucun
  `WorkerSelector`, aucun agent IA) ; jusqu'à 3 tentatives QA au total,
  puis `HUMAN_REVIEW_REQUIRED`.
- **Worker pool** (`config/workers.yaml`) : 4 workers, 2 par provider —
  `alice`/`bob` = anthropic, `victor`/`oscar` = openai.
- **GOVERNED_FULL** : `DEPRECATED` / `REMOVAL_CANDIDATE` (reste
  sélectionnable explicitement, tests verts, non enrichi).
- **Slice 24** : `ACCEPTANCE_DONE` (2026-09-16) — voir détail ci-dessous.
- **Tests offline** : 1158 PASS.
- **Pilote externe Roman Numerals (2026-09-17)** : `PASS` — premier
  pilote réel post-clôture MVP 0.1, repli same-provider observé pour de
  vrai (openai en quota épuisé au moment du run). Détail : voir
  `docs/reports/roman-numerals-lean-pilot-2026-09-17.md` et l'entrée
  datée ci-dessous.
- **Mars Rover (pilote externe)** : en pause, aucun pilote actif.
- **Slice active** : aucune.
- **Next** : POST-MVP 0.1 EXPERIMENT / DISCOVERY — axe (1) Roman Numerals
  fait (PASS) ; axes (2)-(5) proposés, non démarrés — voir `ROADMAP.md`,
  « Next ».

Le détail daté ci-dessous fait foi pour l'historique ; ce résumé reflète
l'état courant.

- **Slice 24 — QA/Rework/Review/Merge Integration : DONE,
  `ACCEPTANCE_DONE` (2026-09-16).** QA devient une capacité opt-in de plus
  de `MVPManager` (`qa_engine`/`qa_policy`/`qa_run_store`/
  `qa_protected_paths`), utilisée exclusivement via le `Protocol`
  `QAEngine` — aucun `isinstance`/import de `InternalQAEngine` dans
  `mvp_manager.py` (preuve directe : deux faux moteurs de forme
  différente traversent la même intégration,
  `tests/test_mvp_manager_qa_integration.py::TestProviderIndependence`).
  Workflow réel : Development → QA Test Authoring (optionnel, adaptatif,
  avant les gates) → Quality Gates → Review indépendante → Final QA
  Verification (read-only, après review APPROVED ou directement après
  les gates sans review) → Merge Eligibility → Merge. QA FAIL avec
  `requires_coding_agent=True` → REWORK (chemin adaptatif existant,
  aucun nouveau routeur) ; un FAIL après Final QA invalide naturellement
  la review déjà APPROVED (SHA-binding existant) → nouvelle review
  obligatoire. `QAPolicy.max_qa_cycles` compté durablement, indépendant
  de `ReviewPolicy.max_review_cycles`. `compute_merge_eligibility`
  (4 kwargs additifs, défaut rétrocompatible) et `ReleaseManager`
  (check `qa-verdict-pass`) étendus sans jamais importer `orchestrator.qa`
  ni appeler de moteur QA. `WaitPhase.QA_AUTHORING` + reprise `RUNNING`
  (jamais `READY`, le développement n'est jamais rejoué) ; recovery d'une
  exécution `qa_testing` orpheline traitée comme une exécution
  `developer`. Bug réel trouvé/corrigé pendant l'intégration :
  `InternalQATestAuthor.run_authoring`'s `base_sha` doit être le head
  juste avant la QA (post-dev), jamais le `base_sha` global du WorkItem.
  1089 tests offline PASS (1074 + 15, `tests/test_mvp_manager_qa_integration.py`).
  **Self-dogfood : `ACCEPTANCE_DONE` (2026-09-16)** —
  `scripts/self_dogfood_full_pipeline_real.py` a fait passer un WorkItem
  gouverné, sur une copie jetable du dépôt, avec les deux providers réels
  (Claude + Codex) : Development → QA Test Authoring → Quality Gate →
  Review indépendante (APPROVED) → QA Final Verification (PASS) →
  éligibilité au merge → **merge réel** (`git merge --ff-only`), plus le
  contrôle négatif obligatoire. Preuve versionnée :
  `docs/reports/self-dogfood-full-pipeline-2026-09-15.html` (commit
  `c292215`, "Stabilize full QA pipeline acceptance"). Voir
  `docs/QA_GOVERNANCE.md` § « Slice 24 » pour le détail et la
  justification complète.
  **Historique (superseded) :** une tentative antérieure (probe quota lu
  seul, 2026-09-15, un seul provider alors disponible — openai en quota
  épuisé) avait échoué en `BLOCKED_BY_PROVIDER` déterministe, faute d'un
  second worker `qa_testing` distinct de l'auteur. Cet état a été
  superseded par le succès du 2026-09-16 ci-dessus, lui-même obtenu avant
  même l'extension du pool à 4 workers (2026-09-17) — le pool étendu
  rendrait aujourd'hui ce scénario robuste même à un seul provider
  disponible, mais ce n'était déjà plus le blocage constaté au moment du
  succès réel.
- **Slice 23 — QA Engine MVP : DONE (Python/pytest uniquement).** Nouveau
  `src/orchestrator/internal_qa_engine.py` — `InternalQAEngine` (implémente
  le `QAEngine` Slice 22), `InternalQAPlan`, `InternalQATestAuthor`,
  `run_qa_cycle`. Composition stricte : `QualityGateRunner` (jamais un
  second runner — une commande ciblée pytest est un `ValidationCommand`
  de plus, via un `project_id` synthétique éphémère), `analyze_test_impact_deterministic`/
  `run_final_verification_gate`/`qa_protection` (Slice 21.5/22, verbatim),
  `evaluate_qa_verdict` (jamais dupliqué). Scope honnête : absence de
  marqueur pytest (`pyproject.toml`/`pytest.ini`/`setup.cfg`/`tox.ini`) =>
  `INCONCLUSIVE`, jamais `PASS` ; aucune prétention JS/Java/mobile/
  browser/BrowserStack/TestSprite/Momentic. Sélection de tests
  déterministe réutilisée (regression-map/critical-paths → tests ciblés,
  repli sur régression globale configurée si rien sélectionné, sinon
  `INCONCLUSIVE` — jamais `PASS` sur preuve vide). Known-flaky : re-run
  borné (jamais infini), tenté et échoué toujours conservé en evidence,
  jamais un skip list. QA Test Authoring adaptatif réutilise
  intégralement le mécanisme existant (Slice 16/17/19) ; capability
  `qa_testing` ajoutée à `config/workers.yaml` (alice/victor, aucun
  worker dédié fabriqué) ; `qa_worker_id != developer_worker_id`
  obligatoire, `!= reviewer_worker_id` préféré (repli automatique, jamais
  bloquant à deux workers). Mutation de production détectée
  indépendamment (jamais une confiance aveugle dans l'event structuré du
  worker) => violation fail-closed. `RealizationReport` enrichi en option
  (`qa_run_store`). Aucune intégration `MVPManager`/merge/release
  (Slice 24). 1074 tests offline PASS (1004 + 70).
  **Smoke réel PASS** (`scripts/smoke_internal_qa_real.py`) : vrai worker
  alice (anthropic/claude_code/sonnet), copie jetable de
  `~/projects/ralph-spike`, test de régression réel ajouté prouvant le
  bug connu de `add()`, aucune modification de production, verdict
  gouverné `FAIL` + `requires_coding_agent=True`, ralph-spike original
  inchangé. Deux bugs réels trouvés et corrigés par ce smoke (fichiers
  `test_*.py` hors `tests/`, bruit `__pycache__` imbriqué — voir
  `docs/QA_GOVERNANCE.md`).
  **Self-dogfood acceptance : BLOCKED_BY_PROVIDER (partiel, honnête)**
  (`scripts/self_dogfood_dev_qa_real.py`) : copie jetable complète de ce
  dépôt lui-même, défaut contrôlé réel introduit, contrôle négatif
  confirmé RED avant correction, vrai worker DEVELOPMENT (alice) a
  réellement corrigé le défaut sans toucher aux tests — puis bloqué
  honnêtement à la sélection du worker QA distinct (seul `anthropic` est
  disponible, `openai` en quota épuisé — un seul worker `qa_testing`
  éligible existe donc, le même que le développeur, exclusion
  obligatoire jamais contournée). Dépôt source (`~/projects/ai-dev-orchestrator`)
  vérifié strictement inchangé avant/après (HEAD identique, aucun statut
  git inattendu). Aucun rapport de réalisation fabriqué pour ce chemin
  bloqué. Aucun reset credit consommé au-delà des sessions réelles
  nécessaires ; aucun push.
- **Slice 22 — QA Governance + Regression Knowledge Base : DONE.**
  Socle QA provider-independent uniquement — aucun `InternalQAEngine`,
  aucune intégration `MVPManager`/merge/release (Slice 23/24). Nouveaux
  `src/orchestrator/qa.py` (`QARequest`/`QAResult`/`QAVerdict`/
  `QARunStatus`/`QAVerdictStatus`/`QAPhase`/`QAPolicy`/
  `QAEvidenceManifest`/`QARunStore`/`QAEngine` Protocol/
  `QAEngineCapabilities`/`TestImpactRequest`/`TestImpactResult`,
  `evaluate_qa_verdict`), `qa_knowledge.py` (`.qa/*.yaml`), `qa_protection.py`
  (baseline SHA-256). **Séparation centrale prouvée** : `QAResult` (ce
  qu'un moteur observe, `engine_reported_status` informatif seulement)
  n'est jamais l'autorité du verdict ; `QAVerdict` vient exclusivement de
  `evaluate_qa_verdict`, fonction pure ne lisant jamais
  `engine_reported_status` — prouvé par deux faux moteurs (style
  interne/externe) donnant le même verdict gouverné malgré des statuts
  auto-déclarés opposés. Policy/manifest snapshotés par `QARun` (motif
  Slice 21.5 généralisé) ; `QARunStore` restart-safe, `record_result`/
  `record_verdict` insert-only. Final Verification read-only réutilise
  **verbatim** `QualityGateRunner.run_gate(require_nonempty_mandatory_manifest=True,
  verify_repository_unchanged=True)` (Slice 21.5) — violation => `FAIL`,
  incapacité à prouver le read-only => `INCONCLUSIVE`, jamais `PASS`.
  Baseline de tests protégés SHA-256, chemins toujours fournis par
  l'appelant (jamais `"tests/"` codé en dur) ; mutation non autorisée =>
  `FAIL`. `.qa/invariants.yaml`/`regression-map.yaml`/`critical-paths.yaml`/
  `known-flaky.yaml` : absence => connaissance vide valide, jamais
  d'auto-création, écriture atomique, YAML invalide/ID dupliqué => échec
  fermé, `known-flaky` ne convertit jamais FAIL en PASS. Frontière stricte
  Git (connaissance produit durable) / SQLite (runtime orchestrateur),
  jamais mélangées. Analyseur Test Impact déterministe minimal (chemin
  changé → regression-map/critical-paths → tests/invariants liés,
  aucun AST/dépendances/sémantique). Ce dépôt seed son propre
  `.qa/invariants.yaml` (4 invariants réellement démontrés par des tests
  existants). Voir `ROADMAP.md`, Slice 22, et `docs/QA_GOVERNANCE.md`.
  1004 tests offline PASS (892 + 112). Aucun service QA externe appelé,
  aucun reset credit consommé.
- **Slice 21.5 — Evidence / SHA hardening : DONE.** Quatre points
  techniques relevés par l'audit Codex de Slice 21, **relus/reproduits
  dans le vrai code avant correction** (jamais pris pour argent
  comptant) : (A) manifest QA obligatoire vide produisait `passed=True`
  — corrigé via `require_nonempty_mandatory_manifest` opt-in (défaut
  `False`, rétrocompatible) sur `QualityGateRunner.run_gate`/
  `_compute_passed` ; (B) `ValidationStore.get_gate_result` recalculait
  `passed` depuis la config *courante* du projet, pas celle appliquée au
  run — corrigé via `record_manifest`/`get_manifest_for_run` (nouvelle
  table insert-only, snapshot du manifest par `validation_run_id`) ; (C)
  aucune vérification d'invariance du HEAD après exécution — corrigé via
  `verify_repository_unchanged` opt-in, lève
  `ReadOnlyValidationViolationError` fail-closed si le HEAD change ; (D)
  **merge TOCTOU/head drift réel** dans `git_governance.py::merge` —
  fusionnait par nom de branche, jamais par SHA pinné, reproduit avec un
  vrai dépôt temporaire (eligibility pour H2, work branch avancée à H3,
  merge aurait fusionné H3 sur la preuve de H2) — corrigé : `merge()`
  exige désormais que le tip réel de `work_branch` soit strictement égal
  à `eligibility.head_sha`, sinon `GitHeadDriftError` fail-closed, `main`
  jamais touchée. Voir `ROADMAP.md`, Slice 21.5, `docs/GIT_GOVERNANCE.md`
  et `docs/QA_STRATEGY.md` §8.1 pour le détail. Primitives génériques
  seulement — `InternalQAEngine` n'est pas construit (reste Slice 23).
  892 tests offline PASS (876 + 16, dont des tests "confirms" qui
  reproduisent chaque bug avant sa correction).
- **Slice 21 — QA Architecture + Build-vs-Adopt Study : DONE, arbitrage
  utilisateur (2026-09-14).** Deux études indépendantes : Claude
  (`docs/QA_BUILD_VS_ADOPT_REPORT_CLAUDE.md`, recherche web réelle,
  `HYBRID` 80/100, second `BUILD_INTERNAL` 76/100) et Codex
  (`docs/QA_BUILD_VS_ADOPT_REPORT.md`, `BUILD_INTERNAL` 75/100, second
  `HYBRID` 74/100), la seconde écrite sans lire la première. Arbitrage
  documenté dans `docs/QA_BUILD_VS_ADOPT_ARBITRATION.md` +
  `docs/reports/qa-build-vs-adopt-arbitration.html` — **jamais tranché
  par les scores** : les deux études convergent en pratique sur
  `InternalQAEngine` first (aucun candidat externe ne passe toutes les
  elimination gates aujourd'hui). **Décision utilisateur : architecture
  cible `HYBRID-READY`, implémentation immédiate
  `BUILD_INTERNAL_MINIMAL`.** TestSprite/Momentic/BrowserStack/Diffblue
  restent des adaptateurs externes futurs possibles (statuts détaillés
  dans l'arbitrage), aucun approuvé aujourd'hui comme gate final.
  **Slice 23 décidée : `InternalQAEngine` MVP mince, Python/pytest
  first** — non commencée cette session. Rapports d'étude + arbitrage
  commités (voir hash ci-dessous), non poussés.
- **Revue de roadmap post-Slice 20 (avec l'utilisateur) : DONE.** Toutes
  les Slices 7-20 de Phase 1 sont DONE ; décision : ouvrir un nouveau
  cycle **QA/Regression Testing Governance** (Slices 21-25). Étude
  d'architecture complète dans `docs/QA_STRATEGY.md` — trois modes gardés
  ouverts (`INTERNAL_QA`/`EXTERNAL_QA`/`HYBRID_QA`), aucun fournisseur
  externe (TestSprite/BrowserStack/Momentic/Diffblue) sélectionné
  définitivement, aucun agent interne présumé nécessaire (même principe
  « pas de biais maison » que l'audit OmniRoute). Session documentation
  uniquement : `ROADMAP.md`/`docs/status.md`/`docs/QA_STRATEGY.md`
  modifiés, **aucun code fonctionnel/test modifié** (876 tests offline
  PASS, inchangé depuis Slice 20). Voir `ROADMAP.md` pour le détail des
  Slices 21-25.
- **Slice 20 — Git/PR/merge governance : DONE.** Nouveau
  `src/orchestrator/git_governance.py` (`LocalGitWorkspace`,
  `GitGovernancePolicy`, `GitWorkItemRecord`/`GitWorkItemStore`,
  `GitGovernanceService`) — l'orchestrateur devient propriétaire de la
  branche/du SHA/de l'éligibilité au merge d'un WorkItem ; Ralph reste le
  moteur d'exécution, les workers ne décident jamais eux-mêmes de la
  branche, du merge, ou de la mergeabilité. Branche gouvernée
  déterministe et stable après restart (`work/<work-item-id>`), `main`
  protégée par défaut, `base_sha` immuable, preuve de merge liée au SHA
  exact (gate + review), fast-forward-only par défaut (jamais de rebase/
  force/reset automatique ; une divergence fait échouer proprement),
  `auto_merge=False` par défaut (testé réellement à `True` sur repos
  temporaires). Intégration opt-in dans `MVPManager` (fresh + rework +
  reprise WAITING + reprise RECOVERY, un seul point de câblage partagé),
  `ReleaseManager` (nouveau check `governed-work-items-merged`) et
  `RealizationReport` (branche/SHA/statut de merge/PR + événements
  d'audit dans la timeline). Abstraction PR optionnelle (`gh` CLI, argv
  explicite) — une PR n'implique jamais une autorisation de merge ;
  aucune vraie PR/push distant créé cette session. Smoke local réel PASS
  (dépôt temporaire, sans provider LLM additionnel). 876 tests offline
  PASS (790 + 86). Voir `ROADMAP.md`, Slice 20, et
  `docs/GIT_GOVERNANCE.md`.
- **Slice 19 — Adaptive review/planning integration : DONE.** Review
  (fresh + reprise `WaitPhase.REVIEW` + reprise `RECOVERY_REQUIRED`, un
  seul point de câblage via `MVPManager._run_review` déjà partagé par les
  trois chemins) et release planning/roadmap synthesis
  (`PlanningCoordinator`, planner **et** synthesizer — les deux confirmés
  être de vraies exécutions Ralph/LLM, jamais une synthèse déterministe
  dans ce dépôt) passent désormais par la même chaîne adaptative que
  development/rework (Slice 17) : pre-flight indépendant par rôle
  (fingerprint Slice 16), `minimum_quality_tier`, `resolve_profile()`
  réutilisée sans seconde implémentation, `AdaptiveExecutionDecision`
  persistée avant toute exécution. `AdaptiveExecutionSelector.select()`
  gagne un paramètre optionnel `author_worker_id` (rétrocompatible) qui
  active la politique cross-provider existante de `WorkerSelector` sans
  jamais la réimplémenter. Aucun profil `CRITICAL` fabriqué (testé
  explicitement) ; `ApprovalCoordinator`/`RoadmapApplicationService`/
  `RealizationReport`/`ActivityReport` non touchés (`RealizationReport`
  agrège déjà les rôles sans filtrage — prouvé par un nouveau test, pas
  par une modification). `config/workers.yaml` non modifié. Voir
  `ROADMAP.md`, Slice 19, et `docs/ADAPTIVE_EXECUTION.md`. 790 tests
  offline PASS (772 + 18). Smoke réel : non relancé (offline uniquement,
  comme demandé).
- **Slice 18.5 — Stabilisation pré-Slice 19 : DONE.** Deux corrections
  factuelles trouvées lors de l'audit OmniRoute : (1) mismatch
  `REVIEW_CAPABILITY` ("reviewer") vs la capability réelle
  `config/workers.yaml` ("code_review") — corrigé, `code_review` est
  désormais la seule convention canonique, testé contre le vrai fichier
  de config ; (2) `reasoning_effort` réellement vérifié comme supporté
  côté `claude_code` via le vrai flag `claude --effort <level>` — transmis
  désormais comme pour `codex`, aucun changement de comportement observable
  tant qu'aucun profil Claude ne définit `reasoning_effort`. Voir
  `ROADMAP.md`, Slice 18.5.
- **Slice 17 — Adaptive Worker/Profile Selection for development/rework :
  DONE.** `WorkerSelector`/`AdaptiveExecutionSelector`/`resolve_profile()`
  couvrent DEVELOPMENT et REWORK, y compris les chemins de reprise
  (`_try_resume_due_wait`, `_try_resume_recovery_required`) — jamais de
  downgrade silencieux, jamais `Worker.profile()` par défaut quand
  l'adaptive execution est activée. Review, release planning et roadmap
  synthesis rendus adaptatifs depuis par la Slice 19 (ci-dessus).
- **Slice 18 — Realization reports + real cross-worker cold-resume
  acceptance : DONE.** Voir `src/orchestrator/realization_report.py`
  (nouveau : `RealizationReport`, `RealizationReportStore`,
  `RealizationReportService`) — un snapshot déterministe et auditable
  d'un WorkItem (timeline, executions, recommendations/adaptive
  decisions, handoffs, quality gates, reviews, waits, incidents), rendu
  en HTML autonome (`render_html()`, aucun CDN/JS externe, déterministe,
  jamais LLM-généré) ; insert-only, un rapport intermédiaire
  (`HANDOFF_READY`) n'est jamais écrasé par le rapport final. Ne remplace
  pas `ActivityReport` (consolidation release/MVP) ; `HandoffRecord` reste
  l'unique artefact machine de passation.
- **Cross-worker cold-resume E2E (validation d'acceptation Slice 17/18,
  2026-09-13)** :
  - offline automatisé (`tests/integration/test_cross_worker_resume_e2e.py`) :
    **PASS**
  - **smoke réel (Claude/Codex) : PASS** (`scripts/smoke_cross_worker_real.py`,
    manuel, jamais lancé par `pytest`) — deux vrais workers de
    `config/workers.yaml`, deux process OS réellement distincts :
    `alice` (anthropic/claude_code, `haiku`, tier `SIMPLE`) puis `victor`
    (openai/codex, `gpt-5.6-terra`, `reasoning_effort=low`, tier
    `SIMPLE`) ; handoff réellement transmis ; quality gate réel PASSED ;
    aucun downgrade ; aucun bug Slice 17/18 découvert. Preuve versionnée
    (sanitizée) : `docs/reports/real-cross-worker-resume-2026-09-13.html`
  - `~/projects/ralph-spike` original : non modifié (vérifié avant/après)
- **Décision produit (2026-09-16) — `LEAN_FEATURE_FLOW` (DEV A → DEV B
  correctif → QA unique déterministe → merge → tag) devient le workflow
  PAR DÉFAUT de `MVPManager` ; l'ancien pipeline (`GOVERNED_FULL`,
  Slices 17-24) devient `DEPRECATED`/`REMOVAL_CANDIDATE`, encore
  sélectionnable explicitement, sa suite de tests reste verte.** Détail
  complet, motivation KISS/YAGNI, et primitives réutilisées : voir
  `ROADMAP.md`. 1154 tests offline PASS (1144 avant + 10 nouveaux dans
  `tests/test_mvp_manager_lean_feature_flow.py`). **Acceptance réelle
  externe : non versionnée** — le rapport
  `docs/reports/mars-rover-lean-feature-flow-2026-09-16.html` cité ici
  n'existe pas dans le dépôt (confirmé par l'audit de clôture MVP 0.1,
  2026-09-17 ; `git log --all` sur ce chemin est vide). `LEAN_FEATURE_FLOW`
  est validé offline par sa suite d'intégration dédiée ; une acceptance
  externe fraîche reste à réaliser (voir « Next » ci-dessous).
- **Décision produit (2026-09-17) — Worker pool fallback : config
  uniquement, `provider`/`backend` restent sur `Worker` (pas de refactor
  `ExecutionProfile`, YAGNI confirmé).** Revue d'architecture
  (`WorkerSelector`/`WorkerRegistry`/`QuotaManager`/`wait.py`/`handoff.py`)
  confirmant que le seul écart réel avec l'invariant de continuité visé
  (« un quota provider épuisé ne doit jamais forcer `WAITING` si un autre
  worker compatible sur un provider disponible existe ») était la taille
  du pool déclaré : un seul worker par provider
  (`alice`/anthropic, `victor`/openai). Corrigé par config seule :
  `config/workers.yaml` étend à 4 workers — `bob` (anthropic) et `oscar`
  (openai), capabilities/profils strictement identiques à leur worker
  primaire, `priority: 90` (contre `100`) pour que la sélection sans
  auteur préfère naturellement le worker primaire quand tous les
  providers sont disponibles. **`src/orchestrator/` non modifié** — aucun
  nom de worker câblé en dur, `WorkerSelector`/`WorkerSelectionPolicy`
  inchangés (`require_distinct_worker_for_review=True` épinglé,
  `prefer_distinct_provider_for_review=True`,
  `require_distinct_provider_for_review=False`, déjà les valeurs par
  défaut). 4 tests offline ajoutés (2 dans `tests/test_worker_registry.py`
  vérifiant la forme du pool réel — 4 workers, 2 par provider, Bob≡Alice/
  Oscar≡Victor ; 2 dans `tests/test_mvp_manager_lean_feature_flow.py`
  prouvant le repli same-provider — anthropic seul disponible, puis
  openai seul disponible — sans jamais passer par `WAITING`). 1158 tests
  offline PASS (1154 avant + 4). Documentation alignée (`ROADMAP.md`,
  `README.md`, `docs/ADAPTIVE_EXECUTION.md`, `docs/QA_GOVERNANCE.md`) :
  plus aucune section courante ne présente `Developer.model`/
  `Developer.provider != Reviewer.*` comme un invariant du chemin nominal
  (l'invariant réel porte sur `worker_id`, `provider` distinct restant
  préféré, jamais requis par défaut). Mars Rover (pilote externe) mis en
  pause par décision utilisateur ce même jour — dépôt pilote intact comme
  preuve/audit, aucun nouveau run, aucun smoke réel, aucune consommation
  de quota provider réel cette session.
- **Décision produit (2026-09-17) — Clôture MVP 0.1 / Phase 1.** Revue de
  clôture factuelle : matrice de preuves AC-par-AC de `MVP_SPEC.yaml` v2
  contre le code/tests/Git réels, zéro nouveau code/test/config. Verdict :
  aucun gap de comportement produit ne bloque le MVP 0.1 — seul le
  contrat (v2, jamais modifié depuis sa création le 2026-09-12) était
  stale. `MVP_SPEC.yaml` v3 : CLI/Ollama de-scopés en options futures,
  `ModelRouter` → `WorkerSelector`, reset de quota simulé → re-probe réel,
  Workspace 100% abstrait → gouvernance Git centralisée avec une exception
  de lecture (`git rev-parse HEAD`) assumée et documentée ; 2 critères
  ajoutés (AC-15 indépendance auteur/second développeur, AC-16
  `LEAN_FEATURE_FLOW`). Texte v2 original entièrement préservé par Git
  (`git log -p -- MVP_SPEC.yaml`), chaque critère modifié porte un
  `historical_note`. Statut Slice 24 réconcilié (voir ci-dessus). Citation
  d'un rapport d'acceptance Lean inexistant retirée (voir ci-dessus).
  `MVP 0.1` = `DONE`, `Phase 1` = `DONE`. 1158 tests offline PASS,
  inchangés.
- **Pilote d'acceptance réel (2026-09-17) — Roman Numerals /
  `LEAN_FEATURE_FLOW` : `PASS`.** Premier pilote externe réel post-clôture
  MVP 0.1, `MVPManager.run_next_work_item(...)` réel sur dépôt jetable
  dédié (`~/projects/roman-numerals-kata`), aucun composant factice. DEV A
  = `alice` (anthropic, 82,1 s), DEV B = `bob` (anthropic, 21,1 s, aucune
  correction). **Repli same-provider observé pour de vrai** : `openai` en
  `quota_exhausted` au moment du run, `WorkerSelector` a basculé sur un 2ᵉ
  worker anthropic plutôt que d'attendre — première confirmation en
  conditions réelles du pool worker/provider fermé le 2026-09-17. QA
  déterministe `PASS` en 1 tentative (22 tests, dont un test de propriété
  round-trip 1..3000 conçu par les développeurs). Merge fast-forward local
  + tag `feature/wi-roman-numerals-kata/done`. 2 exécutions LLM, 0
  `DEV FIX`, 0 `WAITING`. Dépôt de contrôle vérifié inchangé avant/après.
  Limites honnêtes rapportées : `TDD_STRICTNESS = NOT_VERIFIABLE` (commit
  unique, non vérifiable indépendamment), tokens `NOT_AVAILABLE`
  (télémétrie Ralph à zéro, déjà connue peu fiable). Rapport complet :
  `docs/reports/roman-numerals-lean-pilot-2026-09-17.md`.
- **Next : POST-MVP 0.1 EXPERIMENT / DISCOVERY.** Pas encore un MVP 0.2 —
  décision à prendre avec l'utilisateur. Axe (1) — pilote Lean frais
  (Roman Numerals) — **fait, `PASS`** (voir ci-dessus). Axes restants,
  **proposés, non démarrés**, non ordonnés entre eux : (2) étude de
  faisabilité Mistral/Vibe ; (3) étude build-vs-reuse Mammouth AI ;
  (4) étude de l'écosystème des workers/providers gratuits/coût marginal
  nul ; (5) échelle de difficulté progressive des projets de validation
  (dont un futur pilote de niveau supérieur à Roman Numerals).
  CLI/productisation reste une option future, non requise pour démarrer
  ces axes. Toutes les Slices 21-24 du cycle QA sont maintenant `DONE`
  (Slice 24 : `ACCEPTANCE_DONE`) — cette session ne décide pas seule si un
  POC QA externe ou la Slice 25 (conditionnelle) sont réellement utiles.
  OmniRoute reste une qualification future optionnelle, hors roadmap
  principale (voir
  `docs/OMNIROUTE_ARBITRATION.md`).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
