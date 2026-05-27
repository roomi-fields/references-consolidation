---
description: Resolve incomplete textbook references in the registry. For refs that were ingested without year/title (e.g., "Hopcroft FR + EN" → ref slug `hopcroft_0000_untitled`), uses LLM to identify the actual book and either merge into an existing ref or complete missing fields. Bulk cleanup after INGEST.
---

# `/paper-trail:resolve-textbooks` — IA pass on incomplete refs

Étape complémentaire de l'INGEST : nettoie les refs ingérées avec
year/title manquant.

## Usage

```
/paper-trail:resolve-textbooks                  # dry-run, montre les candidates
/paper-trail:resolve-textbooks --apply          # applique les décisions IA
```

## Ce que fait Claude

1. **Liste les candidates** :
   ```bash
   python3 -m pipeline resolve-textbooks --list > /tmp/textbooks.json
   ```
   Le JSON contient pour chaque ref incomplète :
   - `slug`, `author`, `year` (probable 0000/null), `title` (probable empty)
   - `ingest_source` : SOTA d'origine
   - `siblings` : autres refs du registre avec même lastname (pour fusion)

2. **Pour chaque candidate**, invoque un sub-agent IA qui décide :
   - **merge_into** : si un `sibling` correspond au même textbook (par
     exemple `hopcroft_2001_introduction` existe déjà), fusion
   - **complete** : sinon, identifier le textbook par connaissance
     générale (Sipser → "Introduction to the Theory of Computation",
     2012 ; Carton → "Langages formels", 2008 ; etc.) et compléter
     year + title
   - **blocked** : si vraiment impossible d'identifier (ref ambiguë,
     auteur trop générique), marquer `blocked_human:textbook_unidentified`

3. **Construit un fichier `/tmp/textbook_decisions.json`** :
   ```json
   [
     {"slug": "hopcroft_0000_untitled", "action": "merge_into",
      "target_slug": "hopcroft_2001_introduction"},
     {"slug": "sipser_0000_untitled", "action": "complete",
      "year": "2012",
      "title": "Introduction to the Theory of Computation"},
     {"slug": "wolper_0000_untitled", "action": "blocked",
      "reason": "Wolper textbook needs human disambiguation"}
   ]
   ```

4. **Applique** :
   ```bash
   python3 -m pipeline resolve-textbooks --apply-from /tmp/textbook_decisions.json
   ```

5. **Récap final** : N merged, N completed, N blocked.

## Quand l'utiliser

- **Après chaque `/paper-trail:ingest`** sur un SOTA legacy (texte libre)
- **En batch après `/paper-trail:ingest-all`** pour cleanuper d'un coup

## Garde-fous

- Sub-agent ne fabrique JAMAIS un DOI ou une année incertaine. En cas
  de doute, decision = `blocked` (l'utilisateur tranchera).
- Les fusions sont **réversibles** via git (le commit auto vaut snapshot).
- Les refs fusionnées passent en `retracted` avec `retracted_reason:
  merged_into:<target_slug>` (traçabilité).
