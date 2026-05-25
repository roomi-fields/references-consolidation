# LEGAL — paper-trail

> Détails légaux et licences. Pour les attributions précises voir
> `NOTICE.md`. Pour les shadow libraries voir `DISCLAIMER.md`.

## Licence du plugin

`paper-trail` est distribué sous licence **MIT** (voir `LICENSE`).

Cela signifie que vous pouvez :
- Utiliser le plugin à des fins commerciales et non commerciales
- Le modifier, le forker, le redistribuer
- L'inclure dans un produit propriétaire

Sous condition de :
- Préserver l'attribution copyright originale et le texte de licence
  MIT dans toutes les copies ou portions substantielles

## Composants importés

Certains composants du plugin proviennent d'autres projets de l'auteur,
basculés en MIT pour ce repo (voir `NOTICE.md` pour le détail) :

- **Worker B** (`pipeline/`, `tools/`) — originalement écrit pour le
  projet `references-consolidation` (renommé en paper-trail), MIT
- **Helpers PDF acquisition** (`lib/`) — originalement écrits pour le
  plugin Claude Code `source-collector`, MIT
- **Skills d'écriture et d'audit** (`skills/sota-writer`,
  `skills/sota-auditor`, `skills/citation-receipts`,
  `skills/paper-writer`) — originalement écrites pour le projet
  musicology-phd, MIT

## Patterns inspirés (sans copie de code)

Les patterns architecturaux suivants sont inspirés de projets tiers,
sans qu'aucune ligne de code source ne soit copiée. Cela préserve la
compatibilité licence MIT du plugin :

- `Imbad0202/academic-research-skills` (ARS) — CC BY-NC 4.0 : concept
  du pipeline multi-stages, audit anchors, adapter pattern.
  **Aucun code copié.**
- `Agents365-ai/paper-fetch` — MIT : format de sortie JSON, convention
  de nommage. Pattern d'inspiration.
- `JamesWeatherhead/receipts` — MIT : pattern audit local PDF↔claim,
  format RECEIPTS.md. Ré-implémentation Python indépendante.
- `fcakyon/phd-skills` — MIT : conception des hooks d'intégrité.
- `Psypeal/claude-knowledge-vault` — MIT : YAML frontmatter,
  Sci-Hub opt-in pattern.

## Shadow libraries (Anna's Archive, Sci-Hub)

Voir `DISCLAIMER.md` en détail.

**Résumé légal** :
- Désactivés par défaut
- Activation explicite via `RESEARCH_ENABLE_SHADOW_LIBS=1`
- L'utilisateur reconnaît la responsabilité du caractère légal de son
  usage selon sa juridiction
- Le plugin n'héberge aucun contenu protégé
- Tracé dans le registre (`acquisition_attempts[].via` préfixé `_optin`)

## MCPs externes (paper-search, notebooklm, rtfm)

Le plugin documente l'utilisation de MCP servers tiers
(`paper-search`, `notebooklm`, `rtfm`) que l'utilisateur configure
indépendamment dans son `~/.claude/mcp.json` ou `<project>/.mcp.json`.

Ces MCPs ne sont **pas** distribués par le plugin paper-trail. Le
plugin ne dépend pas formellement de leur présence (les skills
fonctionnent en mode dégradé sans paper-search, par exemple, en
demandant à l'utilisateur de fournir les références).

## En cas de question légale

- Pour les questions de licence MIT et d'attribution : ouvrir une
  issue sur https://github.com/roomi-fields/paper-trail/issues
- Pour les questions liées à votre usage des shadow libraries dans
  votre juridiction : consultez un juriste, ne demandez pas conseil
  via les issues du repo
