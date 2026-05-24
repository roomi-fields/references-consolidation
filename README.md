# references-consolidation

Pipeline d'acquisition et de validation de références bibliographiques
pour le projet doctoral de musicologie computationnelle de Romain Peyrichou.

## Pourquoi ce projet existe

Le paper 9α v1 (Computational Linguistics, 2026-02) a été retiré du processus
de revue après détection de **12 erreurs bibliographiques** dont une
attribution inversée et un *quote* fabriqué. La cause racine : citations
écrites « de mémoire » sans vérification de la source.

Ce pipeline rend l'erreur **mécaniquement impossible** :

- **Machine à états stricte** (FSM 8 états) : aucune ref ne peut être citée
  sans avoir traversé acquisition + page 1 validation + claim verification.
- **Anti-homonymie** à chaque étape : validation page 1 (auteur attendu +
  similarité titre + zéro mots-clés off-domain), seuils stricts (Crossref
  title_similarity ≥ 0.7, AA distinctive keywords obligatoires).
- **Cascade 10 niveaux** d'acquisition avec logging de toutes les
  tentatives (`acquisition_attempts[]`).
- **Auto-fix** des dérives connues (R8 : `pdf_path` doublement préfixé).
- **`assert_coverage.py`** : garde-fou mécanique qui refuse exit 0 si une
  étape n'a pas son tableau de 2+ refs testées E2E.

## Commandes

Depuis la racine du repo, avec le venv activé :

```bash
python -m pipeline status              # comptes par état
python -m pipeline run [--ref X]       # pousse les refs actives, lint + doctor en fin
python -m pipeline run --dry-run       # affiche les plans sans muter
python -m pipeline run --no-doctor     # skip les invariants I1-I15 en fin de run
python -m pipeline lint                # invariants R1-R10 (linter du registry)
python -m pipeline doctor              # invariants I1-I15 (sur-couche worker)
python -m pipeline doctor --fix        # auto-fix les violations auto-fixables (I4, I6, I9, I5 semi)
python -m pipeline doctor --severity warn  # filtre min (info/warn/error)
python -m pipeline doctor --json       # sortie machine-readable
python -m pipeline reactivate-ocr      # re-évalue les awaiting_rtfm_ocr via rtfm check
```

Tests E2E :

```bash
python pipeline/tests/test_f1_negative.py            # test anti-homonymie F1
python pipeline/tests/test_invariants_synthetic.py   # 15/15 fixtures invariants I1-I15
python pipeline/tests/assert_coverage.py             # garde-fou couverture (F1-F4, P5, I1-I15)
```

## Dépendances externes (hors repo)

Ce repo ne fonctionne **pas en isolation** — il dépend de ressources
externes maintenues ailleurs :

| Ressource | Localisation | Rôle |
|---|---|---|
| Registry refs YAML | `/mnt/d/Obsidian/Articles/Projets/Ontologie musicale/10_SOURCES/_registry/refs/*.md` | Source de vérité des 909 références |
| Plugin source-collector | `~/.claude/plugins/source-collector/lib/` | Helpers (validate_pdf_content, oa_finder, s2_resolver, annas_archive, archive_org_helper, download_books) |
| RTFM CLI | `~/.local/bin/rtfm` | Index local indexé du corpus |
| lint_registry.py | `/mnt/d/.../10_SOURCES/_registry/tools/lint_registry.py` | Linter d'invariants R1-R10 |

Les chemins sont configurés en absolu dans `pipeline/config.py`.

## Installation

```bash
cd /home/romi/dev/mcp/references-consolidation
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt

# Test rapide : doit retourner ~909 refs
venv/bin/python -m pipeline status
```

## Architecture

Voir `plans/B_worker_FSM_pipeline.md` pour la spec complète et
`plans/B_worker_fixes_2026-05-24.md` pour l'historique des corrections.

Modules :

- `pipeline/config.py` — chemins et constantes
- `pipeline/registry.py` — load/save atomique des fichiers refs
- `pipeline/journal.py` — événements append-only
- `pipeline/dispatcher.py` — décide la prochaine transition
- `pipeline/transitions.py` — fonctions de transition de la FSM
- `pipeline/cascade.py` — cascade 10 niveaux d'acquisition
- `pipeline/rtfm_helper.py` — wrapper `rtfm check --path`
- `pipeline/linter_wrapper.py` — wrapper du linter du registry
- `pipeline/invariants.py` — 15 fonctions `check_I<n>` (Couche 1)
- `pipeline/doctor.py` — orchestrateur invariants + auto-fix + rapport (Couche 1)
- `pipeline/cli.py` — argparse `python -m pipeline ...`
- `pipeline/tests/` — coverage_set, coverage_run, assert_coverage,
  test_f1_negative, test_invariants_synthetic, fixtures synthétiques

## Discipline « livré »

Aucune transition n'est annoncée « livrée » sans :

1. Code écrit et passant les imports.
2. ≥ 2 refs réelles d'archétypes distincts l'ont franchie avec succès.
3. Au moins 1 cas négatif documenté (ex: test anti-homonymie sur ref `retracted`).

Garde-fou mécanique : `python pipeline/tests/assert_coverage.py` doit
retourner exit 0 avant tout message annonçant « livré ».

## Licence

MIT — voir `LICENSE`.
