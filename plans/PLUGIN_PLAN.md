# Plugin Claude Code — `research-anti-hallucination`

> Plan détaillé. Ce repo (actuellement `references-consolidation`) devient
> **le** plugin Claude Code anti-hallucination. Le worker B (CLI `pipeline/`)
> reste, mais comme moteur interne invoqué par les skills. Aucun second repo.
>
> À valider avant code.

---

## 1. Vision en 3 phrases

Un plugin Claude Code qui te permet de demander un nouveau SOTA sur un sujet
(`/research:new-sota`) **avec garantie 0 ref hallucinée**, d'auditer un SOTA
ou article existant pour purger/signaler les fabrications
(`/research:audit-sota`, `/research:audit-article`), et de consolider la
base bibliographique entre les sessions. Il intègre le moteur worker B
existant (FSM + cascade + 19 invariants doctor) et ajoute les couches
orchestration et UX qui manquent aujourd'hui.

Nom proposé : **`research-anti-hallucination`** (ou plus court :
`research-rigor`). À trancher §11.

---

## 2. Mutation du repo

État actuel :

```
references-consolidation/
├── pipeline/              # worker B (Python CLI)
├── tools/                 # scripts admin (reinject_legacy_blocked.py)
├── plans/                 # docs design
├── README.md
└── venv/
```

État cible (additif — rien d'existant n'est supprimé) :

```
references-consolidation/           ← repo (renommé GitHub plus tard ?)
├── .claude-plugin/
│   └── plugin.json                 # NOUVEAU — manifest Claude Code
├── skills/                         # NOUVEAU
│   ├── source-collector/           # migré depuis ~/.claude/plugins/
│   │   └── SKILL.md
│   ├── sota-writer/                # NOUVEAU
│   │   └── SKILL.md
│   ├── sota-auditor/               # NOUVEAU
│   │   └── SKILL.md
│   ├── citation-verifier/          # NOUVEAU (verify_claims généralisé)
│   │   └── SKILL.md
│   └── article-warner/             # NOUVEAU
│       └── SKILL.md
├── commands/                       # NOUVEAU — slash commands
│   ├── new-sota.md
│   ├── audit-sota.md
│   ├── audit-article.md
│   ├── status.md
│   ├── worker-run.md
│   └── worker-doctor.md
├── agents/                         # NOUVEAU — subagents prompts
│   ├── researcher.md
│   ├── validator.md
│   └── curator.md
├── mcp/                            # NOUVEAU — MCP server configs
│   └── paper-search.config.json
├── pipeline/                       # INCHANGÉ — worker B moteur
├── tools/                          # INCHANGÉ — scripts admin
├── plans/                          # INCHANGÉ — docs design
├── README.md                       # RÉÉCRIT — décrit le plugin, pas le worker seul
└── ARCHITECTURE.md                 # NOUVEAU (à la racine) — vue plugin
```

Le worker B (`pipeline/`) reste un module Python importable. Les skills et
commandes l'invoquent soit en CLI (`python -m pipeline …`) soit en
sub-module (`from pipeline.doctor import run_doctor_for_cli`).

---

## 3. Manifest `plugin.json`

Structure validée sur d'autres plugins (`source-collector`,
`claude-scientific-writer`) :

```json
{
  "name": "research-anti-hallucination",
  "description": "Pipeline complet anti-hallucination pour recherche doctorale : création de SOTAs garantis sans fabrication, audit de SOTAs/articles existants, validation mécanique par cascade 10 sources + page 1 + 19 invariants. Intègre worker B (FSM stricte) comme moteur batch.",
  "version": "0.1.0",
  "author": {
    "name": "Romain Peyrichou",
    "email": "research@roomi-fields.com"
  },
  "license": "MIT",
  "keywords": [
    "bibliography", "anti-hallucination", "sota", "citation-verification",
    "pdf-acquisition", "research", "doctoral", "musicology", "claude-skills"
  ]
}
```

---

## 4. Skills — contrats détaillés

Une skill Claude Code = un `SKILL.md` avec frontmatter YAML décrivant
quand l'invoquer, suivi d'instructions pour Claude. Pas de code Python
direct dans la skill (le code reste dans `pipeline/` / `tools/`).

### 4.1 `source-collector` (migration)

**État** : existe déjà dans `~/.claude/plugins/source-collector/`.

**Migration** : copier le `SKILL.md` ici, mettre à jour les chemins
absolus si nécessaires. Le code `lib/*.py` reste accessible via le
sys.path déjà configuré dans `pipeline/config.py`.

