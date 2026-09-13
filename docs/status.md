# Status

Dernière mise à jour : Slice 13 (2026-09-13).

- Dernière slice publiée avant cette session : **Slice 12 — Multi-agent
  release planning + roadmap synthesis** (DONE, offline, tests verts).
- **Slice 13 — Notification + optimistic approval window (20 min) : DONE**
  (offline, tests verts). Voir `src/orchestrator/approval.py` (nouveau :
  `ApprovalWindow`, `ApprovalStore`, `ApprovalCoordinator`,
  `ApprovalPolicy`, `ApprovalStatus`). Layered sur `RoadmapProposal`
  (Slice 12) par référence (`roadmap_proposal_id`), jamais en modifiant
  `planning.RoadmapProposalStatus`. Deadline persistée et figée à la
  création (`auto_approval_enabled` capturé depuis la policy en vigueur à
  cet instant, jamais recalculé) ; `APPROVE`/`REJECT`/`MODIFY` immédiats,
  `AUTO_APPROVED` fail-closed sur `now >= deadline_at`. Notification via
  `NotificationSink` injectable (no-op par défaut), échec de livraison
  audité mais jamais bloquant. Aucune mutation de `ROADMAP.md`, aucune
  création de MVP réel, aucun enchaînement automatique sur `MODIFY`
  (reporté à une slice future).
- Prochaine étape attendue : ré-évaluer **Slice 14 — Git/PR/merge
  governance**, ou construire l'application réelle d'une
  `RoadmapProposal` décidée (mutation `ROADMAP.md` + MVP suivant), pas
  encore numérotée comme slice explicite.

Détail complet des slices, de la vision cible et du découpage incrémental :
voir `ROADMAP.md` (source de vérité fonctionnelle).

Ce fichier reste court et factuel — pas de duplication de `ROADMAP.md`.
