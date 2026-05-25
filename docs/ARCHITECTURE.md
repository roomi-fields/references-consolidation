# ARCHITECTURE — paper-trail

> Vue d'ensemble de l'architecture du plugin Claude Code paper-trail.
> **Statut** : squelette créé en P0, à compléter en P5.

## Sommaire (à compléter)

1. Vue système (diagramme Mermaid plugin + worker B + MCPs)
2. Composants du plugin (skills, commands, agents, hooks, adapters)
3. Intégration avec le worker B (FSM + cascade + doctor)
4. Intégration avec les MCPs (paper-search, notebooklm, rtfm)
5. Flux Cas A (création SOTA) end-to-end
6. Flux Cas B (audit SOTA/article) end-to-end
7. Stratégie de tests (3 niveaux)
8. Décisions architecturales clés (skills statiques, sub-agents, adapter pattern)

## Squelette de référence

Pour le détail du worker B (FSM 8 états, cascade 10 niveaux, 19
invariants doctor), voir :
- `pipeline/ARCHITECTURE.md` (vue d'oiseau worker B)
- `plans/B_worker_FSM_pipeline.md` (spec FSM canonique)
- `plans/plan-design.md` (design Couches 1-5 worker B)

Pour la vision système globale (cas A + cas B), voir :
- `plans/SYSTEM_ARCHITECTURE.md`

Pour le plan d'exécution paper-trail (P0-P5), voir :
- `plans/PLUGIN_EXECUTION_PLAN.md`

(Contenu détaillé à venir en Phase P5 — synthèse exécutive de
l'architecture après que tous les composants soient livrés.)
