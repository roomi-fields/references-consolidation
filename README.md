# paper-trail

Plugin Claude Code **anti-hallucination** pour la recherche scientifique.

> Crée des SOTAs / revues de littérature **garantis sans fabrication**,
> audite les SOTAs et articles existants (purge ou warnings), valide
> mécaniquement chaque citation via un moteur strict : FSM 8 états +
> cascade d'acquisition PDF + page 1 anti-homonymie + 19 invariants de
> cohérence registre.

## Pourquoi ce plugin existe

Paper 9α v1 (Computational Linguistics, 2026-02) a été retiré du
processus de revue après détection de **12 erreurs bibliographiques**
dont une attribution inversée et un *quote* fabriqué. Cause racine :
citations écrites « de mémoire » sans vérification.

Ce plugin rend l'erreur **mécaniquement impossible** :

- **Machine à états stricte** (FSM 8 états) — aucune ref ne peut être
  citée sans avoir traversé acquisition + page 1 validation + claim
  verification
- **Anti-homonymie** à chaque étape — validation page 1 (auteur attendu +
  similarité titre + zéro mots-clés off-domain)
- **Cascade 10 niveaux** d'acquisition PDF avec logging exhaustif des
  tentatives
- **19 invariants doctor** (I1-I19) cross-session — détection automatique
  des dérives du registre
- **`assert_coverage.py`** — garde-fou mécanique qui refuse une release
  si une étape n'a pas son tableau de tests E2E

## Statut

**v0.1.0 — en cours de construction.**

Voir `plans/PLUGIN_EXECUTION_PLAN.md` pour le plan d'exécution en 6
phases (P0 fait, P1-P5 à venir).

Le **worker B** (moteur Python sous `pipeline/`) est complet et testé
(19/19 invariants synthétiques, idempotence, concurrence, événements).
Les skills, commands, agents, hooks et docs sont en cours
d'intégration.

## Quick start

(Section à compléter en P5 — pour l'instant l'installation directe via
`/plugin install` n'est pas garantie stable.)

Workflow utilisateur cible :

```
/paper-trail:new-sota "Petri nets in music notation"
  → recherche multi-source (paper-search MCP)
  → propositions de refs candidates
  → cascade d'acquisition PDFs
  → page 1 validation anti-homonymie
  → rédaction du SOTA avec UNIQUEMENT les refs validées

/paper-trail:audit-sota path/to/SOTA_Existing.md [--purge]
  → audit des refs citées (existence, accessibilité, hallucination ?)
  → option --purge : retire automatiquement les hallucinations

/paper-trail:audit-article path/to/Paper.tex [--warn]
  → audit local PDF↔claim pour chaque citation
  → option --warn : insère commentaires \todo{} dans une copie .bak

/paper-trail:doctor [--fix]
  → vérifie 19 invariants de cohérence du registre
  → option --fix : auto-fix les invariants safe (I4, I6, I9)
```

## Sous-commandes worker B (déjà disponibles)

Depuis la racine du repo, avec le venv activé :

```bash
python -m pipeline status              # comptes par état
python -m pipeline run [--ref X]       # pousse les refs actives
python -m pipeline run --dry-run       # affiche les plans sans muter
python -m pipeline lint                # invariants R1-R10 (linter du registry)
python -m pipeline doctor [--fix]      # invariants I1-I19
python -m pipeline events --since DATE # journal JSONL filtré
python -m pipeline reactivate-ocr      # re-évalue les awaiting_rtfm_ocr
```

Tests E2E :

```bash
python pipeline/tests/test_invariants_synthetic.py  # 19/19 invariants
python pipeline/tests/test_f1_negative.py           # anti-homonymie F1
python pipeline/tests/assert_coverage.py            # garde-fou couverture
```

## Architecture

- **`pipeline/`** : worker B en Python (FSM + cascade + doctor + RTFM
  bridge). Voir `pipeline/ARCHITECTURE.md` et `pipeline/USAGE.md`.
- **`lib/`** : helpers PDF acquisition (oa_finder, s2_resolver,
  archive_org_helper, validate_pdf_content). Sous `lib/shadow/` :
  Anna's Archive et Sci-Hub, opt-in strict.
- **`skills/`, `commands/`, `agents/`, `hooks/`** : enveloppe Claude
  Code (en cours d'intégration).
- **`adapters/`** : layouts vault (obsidian, flat, zotero stub).
- **`docs/`** : documentation utilisateur.
- **`plans/`** : plans de conception et d'exécution.

## Configuration

Variables d'environnement :

```bash
# Vault & registre (defaults : ~/research_vault et sous-dossiers)
export RESEARCH_VAULT_PATH=/path/to/your/vault
export RESEARCH_SOURCES_PATH=$RESEARCH_VAULT_PATH/sources
export RESEARCH_REGISTRY_PATH=$RESEARCH_SOURCES_PATH/_registry

# Layout du vault (defaults : obsidian)
export RESEARCH_VAULT_LAYOUT=obsidian   # obsidian | flat | zotero

# Shadow libraries (opt-in strict — voir DISCLAIMER.md)
export RESEARCH_ENABLE_SHADOW_LIBS=1   # ⚠️ active Anna's Archive + Sci-Hub

# Skip doctor en fin de session
export RESEARCH_SKIP_END_DOCTOR=1      # désactive le SessionEnd hook
```

## Licence et attributions

- **Licence** : MIT (voir `LICENSE`)
- **Shadow libraries** : opt-in strict, voir `DISCLAIMER.md`
- **Attributions** : voir `NOTICE.md` (composants importés et patterns
  inspirés de projets tiers)

## Contribuer

Ce plugin est sous développement actif pour un usage de recherche
doctorale. Le code est public sous MIT mais les contributions externes
ne sont pas encore activement sollicitées tant que la v0.1.0 n'est pas
stable. Issues bienvenues sur
[github.com/roomi-fields/paper-trail/issues](https://github.com/roomi-fields/paper-trail/issues).

## Liens

- Plan d'exécution : `plans/PLUGIN_EXECUTION_PLAN.md`
- Architecture worker B : `pipeline/ARCHITECTURE.md`
- Vision système globale : `plans/SYSTEM_ARCHITECTURE.md`
- Utilisation worker B : `pipeline/USAGE.md`
