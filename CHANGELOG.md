# Changelog

All notable changes to the `paper-trail` plugin are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-28

Major rework of the INGEST pipeline : split into 4 orthogonal passes
(identify / purge / acquire / linkify) + chronic SOTA ↔ registry
coherence guarantee. Breaking semantic change in the `citation-parser`
sub-agent contract.

### Added

- **`pipeline/sota_sync.py`** — central utility for propagating slug
  mutations (retract, merge) to all SOTAs in the vault. Replaces the
  silent desynchronization where `cmd_arbitrate retract` or
  `cmd_resolve_textbooks merge_into` mutated the registry without
  updating the wikilinks in SOTAs.

- **Automatic sync hook** : `cmd_arbitrate decision=retract`,
  `cmd_resolve_textbooks action=merge_into`, and
  `cmd_retract_uncited --apply` now trigger `update_wikilinks_in_sotas`
  automatically. Invariants I22/I23 become self-healing for future
  mutations.

- **Test suites** : `pipeline/tests/test_sota_sync.py` (9/9 unit),
  `pipeline/tests/test_p2_sync_branchements.py` (2/2 integration).

### Changed

- **`agents/citation-parser.md` v2** (breaking semantic) :
  - Rule 10 (NEW) : `raw` must be a strict literal substring of
    `input_text`. Enrichment of `year`/`title` from context is OK
    but `raw` stays the local short mention.
  - Rule 11 (NEW) : multiple mentions of the same work produce
    multiple records, NOT one. Replaces the old destructive dedup
    rule 3.last ("return ONE record with the most complete mention").
  - Consequence : table cells like `| Younger 1967 |` now produce a
    record with `raw="Younger 1967"` (instead of being absorbed by the
    full citation), enabling wikilink substitution in tables.

- **`pipeline/ingest.py::ingest_citations`** : added validation that
  `cit.raw` is a literal substring of the SOTA text. Mismatch is logged
  in `IngestResult.errors` (not blocking — Tier 2 anchoring still
  catches via fuzzy match).

### Plan refonte INGEST — phases restantes

See `plans/compressed-painting-squid.md` for details.

- P4 — `pipeline/purge.py` + `/paper-trail:purge` (cleanup wikilinks
  invalides : retracted, `_0000_*` orphans, ugly suffixes `_2_3_4`,
  technical paths `20_ATLAS/`, `.canvas`).
- P5 — `pipeline/identify.py` + `pipeline/linkify.py` + idempotent
  `## Statut des sources` section at the bottom of each SOTA.
- P6 — `pipeline/acquire.py` (targeted cascade wrapper).
- P7 — Auto-fix I22/I23 in `pipeline doctor --fix`.
- P8 — `/paper-trail:registry-cleanup` + global invariance tests.

## [0.1.0] — 2026-05-25

First release. Anti-hallucination Claude Code plugin for academic
research. Research-first workflow, strict state machine, 10-source
acquisition cascade, page 1 anti-homonymy validation, per-citation
audit.

### Added

#### Acquisition and validation engine

- **State machine (8 states)**: `candidate`, `uid_resolved`,
  `pdf_acquired`, `awaiting_rtfm_ocr`, `needs_reacquisition`,
  `page1_validated`, `sota_cited_confirmed`, `retracted` (plus
  `blocked_human:*` variants)
- **Acquisition cascade (8 legal sources)**: Crossref OA, arXiv,
  OpenAlex, Unpaywall, HAL, CORE, archive.org, WebSearch queue
- **Two shadow libraries opt-in**: Sci-Hub and Anna's Archive
  activated only via `RESEARCH_ENABLE_SHADOW_LIBS=1` (see
  `DISCLAIMER.md`)
- **Page 1 anti-homonymy validation**: required before accepting any
  downloaded PDF (expected author, title similarity ≥ 0.3, zero
  off-domain keywords)
- **19 mechanical invariants** (I1-I19) with safe auto-fix for
  cosmetic drift (I4, I6, I9, plus I5/I7 semi)
- **WorkerLock** (`fcntl`) to prevent concurrent mutating sessions
- **Per-source circuit breakers** with open-after-N-failures logic
- **Post-write validation** on every registry save (immediate
  rejection if YAML corrupts)