**Trigger** : « acquérir les PDFs », « DL ces refs », « cascade ».

**Sous-agent ou direct ?** Reste autonome — peut être invoquée par
`sota-writer` ou `sota-auditor` en sub-task.

### 4.2 `sota-writer` — NOUVEAU (Cas A complet)

**Trigger** : « écris un SOTA sur X », « rédige une revue sur Y »,
`/research:new-sota`.

**Contrat** :
- Entrée : sujet (texte libre), optionnel : domaine cible
  (musicology/biblio_maths/etc.), optionnel : profondeur (nb refs).
- Sortie : un fichier `SOTA_<topic>.md` au bon emplacement
  (`10_SOURCES/<biblio>/`), uniquement avec des refs en
  `sota_cited_confirmed`. Plus un rapport des refs proposées mais
  écartées (cascade_exhausted ou retracted).

**Étapes internes** :
1. Lance `paper-search` MCP (ou alternative) sur le sujet.
2. Pour chaque résultat candidate, propose à l'utilisateur (oui/non).
3. Crée les refs `state=candidate` dans le registre via
   `pipeline.registry.save_ref`.
4. Invoque `source-collector` skill (sub-agent) pour faire passer
   chaque ref dans la FSM.
5. Attend la fin du batch, agrège : OK, retracted, blocked.
6. Rédige le markdown SOTA en s'appuyant uniquement sur les refs
   `page1_validated` / `sota_cited_confirmed`. Cite chaque ref avec
   `[[slug]]`.
7. Liste séparée des refs proposées mais non validées (sous une
   section « Refs écartées » avec raison).

**Garde-fou** : refuse d'écrire le SOTA si plus de 30 % des candidates
échouent à atteindre `page1_validated` (signal que le sujet est trop
flou ou que la cascade a un problème ; humain décide).

### 4.3 `sota-auditor` — NOUVEAU (Cas B partie SOTA)

**Trigger** : « vérifie ce SOTA », « audite mes SOTAs »,
`/research:audit-sota`.

