# NOTICE — Attributions

Ce plugin Claude Code `paper-trail` est sous licence MIT (cf. `LICENSE`).
Certains composants ou patterns sont inspirés de projets tiers ; cette
notice en liste les attributions et les origines.

---

## Composants importés depuis d'autres projets de l'auteur

### Worker B (FSM 8 états + cascade 10 niveaux + 19 invariants doctor)

Écrit par Romain Peyrichou pour le projet `references-consolidation`
(2026-05). Modules sous `pipeline/` et `tools/`. Licence : MIT (auteur
original = mainteneur actuel).

### Helpers PDF acquisition (lib/)

Originalement écrits pour le plugin Claude Code `source-collector`
(`~/.claude/plugins/source-collector/lib/`) par Romain Peyrichou.
Importés ici en MIT pour rendre le plugin paper-trail self-contained.

Fichiers concernés :
- `lib/oa_finder.py` — Crossref OA URL resolver
- `lib/s2_resolver.py` — Semantic Scholar resolver
- `lib/archive_org_helper.py` — archive.org search & download
- `lib/validate_pdf_content.py` — page 1 anti-homonymy validation
- `lib/download_books.py` — generic PDF download utility
- `lib/shadow/annas_archive.py` — Anna's Archive helper (opt-in)
- `lib/shadow/scihub.py` — Sci-Hub helper (opt-in, extracted from
  pipeline/cascade.py)

### Skills d'écriture et d'audit (skills/)

Originalement écrites pour le projet doctoral musicology-phd
(`~/dev/musicology-phd/.claude/skills/`) par Romain Peyrichou.
Généralisées pour usage scientifique multi-domaines dans paper-trail,
basculées en MIT.

Skills concernées :
- `skills/sota-writer/` (ex `~/musicology-phd/.claude/skills/sota-writer/`)
- `skills/sota-auditor/` (ex `~/musicology-phd/.claude/skills/sota-curator/`,
  renommée pour cohérence terminologique)
- `skills/citation-receipts/` (ex
  `~/musicology-phd/.claude/skills/citation-verification/`, enrichie
  du format RECEIPTS.md)
- `skills/paper-writer/` (ex `~/musicology-phd/.claude/skills/paper-writer/`)

Agents et outils dérivés :
- `agents/researcher.md` (ex `corpus-explorer` skill, converti en sub-agent)
- `tools/notebooklm-integration.md` (ex `notebooklm-manager` skill)
- `tools/citation_audit.py` (généralisation de
  `~/musicology-phd/scripts/verify_claims.py` +
  `~/musicology-phd/scripts/validate_claims_s2.py`, paramétré par
  fichier source)

---

## Patterns inspirés de plugins tiers (sans copie de code)

### `Imbad0202/academic-research-skills` (ARS) v3.9.4

Licence : CC BY-NC 4.0. Auteur : Cheng-I Wu
(https://github.com/Imbad0202/academic-research-skills).

**Concepts repris** (pas de code copié) :
- Architecture du pipeline d'écriture multi-stages (research → write →
  review → revise → finalize)
- Notion d'audit anchors pour la traçabilité des claims
- Pattern adapter pour les vaults (Obsidian, flat, etc.)

Aucun fichier source ARS n'est inclus dans paper-trail. La compatibilité
licence MIT du plugin est préservée car CC BY-NC 4.0 n'autorise pas le
fork sous une licence différente, mais permet l'inspiration sur des
concepts/architectures non copyrightables.

### `Agents365-ai/paper-fetch` v0.5.0

Licence : MIT. Auteur : Agents365-ai
(https://github.com/Agents365-ai/paper-fetch).

**Patterns repris** :
- Format de sortie JSON stable pour les acquisitions de PDFs
- Convention de nommage de fichier `{first_author}_{year}_{journal}_{title}.pdf`
- Exit codes typés pour routing orchestrateur

### `JamesWeatherhead/receipts`

Licence : MIT. Auteur : James Weatherhead
(https://github.com/JamesWeatherhead/receipts).

**Patterns repris** :
- Audit local PDF↔claim (lecture du paper + sources, génération verdict
  par citation)
- Format `RECEIPTS.md` avec verdicts structurés VALID / ADJUST /
  INVALID + raison + correction suggérée

Le code de receipts est en JavaScript. Notre implémentation est en
Python (`tools/citation_audit.py` + skill `citation-receipts`),
ré-implémentation indépendante du pattern.

### `fcakyon/phd-skills` v1.3.0

Licence : MIT. Auteur : fcakyon
(https://github.com/fcakyon/phd-skills).

**Patterns repris** :
- Conception des hooks d'intégrité (`PostToolUse`, `PreToolUse`,
  `SessionEnd`) pour vérifier les artefacts académiques en temps réel
- Idée du `factcheck` contre des bases bibliographiques

### `Psypeal/claude-knowledge-vault` v2.4.0

Licence : MIT. Auteur : Psypeal
(https://github.com/Psypeal/claude-knowledge-vault).

**Concepts repris** :
- YAML frontmatter pour les références (`.vault/raw/<slug>.md`)
- Pattern « Sci-Hub opt-in par projet » (que nous avons étendu en
  opt-in via variable d'environnement)

---

## MCPs utilisés (configurés par l'utilisateur, hors-périmètre)

Paper-trail interagit avec des MCP servers que l'utilisateur configure
indépendamment dans son `~/.claude/mcp.json` ou `<project>/.mcp.json` :

- `paper-search` (recherche académique multi-plateforme)
- `notebooklm` (corpus de livres, optionnel)
- `rtfm` (indexation locale, optionnel)

Ces MCPs ne sont pas inclus dans paper-trail. Le plugin documente leur
utilisation mais ne dépend pas formellement de leur présence.