- **JSONL event log** with `pipeline events --since DATE --to STATE`
- **RTFM bridge** for OCR integration and failure correlation

#### Claude Code plugin layer

- **6 skills**: `pdf-cascade`, `registry-doctor`, `sota-writer`,
  `sota-auditor`, `citation-receipts`, `paper-writer`
- **9 slash commands**: `/paper-trail:status`, `:cascade`, `:doctor`,
  `:reactivate-ocr`, `:new-sota`, `:audit-sota`, `:audit-article`,
  `:receipts`, `:new-paper`
- **4 sub-agents**: `cascade-runner`, `page1-validator`, `researcher`,
  `claim-checker`
- **3 hooks**: `PreToolUse` (refuses writing a SOTA citing
  unvalidated references), `PostToolUse` (mini consistency check on
  edited reference), `SessionEnd` (full consistency sweep)
- **3 vault adapters**: `obsidian` (default), `flat`, `zotero` (V2
  stub)
- **5 Python utilities**: `reset_registry.py`, `identify_pdfs.py`,
  `citation_audit.py`, `precheck_sota_wikilinks.py`,
  `reinject_legacy_blocked.py`
- **Mechanical coverage guard** (`assert_coverage.py`) refuses to
  ship a new version without explicit test evidence for each
  component (4 fixes + 19 invariants + 6 skills)
- **Configuration via environment variables**: `RESEARCH_VAULT_PATH`,
  `RESEARCH_SOURCES_PATH`, `RESEARCH_REGISTRY_PATH`,
  `RESEARCH_VAULT_LAYOUT`, `RESEARCH_ENABLE_SHADOW_LIBS`,
  `RESEARCH_ENABLE_NOTEBOOKLM`, `RESEARCH_SKIP_END_DOCTOR`

#### Documentation

- README with quick start and architecture overview
- `docs/USAGE.md` — daily workflows
- `docs/ARCHITECTURE.md` — system diagrams (Mermaid)
- `docs/LEGAL.md` — licensing and attribution detail
- `DISCLAIMER.md` — shadow libraries opt-in policy and jurisdictional
  responsibilities
- `NOTICE.md` — third-party attribution
- `CHANGELOG.md`

### Inspiration patterns (no code copied)

- [`paper-fetch`](https://github.com/Agents365-ai/paper-fetch) (MIT):
  stable JSON output format, file naming convention
- [`receipts`](https://github.com/JamesWeatherhead/receipts) (MIT):
  local PDF↔claim audit pattern, `RECEIPTS.md` format
- [`phd-skills`](https://github.com/fcakyon/phd-skills) (MIT):
  integrity hooks (PreToolUse, PostToolUse, SessionEnd)
- [`claude-knowledge-vault`](https://github.com/Psypeal/claude-knowledge-vault)
  (MIT): YAML frontmatter for Obsidian, Sci-Hub opt-in pattern
- [`academic-research-skills`](https://github.com/Imbad0202/academic-research-skills)
  (CC BY-NC 4.0): research-write-review-revise pipeline architecture
  (**concept only, no code copied**)

See `NOTICE.md` for full attribution.

### Known limitations

- **Zotero adapter**: stub, raises `NotImplementedError`. Planned
  for V0.2.
- **Full ARS-style writing pipeline**: `sota-writer` covers the
  essential research-first workflow but the 10-stage pipeline with
  reviewer/revision/finalize stages is not implemented in V0.1.
- **`paper-search` MCP**: referenced by `sota-writer` and `researcher`
  agent but must be configured by the user in `~/.claude/mcp.json`
  (not bundled with the plugin).
- **WSL2 drvfs**: I/O performance on `/mnt/d/` is noticeably slower
  than native filesystems during large audits.

### Roadmap V0.2

- Full ARS-style writing pipeline (review + revision + finalize)
- Zotero adapter implementation
- Optional bundled `paper-search` MCP alternative
- Enriched RTFM correlation invariants (use `rtfm check --slug -f json`
  for persistent failure flags)
- Automated E2E test suite on representative fixtures

---

[0.1.0]: https://github.com/roomi-fields/paper-trail/releases/tag/v0.1.0