**Contrat** :
- Entrée : chemin SOTA ou wildcard.
- Sortie : rapport markdown classant chaque ref citée en :
  - OK (`sota_cited_confirmed`)
  - À VALIDER (`page1_validated` mais pas encore confirmé sémantique)
  - INACCESSIBLE (`blocked_human:cascade_exhausted` ou
    `blocked_human:no_pdf_available`)
  - HALLUCINÉE (`retracted` ou `blocked_human:title_mismatch`
    + confidence haute)
  - INCONNUE DU REGISTRE (wikilink vers ref qui n'existe pas)
- Option `--purge` : supprime les wikilinks HALLUCINÉE du SOTA, ajoute
  une note en bas du SOTA listant ce qui a été retiré.

**Étapes internes** :
1. Parse le SOTA, extrait tous les `[[slug]]`.
2. Pour chaque slug, cherche `_registry/refs/<slug>.md`.
3. Si absent : crée la ref (state=candidate) via
   `import_asymetry_sotas.py` ou équivalent, signale "INCONNUE".
4. Classifie selon le `state` actuel.
5. Pour les INACCESSIBLES / HALLUCINÉES, déclenche éventuellement un
   re-run worker (option `--retry`).
6. Produit le rapport. Si `--purge`, mute le SOTA.

### 4.4 `citation-verifier` — NOUVEAU (Cas B partie article)

**Trigger** : « vérifie les citations de cet article »,
`/research:audit-article`.

**Contrat** :
- Entrée : fichier LaTeX (`.tex`) ou Markdown.
- Sortie : rapport listant chaque citation et son statut (existante,
  page1_validated, claim vérifiable dans le PDF).

**Étapes** :
1. Parse les `\cite{key}` LaTeX ou `[[slug]]` Markdown.
2. Pour chaque citation, retrouve la ref du registre.
3. Si la ref est `page1_validated`+ et la citation contient un claim
   spécifique (extrait du contexte autour de la citation), lance la
   recherche du claim dans le PDF (équivalent généralisé de
   `verify_claims.py`).
4. Classifie : claim_found / claim_absent / pdf_missing /
   ref_missing.

### 4.5 `article-warner` — NOUVEAU (Cas B sortie)

**Trigger** : « insère les warnings dans cet article »,
`/research:audit-article --warn`.

**Contrat** :
- Entrée : fichier LaTeX/Markdown + rapport `citation-verifier`.
- Sortie : fichier muté avec warnings explicites pour chaque citation
  problématique. Format :
  - LaTeX : `\todo[color=red]{REF NON VALIDÉE: <raison>}` à côté du
    `\cite{key}`
  - Markdown / Obsidian : callout `> [!warning] Ref douteuse : raison`

**Garde-fou** : crée d'abord une copie `.bak` du fichier source.

---

## 5. Commandes slash

Une commande slash = un `commands/<name>.md` avec frontmatter
`description:` + corps qui est le prompt envoyé à Claude quand
l'utilisateur tape `/research:<name> <args>`.

| Commande | Args | Skill principale invoquée |
|---|---|---|
| `/research:new-sota` | `<sujet>` | `sota-writer` |
| `/research:audit-sota` | `<path-or-glob>` | `sota-auditor` |
| `/research:audit-article` | `<path> [--warn]` | `citation-verifier` (+ `article-warner` si `--warn`) |
| `/research:status` | (aucun) | direct CLI : `python -m pipeline status` |
| `/research:worker-run` | `[--state X] [--ref Y] [--limit N]` | direct CLI : `python -m pipeline run …` |
| `/research:worker-doctor` | `[--fix] [--correlate-rtfm]` | direct CLI : `python -m pipeline doctor …` |

---

## 6. Subagents

Trois subagents pour parallélisation et isolation de contexte :

### 6.1 `researcher` (agents/researcher.md)

**Rôle** : prend un sujet ou un titre, interroge `paper-search` MCP +
arXiv/S2/OpenAlex directs, retourne une liste structurée de candidates
(title, authors, year, DOI/arXiv-id, source, confidence_score).

**Outils** : MCP tools + WebFetch (fallback).

### 6.2 `validator` (agents/validator.md)

**Rôle** : prend un PDF + une ref attendue (auteur, titre, année),
lance `validate_pdf_against_ref` (lib existante) et retourne OK / KO +
raison. Wrappe la logique anti-homonymie.

**Outils** : Bash (pour `pdftotext`), Read (pour PDF déjà extraits).

### 6.3 `curator` (agents/curator.md)

**Rôle** : prend une ref `page1_validated` + son contexte de citation,
décide si elle est sémantiquement appropriée (citation correspond à un
claim réel du PDF) → passe `sota_cited_confirmed` ou
`blocked_human:claim_mismatch`.

**Outils** : Read, recherche dans le PDF.

---

## 7. MCP servers

### 7.1 `paper-search` — décision nécessaire

Le SKILL.md de `source-collector` référence des outils
`mcp__paper-search__*` (search_openalex, download_arxiv, etc.) mais ce
MCP n'est **pas dans la config actuelle** (`~/.claude/mcp.json` n'a
que `chrome-devtools`, `neo4j-cypher`, `neo4j-data-modeling`,
`notebooklm`).

Options :
1. **Retrouver et réinstaller** le MCP `paper-search` original
2. Utiliser un MCP communautaire alternatif (`paper-search-mcp`,
   `scholar-mcp`, etc.)
3. **Construire un MCP custom** dans ce plugin (`mcp/paper-search/`)
   qui agrège OpenAlex + arXiv + S2 + Crossref via leurs REST APIs
   (réutilise les helpers `lib/oa_finder.py`, `lib/s2_resolver.py`)

Recommandation : **option 3** si pas de trace de l'original. C'est
~200 LOC Python, tous les helpers existent déjà dans `pipeline/` et
`lib/`. Et ça garde tout dans le plugin.

### 7.2 MCP registry custom ?

Optionnel. Un MCP qui expose le registre YAML comme ressource
queryable. Utile si on veut que d'autres agents IA (hors plugin)
puissent lire l'état du registre. **Hors-scope V1**.

---

## 8. Hooks

À étoffer après MVP. Idées :

- **PreToolUse(Write)** sur un SOTA : refuse l'écriture si certains
  wikilinks pointent vers des refs non-`sota_cited_confirmed`. Force
  l'utilisateur à passer par `/research:audit-sota` d'abord.
- **PostToolUse(Edit)** sur `_registry/refs/*.md` : déclenche un mini
  `doctor` sur la ref modifiée pour catcher les drifts immédiatement.
- **SessionEnd** : `pipeline doctor --severity error` pour rapport en
  fin de session.

---

## 9. Worker B intégré

Aucune réécriture. Le worker B (`pipeline/`) reste tel quel :

