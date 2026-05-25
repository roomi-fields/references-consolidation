# paper-trail

> Anti-hallucination plugin for academic research in Claude Code.
> Create literature reviews and papers guaranteed without fabricated
> citations.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](CHANGELOG.md)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-purple.svg)](https://docs.claude.com/en/docs/claude-code/plugins)

## Why this exists

Citation hallucinations in academic writing — fabricated quotes,
inverted attributions, misidentified authors — are systemic when the
author writes from memory and looks up sources afterward. Retracted
papers from major venues have shown the failure mode is structural,
not anecdotal.

`paper-trail` makes the error **mechanically impossible** by enforcing
a research-first workflow with strict state transitions, automated PDF
acquisition with anti-homonymy validation, and per-citation audit
against the actual source text.

## Features

- **Strict state machine (8 states)** — no reference can be cited
  without traversing acquisition → page 1 validation → claim
  verification
- **Anti-homonymy guard** — page 1 of each downloaded PDF is matched
  against expected author, title, and domain keywords before
  acceptance
- **10-source acquisition cascade** — Crossref OA, arXiv, OpenAlex,
  Unpaywall, HAL, CORE, archive.org, WebSearch queue (+ Sci-Hub and
  Anna's Archive available as **strict opt-in**, see
  [`DISCLAIMER.md`](DISCLAIMER.md))
- **19 mechanical invariants** — automated registry health check with
  safe auto-fix for cosmetic drift
- **Inverted writing workflow** — researches and reads first, writes
  last; refuses to draft when source corpus is too thin
- **Per-citation audit** — generates `RECEIPTS.md` classifying each
  citation as VALID / ADJUST / INVALID / UNVERIFIABLE
- **Vault-agnostic** — works with Obsidian, flat markdown, or Zotero
  layouts (adapter pattern)
- **Mechanical coverage guard** — refuses to ship a new version
  without explicit test evidence for each component

## Supported sources

paper-trail's acquisition cascade and search layer cover the major
academic indexing and full-text platforms.

### Open access (default, always enabled)

| Source | Coverage | Used for |
|---|---|---|
| **Crossref** | Cross-domain DOI registry, 150M+ records | DOI lookup, metadata, open-access URL |
| **arXiv** | Preprints (CS, math, physics, q-bio, q-fin, stats, EE) | Full-text PDFs of preprints |
| **OpenAlex** | 200M+ scholarly works, cross-domain aggregator | Metadata, abstracts, citation graph |
| **Unpaywall** | 30M+ free full-text articles | OA PDF discovery |
| **HAL** | French academic repository | Full-text, OA |
| **CORE** | UK-based aggregator, 200M+ OA records | Full-text fallback |
| **archive.org** | Digitized books and articles, Internet Archive | Books, older publications, scans |
| **Semantic Scholar** | AI-curated academic graph, 200M+ papers | Cross-reference, related papers |
| **PubMed / PMC / bioRxiv / medRxiv** | Biomedical, preprints | Biomedical full-text |
| **Zenodo / SSRN / DBLP / DOAJ / BASE / IACR / EuropePMC** | Cross-domain | Additional metadata and full-text |

### Shadow libraries (strict opt-in, see DISCLAIMER.md)

| Source | Coverage | Activation |
|---|---|---|
| **Sci-Hub** | Paywalled scholarly literature, ~88M papers | `RESEARCH_ENABLE_SHADOW_LIBS=1` |
| **Anna's Archive** | Books and articles, aggregates Library Genesis, Sci-Hub, Z-Library | `RESEARCH_ENABLE_SHADOW_LIBS=1` |

Shadow-library activation is **explicit** and **per-session**. A
disclaimer prints to stderr on first use. The user is responsible
for legal compliance in their jurisdiction. See
[`DISCLAIMER.md`](DISCLAIMER.md).

### Local indexing (optional MCP integrations)

| Source | Coverage | Activation |
|---|---|---|
| **paper-search MCP** | Unified API over 22 platforms above | Configure in `~/.claude/mcp.json` |
| **NotebookLM MCP** | Books corpus (Q&A with citations) | `RESEARCH_ENABLE_NOTEBOOKLM=1` + MCP config |
| **RTFM MCP** | Local indexed corpus (code, docs, research) | Configure in `~/.claude/mcp.json` |

## Quick start

### Install

In a Claude Code session:

```
/plugin install file:///path/to/paper-trail
```

Or via marketplace (when published):

```
/plugin marketplace add roomi-fields
/plugin install paper-trail
```

### Configure

Minimum configuration in your shell profile or project `.env`:

```bash
export RESEARCH_VAULT_PATH=/path/to/your/vault
export RESEARCH_VAULT_LAYOUT=obsidian   # or 'flat' or 'zotero' (V2)
```

For complete configuration, see [`docs/USAGE.md`](docs/USAGE.md).

### Try it

```
/paper-trail:status              # overview of the registry
/paper-trail:new-sota "your research topic"
/paper-trail:audit-article path/to/your/paper.tex
```

## How it works

Three primary workflows, all enforced by the plugin's state machine
and pre-write hooks:

### Creating a new literature review

```
/paper-trail:new-sota "topic"
```

1. Multi-source search across 22 academic platforms via the
   `paper-search` MCP
2. You select candidate references from proposed matches
3. Automated PDF acquisition with mandatory page 1 anti-homonymy
   validation
4. Structured notes extracted into each reference's markdown body
5. Final SOTA cites **only** references that reached
   `page1_validated` state; rejected candidates are listed with
   reasons

### Auditing an existing literature review or paper

```
/paper-trail:audit-sota path/to/SOTA.md [--purge]
/paper-trail:audit-article path/to/paper.tex [--warn]
```

Classifies each citation, optionally purges hallucinations from the
SOTA (with `.bak` backup), or inserts inline warnings adjacent to
problematic citations in the paper.

### Daily registry maintenance

```
/paper-trail:cascade <slug>      # acquire a specific reference
/paper-trail:doctor [--fix]      # check and repair registry consistency
/paper-trail:reactivate-ocr      # resume OCR-waiting refs
```

## Architecture overview

```
User → 9 slash commands → 6 skills → 4 sub-agents → Worker engine
                                                     ↓
                                                YAML registry
```

- **Skills** orchestrate (Claude Code markdown)
- **Sub-agents** isolate heavy work (PDF reading, search) from main
  context
- **Worker engine** (Python) enforces the FSM, runs the cascade,
  checks invariants — deterministic, testable
- **YAML registry** is the source of truth (one file per reference,
  Obsidian-compatible frontmatter)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full
diagram and [`pipeline/ARCHITECTURE.md`](pipeline/ARCHITECTURE.md)
for the worker engine internals.

## Documentation

- [`docs/USAGE.md`](docs/USAGE.md) — daily workflows
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system overview
- [`docs/LEGAL.md`](docs/LEGAL.md) — licensing and attribution
- [`DISCLAIMER.md`](DISCLAIMER.md) — shadow libraries opt-in policy
- [`NOTICE.md`](NOTICE.md) — third-party attributions
- [`CHANGELOG.md`](CHANGELOG.md) — version history

## Configuration via environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RESEARCH_VAULT_PATH` | `~/research_vault` | Vault root |
| `RESEARCH_SOURCES_PATH` | `$VAULT/sources` | PDF directory |
| `RESEARCH_REGISTRY_PATH` | `$SOURCES/_registry` | YAML registry |
| `RESEARCH_VAULT_LAYOUT` | `obsidian` | Adapter (obsidian / flat / zotero) |
| `RESEARCH_ENABLE_SHADOW_LIBS` | unset | Enable Anna's Archive & Sci-Hub (opt-in, see DISCLAIMER) |
| `RESEARCH_ENABLE_NOTEBOOKLM` | unset | Enable NotebookLM in `sota-writer` phase A |
| `RESEARCH_SKIP_END_DOCTOR` | unset | Skip the SessionEnd consistency check |

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

This plugin builds on patterns and components from several open-source
projects in the Claude Code ecosystem. See [`NOTICE.md`](NOTICE.md)
for detailed attributions.

## Contributing

Issues and pull requests welcome at
[github.com/roomi-fields/paper-trail/issues](https://github.com/roomi-fields/paper-trail/issues).
The plugin is in active development; structural changes may happen
between minor versions before v1.0.
