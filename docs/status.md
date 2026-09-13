# Status

Dernière mise à jour : Slice 14 (2026-09-13).

- **Slice 13 — Notification + optimistic approval window (20 min) :
  DONE.** Voir `src/orchestrator/approval.py`.
- **Slice 14 — Apply decided RoadmapProposal + create/start next MVP :
  DONE** (offline, tests verts). Voir
  `src/orchestrator/roadmap_application.py` (nouveau :
  `RoadmapApplicationStore`, `RoadmapApplicationService`,
  `RoadmapApplication`). Ferme la boucle autonome Release N ->
  ActivityReport -> planning -> RoadmapProposal -> approbation -> MVP
  N+1. Seules les décisions `APPROVED`/`AUTO_APPROVED` autorisent
  l'application (fail-closed) ; le hash sha256 de `ROADMAP.md` est
  revérifié avant toute mutation (`CONFLICT`/STALE_PROPOSAL sinon,
  aucune mutation) ; l'application est déterministe (aucun LLM), atomique
  (`tempfile` + `os.replace`), et idempotente/redémarrable (un
  `APPLYING` orphelin n'est jamais rejoué aveuglément — reconciliation
  explicite via `reconcile()`). `ROADMAP.md` garde tout son contenu
  humain verbatim ; seule une section clairement marquée est régénérée
  depuis le store. Aucune opération Git runtime. Le démarrage du MVP
  suivant reste un appel contrôlé, optionnel, à l'API existante de
  `MVPManager` (jamais réimplémentée).
- Prochaine étape attendue : **Slice 15 — Git/PR/merge governance**, à
  ré-évaluer (rien dans les slices 7-14 ne l'a rendue bloquante jusqu'ici).

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