- CLI : `python -m pipeline status|run|doctor|events|…`
- Bibliothèque : `from pipeline.registry import iter_refs, save_ref`,
  `from pipeline.doctor import run_doctor_for_cli`, etc.

Les skills l'invoquent via :
- Bash quand on veut un run batch (`python -m pipeline run --state candidate`)
- Import direct quand on a besoin de mutations programmatiques fines

Documentation interne (cf. `pipeline/ARCHITECTURE.md`) reste valide.

---

## 10. Roadmap d'implémentation (phases indépendantes)

### Phase P0 — Structure plugin (1 session)

- `.claude-plugin/plugin.json`
- `README.md` réécrit (le repo est un plugin)
- Squelette `skills/`, `commands/`, `agents/`, `mcp/` avec READMEs
- Migration de `source-collector/SKILL.md` depuis `~/.claude/plugins/`
- Test : installer le plugin localement (`/plugin install file://...`),
  vérifier que `/research:status` fonctionne (déjà mappe sur CLI worker)

**Critère d'acceptance** : le plugin s'installe sans erreur, les
commandes worker-* invoquent bien le CLI existant.

### Phase P1 — MCP `paper-search` custom (1-2 sessions)

- `mcp/paper-search/server.py` agrégeant OpenAlex, arXiv, S2,
  Crossref via leurs APIs publiques
- Réutilise `lib/oa_finder.py`, `lib/s2_resolver.py`,
  `lib/archive_org_helper.py` quand pertinent
- Tools exposés : `search`, `download_by_doi`, `download_by_arxiv_id`,
  `enrich_metadata`

**Critère d'acceptance** : `mcp__paper-search__search` retourne ≥ 5
résultats pour une requête de test connue.

### Phase P2 — Skill `sota-writer` MVP (2-3 sessions)

- `skills/sota-writer/SKILL.md` détaillant les 7 étapes (cf. §4.2)
- Commande `commands/new-sota.md` qui invoque la skill
- Test E2E : `/research:new-sota "Petri nets in music notation"`
  produit un SOTA avec 5-10 refs `page1_validated`

**Critère d'acceptance** : 0 ref hallucinée dans le SOTA produit
(toutes les wikilinks pointent vers refs `page1_validated`+).

### Phase P3 — Skill `sota-auditor` (1-2 sessions)

- `skills/sota-auditor/SKILL.md`
- Commande `commands/audit-sota.md`
- Test : auditer un SOTA réel existant
  (`SOTA_Bernard_Bel_Temperaments_Intonation.md`)
- Implémente l'option `--purge`

**Critère d'acceptance** : rapport correct sur SOTA réel, purge
testable en dry-run sur copie.

### Phase P4 — Skill `citation-verifier` + `article-warner` (2 sessions)

- Généraliser `verify_claims.py` (paramétrable par fichier source)
- `skills/citation-verifier/SKILL.md`
- `skills/article-warner/SKILL.md`
- Commande `commands/audit-article.md` avec `--warn`
- Test : auditer un article LaTeX en cours

**Critère d'acceptance** : warnings insérés correctement dans une
copie `.bak`, format LaTeX et Markdown supportés.

### Phase P5 — Hooks + polish (1 session)

- Hooks `PostToolUse`, `SessionEnd`, `PreToolUse` (cf. §8)
- README + tests d'intégration
- (optionnel) `marketplace.json` pour publication

---

## 11. Décisions humaines à prendre avant Phase P0

1. **Nom final** : `research-anti-hallucination` (long, explicite) /
   `research-rigor` (court) / autre ?
2. **Renommer le repo GitHub** : oui (`roomi-fields/research-anti-hallucination`)
   ou non (garder `references-consolidation`, juste changer le
   `plugin.json`) ?
3. **`paper-search` MCP** : option 1/2/3 du §7.1 ?
4. **Format SOTA produit par `sota-writer`** : markdown Obsidian
   (sections, wikilinks) ou format spécifique ?
5. **Format warnings article-warner** : LaTeX `\todo{}` rouge /
   Markdown callout / autre ?
6. **Inclure ce plugin dans un marketplace** publiable (option pour
   d'autres chercheurs) ou strictement perso ?

---

## 12. Ce qui sort de scope V1

- Sota-curator skill avec confirmation automatique des claims
  (Cas A étape 7 reste humain-in-the-loop)
- MCP registry exposé en lecture pour agents tiers
- Internationalisation EN/FR systématique
- UI graphique (tout reste CLI/markdown)
- Sync registre multi-machines (versionnement git du registre est hors
  scope du plugin)
