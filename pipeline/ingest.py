"""Module INGEST — convertit les SOTAs du vault en source du registre.

Étape 1 du pipeline cible (cf. plans/PIPELINE_CIBLE.md).

Workflow :
1. L'adapter extrait les sections candidates (`extract_bibliography_sections`)
2. Le sub-agent `citation-parser` (côté Claude, non Python) parse chaque
   section en JSON structuré `[{author, year, title, doi?, ...}]`
3. Ce module (`ingest_citations`) prend ce JSON et :
   - identify : résout DOI/UID via Crossref/S2 si absent
   - reconcile : check si une ref existe déjà (DOI strict, sinon fuzzy)
   - create_or_reuse : crée une ref candidate ou réutilise le slug
   - substitute : remplace le texte par `[[slug]]` dans le SOTA

Le backup git est géré en amont par `_ensure_git_backup` (commit auto
avant chaque session INGEST).
"""
from __future__ import annotations
import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from .config import REFS, VAULT
from .registry import load_ref, iter_refs


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses : input (citation parsée) + output (résultat ingestion)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedCitation:
    """Citation extraite par le sub-agent `citation-parser`.

    Format aligné sur le contrat de l'agent (cf. agents/citation-parser.md).
    """
    author: str
    year: str
    title: str
    raw: str
    confidence: str = "high"        # high / medium / low
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    venue: Optional[str] = None
    source_offset: int = 0
    # Champs renseignés en cours de pipeline
    resolved_doi: Optional[str] = None
    matched_slug: Optional[str] = None
    created_slug: Optional[str] = None


@dataclass
class IngestResult:
    """Résultat d'ingestion d'un SOTA.

    Contient des compteurs de qualité observables session par session
    (cf. H6 du plan robustesse) : taux DOI résolus, taux refs matchées vs
    nouvelles, taux substitutions / citations, durée.
    """
    sota_path: Path
    new_refs: list[str] = field(default_factory=list)
    reused_refs: list[str] = field(default_factory=list)
    skipped_low_confidence: list[str] = field(default_factory=list)
    substitutions: int = 0
    errors: list[str] = field(default_factory=list)
    # Compteurs métriques (H6)
    citations_total: int = 0
    doi_resolved: int = 0
    matched_by_doi: int = 0
    matched_by_fuzzy: int = 0
    orphan_pdfs_found: int = 0
    page1_validated: int = 0
    duration_seconds: float = 0.0

    def to_metrics_dict(self) -> dict:
        """Sérialise les métriques en dict JSON-compatible.

        Format consommable par H7 (fixtures de test) et par les hooks de
        monitoring batch.
        """
        try:
            sota_rel = str(self.sota_path.relative_to(VAULT))
        except (ValueError, OSError):
            sota_rel = str(self.sota_path)
        return {
            "sota": sota_rel,
            "citations_total": self.citations_total,
            "doi_resolved": self.doi_resolved,
            "matched_by_doi": self.matched_by_doi,
            "matched_by_fuzzy": self.matched_by_fuzzy,
            "new_refs_created": len(self.new_refs),
            "reused_refs": len(self.reused_refs),
            "orphan_pdfs_found": self.orphan_pdfs_found,
            "page1_validated": self.page1_validated,
            "wikilinks_substituted": self.substitutions,
            "skipped_low_confidence": len(self.skipped_low_confidence),
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": self.errors,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Git backup
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_GITIGNORE = """\
# Fichiers binaires lourds (non versionnés)
**/*.pdf
**/*.epub
**/*.mobi
**/*.djvu

# Dossiers de sources brutes
**/Sources/
**/_quarantine/

# Workspace Obsidian
.obsidian/workspace*
.obsidian/cache
.obsidian/plugins/*/data.json

# Système
.DS_Store
Thumbs.db
*~
"""


def _ensure_git_backup(vault_root: Path, message: str) -> bool:
    """Vérifie que vault_root est un repo git, commit avant INGEST.

    Si pas de `.git/`, refuse de tourner et propose à l'utilisateur de
    lancer `pipeline ingest --init-git` au préalable.

    Retourne True si backup OK, False sinon.
    """
    if not vault_root.exists():
        return False
    git_dir = vault_root / ".git"
    if not git_dir.exists():
        print(f"[ERR] {vault_root} n'est pas un repo git.", flush=True)
        print(f"      Pour initialiser : `pipeline ingest --init-git`",
              flush=True)
        return False
    # Commit les changements en cours avant la modification INGEST.
    # Timeout généreux : git add . peut prendre plusieurs minutes sur
    # un gros vault Obsidian la 1ère fois.
    try:
        subprocess.run(
            ["git", "-C", str(vault_root), "add", "."],
            check=True, capture_output=True, timeout=600,
        )
        result = subprocess.run(
            ["git", "-C", str(vault_root), "commit", "-m", message,
             "--allow-empty"],
            capture_output=True, timeout=120, text=True,
        )
        if result.returncode != 0:
            print(f"[WARN] git commit a échoué : {result.stderr[:200]}",
                  flush=True)
            return False
    except subprocess.TimeoutExpired:
        print("[ERR] git commit timeout (10 min dépassées)", flush=True)
        return False
    except FileNotFoundError:
        print("[ERR] git n'est pas installé sur ce système", flush=True)
        return False
    return True


def init_git_vault(vault_root: Path) -> bool:
    """Initialise un repo git dans le vault, crée .gitignore, premier commit.

    À appeler explicitement via `pipeline ingest --init-git` la première fois.
    """
    if not vault_root.exists():
        print(f"[ERR] vault introuvable : {vault_root}", flush=True)
        return False
    git_dir = vault_root / ".git"
    if git_dir.exists():
        print(f"[NOOP] git déjà initialisé dans {vault_root}", flush=True)
        return True
    gitignore = vault_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8")
        print(f"[ok] .gitignore créé ({len(DEFAULT_GITIGNORE)} chars)",
              flush=True)
    try:
        subprocess.run(["git", "-C", str(vault_root), "init"],
                       check=True, capture_output=True, timeout=30)
        # Sur un gros vault Obsidian, `git add .` peut prendre plusieurs
        # minutes la 1ère fois (10 000+ fichiers à indexer).
        subprocess.run(["git", "-C", str(vault_root), "add", "."],
                       check=True, capture_output=True, timeout=600)
        subprocess.run(
            ["git", "-C", str(vault_root), "commit", "-m",
             "Initial vault state before paper-trail INGEST"],
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERR] git init/commit failed : {e.stderr[:200] if e.stderr else e}",
              flush=True)
        return False
    print(f"[ok] git initialisé dans {vault_root}", flush=True)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Slug generation & normalisation
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_TITLE_STOPWORDS = {
    "a", "an", "the", "of", "to", "for", "in", "on", "and", "or",
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
    "à", "au", "aux", "en", "sur", "par", "pour",
}


_AUTHOR_NOISE_WORDS = {"et", "al", "al.", "and", "&", "etc", "et al"}


def _extract_first_author_lastname(author_text: str) -> str:
    """Extrait le nom de famille du premier auteur.

    Formats acceptés :
      - "Heydari, M. & Mahadevan, M." → "heydari"
      - "Heydari M., Mahadevan M." → "heydari"
      - "M. Heydari & M. Mahadevan" → "heydari"
      - "Heydari et al." → "heydari"
      - "Heydari" → "heydari"
    """
    if not author_text:
        return "unknown"
    # Tronque à "et al" et variantes pour ne pas inclure le bruit
    text = re.sub(r"\bet\s+al\.?", "", author_text, flags=re.IGNORECASE)
    # Coupe au premier séparateur multi-auteurs
    first = re.split(r"\s*(?:&|;|\band\b|,\s+[A-Z])", text, maxsplit=1)[0]
    # Si "Lastname, F." → "Lastname"
    if "," in first:
        last = first.split(",")[0].strip()
    else:
        # "F. Lastname" ou "First Lastname" → dernier mot non-initiale et
        # non-bruit
        words = [
            w for w in first.split()
            if not re.fullmatch(r"[A-Z]\.+", w)
            and _to_ascii_lower(w).rstrip(".") not in _AUTHOR_NOISE_WORDS
        ]
        last = words[-1] if words else first.strip()
    # Normalisation : lower, ascii, alphanum
    last = _to_ascii_lower(last)
    last = re.sub(r"[^a-z0-9]", "", last)
    return last or "unknown"


def _to_ascii_lower(s) -> str:
    """Normalise une chaîne en ASCII lowercase (best-effort).
    Tolère None et chaîne vide.
    """
    if not s:
        return ""
    import unicodedata
    norm = unicodedata.normalize("NFKD", s)
    ascii_str = "".join(c for c in norm if not unicodedata.combining(c))
    return ascii_str.lower()


def _title_first_significant_word(title: str) -> str:
    """Premier mot significatif du titre (skip stopwords)."""
    if not title:
        return "untitled"
    words = re.findall(r"[A-Za-zÀ-ÿ]+", title)
    for w in words:
        if _to_ascii_lower(w) not in _SLUG_TITLE_STOPWORDS and len(w) > 2:
            return _to_ascii_lower(w)
    return _to_ascii_lower(words[0]) if words else "untitled"


def _make_slug(author: str, year: str, title: str) -> str:
    """Génère un slug canonique pour une ref.

    Format : `<lastname>_<year>_<first_significant_title_word>`
    Ex : 'Heydari, M. & Mahadevan, M.' + '2021' + 'BeatNet ...'
        → 'heydari_2021_beatnet'
    """
    last = _extract_first_author_lastname(author)
    yr = re.sub(r"[^0-9]", "", year or "")[:4] or "0000"
    word = _title_first_significant_word(title)
    return f"{last}_{yr}_{word}"


# ─────────────────────────────────────────────────────────────────────────────
# Identification via Crossref / S2 (réutilise lib/)
# ─────────────────────────────────────────────────────────────────────────────

_PAPER_SEARCH_PROJECT = "/home/romi/dev/mcp/paper-search-mcp"
_PAPER_SEARCH_SOURCES = "crossref,openalex,dblp,semantic,openaire,europepmc"
_PAPER_SEARCH_TIMEOUT_S = 35
_DOI_CACHE: dict[tuple[str, str, str], Optional[str]] = {}


def _paper_search_doi(title: str, author: str, year: str) -> Optional[str]:
    """Fallback DOI via paper-search MCP CLI (Crossref/OpenAlex/dblp/S2/...).

    On ne renvoie un DOI que si l'année du résultat matche l'année attendue
    (si fournie) ET si le lastname attendu apparaît dans les auteurs.
    """
    if not title:
        return None
    query_terms = [title.strip()]
    if author:
        query_terms.append(author.strip())
    if year:
        query_terms.append(year.strip())
    query = " ".join(query_terms)
    if not Path(_PAPER_SEARCH_PROJECT).exists():
        return None
    cmd = [
        "uv", "run", "--project", _PAPER_SEARCH_PROJECT,
        "python", "-m", "paper_search_mcp.cli", "search", query,
        "-s", _PAPER_SEARCH_SOURCES,
        "-n", "3",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PAPER_SEARCH_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except Exception:
        return None
    expected_lastname = _to_ascii_lower(_extract_first_author_lastname(author or ""))
    expected_year = (year or "").strip()
    best_doi: Optional[str] = None
    for paper in data.get("papers", []):
        doi = (paper.get("doi") or "").strip()
        if not doi:
            continue
        ptitle = paper.get("title") or ""
        pauthors = str(paper.get("authors") or "").lower()
        pdate = str(paper.get("published_date") or "")
        pyear = pdate[:4] if pdate else ""
        if expected_year and pyear and pyear != expected_year:
            continue
        if expected_lastname and expected_lastname not in pauthors:
            continue
        if title:
            t_norm = _normalize_title(title)
            p_norm = _normalize_title(ptitle)
            if t_norm and p_norm and SequenceMatcher(None, t_norm, p_norm).ratio() < 0.55:
                continue
        best_doi = doi
        break
    return best_doi


def _identify_doi(citation: ParsedCitation) -> Optional[str]:
    """Résout le DOI d'une citation.

    Ordre :
    1. Si citation.doi présent (extrait par le parser) → return
    2. Crossref search par titre+auteur+année (lib/oa_finder.py) — rapide
    3. paper-search MCP CLI : Crossref + OpenAlex + dblp + Semantic Scholar +
       OpenAIRE + Europe PMC — fallback large (~30s)

    Cache LRU sur (lastname, year, title_prefix) pour dédupliquer dans une
    même session INGEST (utile si plusieurs SOTAs citent la même ref).
    """
    if citation.doi:
        return citation.doi.strip()
    lastname = _to_ascii_lower(_extract_first_author_lastname(citation.author or ""))
    year = (citation.year or "").strip()
    title_key = _normalize_title(citation.title or "")[:60]
    cache_key = (lastname, year, title_key)
    if cache_key in _DOI_CACHE:
        return _DOI_CACHE[cache_key]

    doi: Optional[str] = None
    # Niveau 1 : Crossref direct via oa_finder
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
        try:
            from oa_finder import crossref_search_by_title  # type: ignore
        except ImportError:
            crossref_search_by_title = None
        if crossref_search_by_title is not None:
            result = crossref_search_by_title(
                title=citation.title,
                author=citation.author,
                year=citation.year,
            )
            if isinstance(result, dict) and result.get("doi"):
                doi = result["doi"]
    except Exception:
        pass

    # Niveau 2 : paper-search MCP (OpenAlex + dblp + S2 + OpenAIRE + Europe PMC)
    if not doi and (citation.title or "").strip():
        doi = _paper_search_doi(citation.title or "", citation.author or "", year)

    _DOI_CACHE[cache_key] = doi
    return doi


# ─────────────────────────────────────────────────────────────────────────────
# Réconciliation avec le registre (dédup)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """Normalise un titre pour comparaison fuzzy."""
    t = _to_ascii_lower(title)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Cache du registre chargé (pour éviter iter_refs() × N sessions)
_REGISTRY_CACHE: Optional[list] = None

# Connexion SQLite persistante read-only sur la DB RTFM (H3).
# Évite de relancer `subprocess.run(["rtfm", "search", ...])` × N citations
# (chaque démarrage subprocess coûte ~5-10s en cold start ; SQLite direct
# coûte ~50-500ms par requête après la première connexion).
_RTFM_SQLITE_CONN = None  # sqlite3.Connection | None
_RTFM_SQLITE_TRIED = False


def _get_registry_cached() -> list:
    """Charge toutes les refs une seule fois pour la session."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = list(iter_refs())
    return _REGISTRY_CACHE


def _rtfm_sqlite_conn():
    """Ouvre (et met en cache) la connexion SQLite read-only sur la DB RTFM.

    Retourne None si la DB est indisponible ou si l'ouverture a déjà échoué
    pendant la session.
    """
    global _RTFM_SQLITE_CONN, _RTFM_SQLITE_TRIED
    if _RTFM_SQLITE_TRIED:
        return _RTFM_SQLITE_CONN
    _RTFM_SQLITE_TRIED = True
    from .config import RTFM_DB
    if not RTFM_DB.exists():
        return None
    try:
        import sqlite3
        _RTFM_SQLITE_CONN = sqlite3.connect(
            f"file:{RTFM_DB}?mode=ro", uri=True, timeout=5,
        )
    except Exception:
        _RTFM_SQLITE_CONN = None
    return _RTFM_SQLITE_CONN


def _sanitize_fts5_query(q: str) -> str:
    """Nettoie une query pour FTS5 : supprime les caractères réservés
    (`"`, `(`, `)`, `*`) qui pourraient casser le MATCH.
    """
    return re.sub(r"[\"()\*]", " ", q).strip()


def _rtfm_prefilter_registry_slugs(
    citation: ParsedCitation, limit: int = 10
) -> list[str]:
    """RTFM-first : pré-filtre les refs registre candidates via FTS5.

    Stratégie :
    1. SQLite direct sur `chunks_fts` (connexion persistante read-only) —
       ~50-500ms par requête après warmup.
    2. Si SQLite indisponible, fallback subprocess `rtfm search` (~5-10s).
    3. Si tout échoue, retourne [] (l'appelant scanne tout le registre).
    """
    parts = []
    lastname = _extract_first_author_lastname(citation.author)
    if lastname and lastname != "unknown":
        parts.append(lastname)
    if citation.year:
        parts.append(str(citation.year))
    if citation.title:
        first_word = _title_first_significant_word(citation.title)
        if first_word and first_word != "untitled":
            parts.append(first_word)
    if not parts:
        return []
    raw_query = _sanitize_fts5_query(" ".join(parts))
    if not raw_query:
        return []

    # Niveau 1 : SQLite direct via FTS5 + ranking bm25
    con = _rtfm_sqlite_conn()
    if con is not None:
        try:
            rows = con.execute(
                """
                WITH fts AS (
                  SELECT rowid, rank
                    FROM chunks_fts
                   WHERE chunks_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?
                )
                SELECT b.filename, MIN(fts.rank) AS score
                  FROM fts
                  JOIN chunks c ON fts.rowid = c.id
                  JOIN books b ON c.book_id = b.id
                 WHERE b.filename LIKE '%_registry/refs/%'
                 GROUP BY b.id
                 ORDER BY score
                 LIMIT ?
                """,
                (raw_query, limit * 10, limit),
            ).fetchall()
            slugs: list[str] = []
            seen = set()
            for filename, _score in rows:
                slug = Path(filename).stem
                if slug not in seen:
                    seen.add(slug)
                    slugs.append(slug)
            if slugs:
                return slugs
        except Exception:
            pass  # fallback subprocess

    # Niveau 2 (fallback) : subprocess rtfm search
    from .config import RTFM_DB
    try:
        proc = subprocess.run(
            ["rtfm", "search", raw_query, "--db", str(RTFM_DB),
             "--limit", str(limit * 3), "-f", "json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        results = data.get("results") or []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError):
        return []

    slugs = []
    seen = set()
    for r in results:
        f = r.get("file") or r.get("path") or ""
        if "_registry/refs/" not in f:
            continue
        slug = Path(f).stem
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
        if len(slugs) >= limit:
            break
    return slugs


def _reconcile_with_registry(
    citation: ParsedCitation,
    doi: Optional[str],
    title_threshold: float = 0.8,
) -> Optional[str]:
    """Cherche dans le registre une ref qui correspond à cette citation.

    Stratégie RTFM-first :
    1. DOI strict d'abord (sur les refs en cache)
    2. RTFM `rtfm search` pour pré-filtrer 10 candidats max
       → fuzzy match sur ces 10 (auteur+année+titre Levenshtein)
    3. Si RTFM rien ne donne, fallback fuzzy sur tout le registre
    """
    cite_lastname = _extract_first_author_lastname(citation.author)
    cite_year = re.sub(r"[^0-9]", "", citation.year or "")[:4]
    cite_title_norm = _normalize_title(citation.title)

    refs_cache = _get_registry_cached()
    # Index slug → ref pour lookup rapide
    by_slug = {r.slug: r for r in refs_cache}

    # 1. DOI strict (rapide même sur 900 refs)
    if doi:
        doi_lower = doi.lower()
        for ref in refs_cache:
            ref_uid = ref.frontmatter.get("uid") or ""
            if ref_uid.startswith("doi:") and ref_uid[4:].strip().lower() == doi_lower:
                return ref.slug

    # 2. RTFM-first pré-filtrage
    candidate_slugs = _rtfm_prefilter_registry_slugs(citation, limit=10)
    refs_to_check = [by_slug[s] for s in candidate_slugs if s in by_slug]

    # 3. Fallback : tout le registre si RTFM rien
    if not refs_to_check:
        refs_to_check = refs_cache

    # Fuzzy match : auteur exact + année exacte + Levenshtein titre
    for ref in refs_to_check:
        fm = ref.frontmatter
        ref_author = fm.get("author") or ""
        ref_year = re.sub(r"[^0-9]", "", str(fm.get("year") or ""))[:4]
        if not ref_author or not ref_year:
            continue
        if _extract_first_author_lastname(ref_author) != cite_lastname:
            continue
        if ref_year != cite_year:
            continue
        ref_title_norm = _normalize_title(fm.get("title") or "")
        if not ref_title_norm or not cite_title_norm:
            continue
        sim = SequenceMatcher(None, ref_title_norm, cite_title_norm).ratio()
        if sim >= title_threshold:
            return ref.slug
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Création de ref candidate
# ─────────────────────────────────────────────────────────────────────────────

REF_TEMPLATE = """\
---
state: {state}
slug: {slug}
author: {author}
year: '{year}'
title: {title}
{uid_line}{pdf_line}\
created_by: ingest
created_at: '{created_at}'
ingest_source: {sota_relpath}
state_history:
- at: '{created_at}'
  by: ingest
  meta:
    sota: {sota_relpath}
    confidence: {confidence}
{pdf_history_line}\
  state: {state}
---

Reference ingested from {sota_relpath} on {created_at}.

Raw citation text from SOTA :

> {raw}
"""


# Cache module-level pour éviter le rglob() par citation.
# Sur WSL+mount Windows, ~600 PDFs × 29 citations = ~2 min sans cache,
# 1 scan unique au démarrage + lookups dict = ~3 s avec cache.
_PDF_INDEX_CACHE: Optional[dict[str, list[Path]]] = None
_PDF_USED_CACHE: Optional[set[str]] = None


def _build_pdf_index() -> dict[str, list[Path]]:
    """1 seul scan PDFs vault ; clé = '<lastname>_<year>' lowercase.

    Hiérarchie de sources (rapide → fallback) :
    1. `rtfm files "*.pdf"` — utilise l'index RTFM existant si dispo
       (~3s pour 1500 PDFs, retourne les paths relatifs).
    2. `subprocess find` — Linux find (~1s, mais ne traite que ce qui
       est sur disque, pas filtré par état RTFM).
    3. `Path.rglob` Python — fallback ultime (~4 min sur WSL+mount).
    """
    from .config import SOURCES, VAULT, RTFM_DB
    index: dict[str, list[Path]] = {}
    if not SOURCES.exists():
        return index
    key_re = re.compile(r"^([a-z][a-z0-9]*)_((?:19|20)\d{2})", re.IGNORECASE)

    # 1) RTFM-first
    try:
        proc = subprocess.run(
            ["rtfm", "files", "--db", str(RTFM_DB), "*.pdf"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode == 0:
            # Format : "  [corpus] relative_path  (N bytes)"
            line_re = re.compile(r"^\s*\[[^\]]+\]\s+(.+?)\s+\(\d+\s+bytes\)\s*$")
            for line in proc.stdout.splitlines():
                m_line = line_re.match(line)
                if not m_line:
                    continue
                p = VAULT / m_line.group(1)
                m = key_re.match(p.stem.lower())
                if m:
                    key = f"{m.group(1)}_{m.group(2)}"
                    index.setdefault(key, []).append(p)
            if index:
                return index
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 2) find Linux
    try:
        proc = subprocess.run(
            ["find", str(SOURCES), "-name", "*.pdf"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                p = Path(line)
                m = key_re.match(p.stem.lower())
                if m:
                    key = f"{m.group(1)}_{m.group(2)}"
                    index.setdefault(key, []).append(p)
            if index:
                return index
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 3) Fallback rglob Python (lent sur WSL mais fonctionne partout)
    for p in SOURCES.rglob("*.pdf"):
        m = key_re.match(p.stem.lower())
        if m:
            key = f"{m.group(1)}_{m.group(2)}"
            index.setdefault(key, []).append(p)
    return index


def reset_orphan_cache() -> None:
    """Invalide les caches PDF index et used. À appeler entre deux
    sessions ingest si la session n'est pas un nouveau process.
    """
    global _PDF_INDEX_CACHE, _PDF_USED_CACHE
    _PDF_INDEX_CACHE = None
    _PDF_USED_CACHE = None


def _find_orphan_pdf_for_citation(
    citation: "ParsedCitation",
) -> Optional[Path]:
    """Cherche un PDF orphelin dans Sources qui matche `<lastname>_<year>`.

    Caches module-level :
    - `_PDF_INDEX_CACHE` : 1 scan rglob() au 1er appel, puis dict lookup.
    - `_PDF_USED_CACHE` : 1 scan iter_refs() au 1er appel ; mis à jour au
      fur et à mesure (ajout du PDF qu'on associe à chaque appel).

    Pour invalider entre deux sessions ingest dans le même process,
    appeler `reset_orphan_cache()`.
    """
    global _PDF_INDEX_CACHE, _PDF_USED_CACHE
    from .config import SOURCES

    lastname = _extract_first_author_lastname(citation.author)
    year = re.sub(r"[^0-9]", "", citation.year or "")[:4]
    if not lastname or lastname == "unknown" or not year:
        return None

    if _PDF_INDEX_CACHE is None:
        _PDF_INDEX_CACHE = _build_pdf_index()
    if _PDF_USED_CACHE is None:
        _PDF_USED_CACHE = set()
        for ref in iter_refs():
            pp = ref.frontmatter.get("pdf_path")
            if pp:
                try:
                    _PDF_USED_CACHE.add(str((SOURCES / pp).resolve()))
                except OSError:
                    pass

    key = f"{lastname.lower()}_{year}"
    for p in _PDF_INDEX_CACHE.get(key, []):
        resolved = str(p.resolve())
        if resolved in _PDF_USED_CACHE:
            continue
        # Marqué utilisé pour éviter d'associer ce PDF à 2 refs créées
        # dans la même session.
        _PDF_USED_CACHE.add(resolved)
        return p
    return None


def _try_validate_page1(pdf_path: Path, citation: "ParsedCitation") -> tuple[bool, str]:
    """Valide la page 1 d'un PDF contre les attributs d'une citation.

    Hiérarchie RTFM-first :
    1. Si RTFM a indexé le PDF avec du texte (chunks > 0, searchable),
       valider via `validate_text_against_ref` sur les chunks RTFM.
       Évite pdftotext (~5-30s/PDF sur WSL).
    2. Fallback : `validate_pdf_against_ref` qui utilise pdftotext.
    """
    expected_author = citation.author or ""
    expected_year = str(citation.year or "")
    expected_title = citation.title or ""

    # 1) RTFM-first
    try:
        from .rtfm_helper import rtfm_status_for_ref, rtfm_first_chunks_text
        from .config import SOURCES
        verdict, info = rtfm_status_for_ref(pdf_path, sources_root=SOURCES)
        if verdict == "ok" and info.get("chunks", 0) > 0:
            rtfm_slug = info.get("rtfm_slug") or ""
            rtfm_text = rtfm_first_chunks_text(rtfm_slug, n_chunks=10) if rtfm_slug else ""
            if rtfm_text:
                import sys as _sys
                _sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
                import validate_pdf_content as v
                return v.validate_text_against_ref(
                    rtfm_text,
                    expected_author=expected_author,
                    expected_year=expected_year,
                    expected_title=expected_title,
                )
    except Exception:
        pass

    # 2) Fallback pdftotext (lent sur WSL)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
        import validate_pdf_content as v
        return v.validate_pdf_against_ref(
            pdf_path,
            expected_author=expected_author,
            expected_year=expected_year,
            expected_title=expected_title,
        )
    except Exception as e:
        return False, f"validation_error:{type(e).__name__}"


def _create_ref(
    citation: ParsedCitation,
    doi: Optional[str],
    sota_path: Path,
) -> str:
    """Crée une nouvelle ref candidate dans le registre.

    Retourne le slug créé. Si un slug existe déjà avec un fichier, on
    ajoute un suffixe `_2`, `_3`, etc. pour éviter l'écrasement.
    """
    base_slug = _make_slug(citation.author, citation.year, citation.title)
    slug = base_slug
    i = 2
    while (REFS / f"{slug}.md").exists():
        slug = f"{base_slug}_{i}"
        i += 1

    uid_line = ""
    if doi:
        uid_line = f"uid: 'doi:{doi}'\n"
    elif citation.arxiv_id:
        uid_line = f"uid: 'arxiv:{citation.arxiv_id}'\n"

    try:
        sota_relpath = str(sota_path.relative_to(VAULT))
    except ValueError:
        sota_relpath = str(sota_path)

    # Cherche un PDF orphelin matchant (pour les refs "Local" qui ont
    # leur PDF déjà sur disque). Si trouvé + page 1 valide → state
    # directement page1_validated.
    orphan = _find_orphan_pdf_for_citation(citation)
    state = "candidate"
    pdf_line = ""
    pdf_history_line = ""
    if orphan is not None:
        from .config import SOURCES
        rel_pdf = str(orphan.relative_to(SOURCES))
        is_ok, reason = _try_validate_page1(orphan, citation)
        if is_ok:
            state = "page1_validated"
            # Calculer sha256 du PDF
            import hashlib
            try:
                sha = hashlib.sha256(orphan.read_bytes()).hexdigest()
                pdf_line = (
                    f"pdf_path: {rel_pdf}\n"
                    f"pdf_sha256: '{sha}'\n"
                    f"pdf_origin: orphan_match_at_ingest\n"
                )
                pdf_history_line = (
                    f"    pdf_path: {rel_pdf}\n"
                    f"    pdf_origin: orphan_match\n"
                    f"    page1_validation: ok\n"
                )
            except OSError:
                state = "candidate"
                pdf_line = ""
        else:
            # PDF trouvé mais page 1 ne valide pas — on garde candidate
            # avec un flag pour audit ultérieur.
            pdf_line = (
                f"# PDF orphelin trouvé mais page 1 invalide ({reason}) — "
                f"non associé\n"
            )

    content = REF_TEMPLATE.format(
        slug=slug,
        state=state,
        author=_yaml_quote(citation.author),
        year=citation.year or "0000",
        title=_yaml_quote(citation.title),
        uid_line=uid_line,
        pdf_line=pdf_line,
        pdf_history_line=pdf_history_line,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        sota_relpath=sota_relpath,
        confidence=citation.confidence,
        raw=citation.raw.replace("\n", " ").strip()[:300],
    )
    REFS.mkdir(parents=True, exist_ok=True)
    (REFS / f"{slug}.md").write_text(content, encoding="utf-8")
    return slug


def _yaml_quote(s: str) -> str:
    """Quote a YAML scalar value safely (single-quoted, escape singles)."""
    if not s:
        return "''"
    safe = s.replace("'", "''")
    return f"'{safe}'"


# ─────────────────────────────────────────────────────────────────────────────
# Substitution texte → [[wikilink]]
# ─────────────────────────────────────────────────────────────────────────────

def _wikilink_for_slug(slug: str) -> str:
    """Pour un slug du registre, retourne le wikilink complet à insérer.

    Format Obsidian : `[[<target>|<alias>]]`
    - target = nom de fichier complet avec extension (Heydari_2021_BeatNet_..._joi.pdf)
      → clic ouvre le PDF
    - alias = forme courte lisible (`heydari_2021`) → affichage compact

    Si la ref n'a pas de pdf_path : fallback `[[slug]]` (pointe vers la
    fiche registre).
    """
    ref_path = REFS / f"{slug}.md"
    if not ref_path.exists():
        return f"[[{slug}]]"
    ref = load_ref(ref_path)
    if not ref:
        return f"[[{slug}]]"

    pdf_path = ref.frontmatter.get("pdf_path")
    if not pdf_path:
        return f"[[{slug}]]"

    target = Path(pdf_path).name  # avec extension
    # Alias court : lastname_year (extrait du slug registre qui est déjà
    # de la forme lastname_year_word)
    parts = slug.split("_")
    if len(parts) >= 2:
        alias = f"{parts[0]}_{parts[1]}"
    else:
        alias = slug
    return f"[[{target}|{alias}]]"


_LIST_MARKER_RE = re.compile(r"^(\s*(?:[-*+]|\d+\.|\|\s*\d+\s*\||\|)\s+)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def _line_already_has_lastname_wikilink(line: str, lastname_anorm: str) -> bool:
    """True si la ligne contient déjà un wikilink (`[[target]]` ou
    `[[target|alias]]`) dont la target ou l'alias contient le lastname
    (comparaison alphanum, tolère le tiret de Vijay-Shanker).
    """
    if not lastname_anorm:
        return False
    for m in _WIKILINK_RE.finditer(line):
        target = m.group(1).lower()
        target_anorm = re.sub(r"[^a-z0-9]", "", target)
        if lastname_anorm in target_anorm:
            return True
    return False


def _prefix_line_with_wikilink(line: str, wikilink: str) -> str:
    """Insère `wikilink — ` en tête de ligne en préservant le marqueur de
    liste s'il y en a un.
    """
    m = _LIST_MARKER_RE.match(line)
    if m:
        return f"{m.group(1)}{wikilink} — {line[m.end():]}"
    # Pas de marqueur (paragraphe libre)
    return f"{wikilink} — {line}"


def _normalize_for_match(s: str) -> str:
    """Normalise une chaîne pour comparaison fuzzy : retire markdown gras,
    italique, ponctuation décorative.
    """
    s = re.sub(r"[*_`]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _substitute_to_wikilink(
    sota_path: Path,
    citation: ParsedCitation,
    slug: str,
) -> bool:
    """Substitue la citation dans le SOTA par un wikilink en tête de ligne.

    Stratégie en cascade (H4) :

    1. **Match strict** : `raw` exactement présent → préfixe avec wikilink.
    2. **Ancrage scoré** : pour chaque ligne candidate non-wikilinkée
       contenant le lastname, on calcule un score (lastname=0.4, year=0.4,
       premier mot du titre=0.2) augmenté de la similarité au `raw`. La
       meilleure ligne au-dessus du seuil 0.55 est préfixée.
    3. Sinon, échec → False.

    Idempotence : si le slug apparaît déjà dans le texte d'une ligne
    candidate, cette ligne est ignorée.

    Retourne True si une substitution a été faite.
    """
    try:
        text = sota_path.read_text(encoding="utf-8")
    except OSError:
        return False
    raw = citation.raw.strip()
    wikilink = _wikilink_for_slug(slug)
    raw_norm = _normalize_for_match(raw) if raw else ""

    # Calcul du lastname normalisé (alphanum) pour l'idempotence T2
    lastname = _extract_first_author_lastname(citation.author)
    lastname_lc = lastname.lower() if lastname and lastname != "unknown" else ""
    lastname_anorm = re.sub(r"[^a-z0-9]", "", lastname_lc)

    def _alphanum(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    # Tier 1 : match strict du raw, ligne par ligne. Une ligne contenant
    # le raw est substituée SAUF si elle a déjà un wikilink avec ce lastname
    # (idempotence locale, pas globale — on substitue les autres occurrences
    # même si une ligne de récap est déjà wikilinkée).
    if raw and raw in text:
        new_lines = []
        any_subst = False
        for line in text.split("\n"):
            if (raw in line and not _line_already_has_lastname_wikilink(
                    line, lastname_anorm)):
                new_lines.append(line.replace(raw, f"{wikilink} — {raw}", 1))
                any_subst = True
            else:
                new_lines.append(line)
        if any_subst:
            sota_path.write_text("\n".join(new_lines), encoding="utf-8")
            return True

    # Tier 2 : ancrage scoré ligne par ligne (besoin du lastname)
    if not lastname_lc:
        return False
    year_str = re.sub(r"[^0-9]", "", citation.year or "")[:4]
    title_word = _title_first_significant_word(citation.title or "")
    title_word_lc = title_word.lower() if title_word and title_word != "untitled" else ""

    best_line: Optional[str] = None
    best_score = 0.0
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 6:
            continue
        # Idempotence forte (T2) : skip si un wikilink existe déjà sur la
        # ligne avec ce lastname (ex: [[sipser_0000_introduction]] est là,
        # on n'ajoute pas un autre [[sipser_2012_...]] à côté).
        if _line_already_has_lastname_wikilink(line, lastname_anorm):
            continue
        line_lc = line.lower()
        line_anorm = _alphanum(line_lc)
        # Required minimum : lastname doit apparaître (normalisé alphanum
        # pour matcher "Vijay-Shanker" ↔ "vijayshanker")
        if lastname_anorm and lastname_anorm not in line_anorm:
            continue
        score = 0.4  # lastname matché
        if year_str and year_str in line:
            score += 0.4
        if title_word_lc and title_word_lc in line_lc:
            score += 0.2
        # Bonus similarité avec le raw normalisé
        if raw_norm:
            line_norm = _normalize_for_match(line)
            sim = SequenceMatcher(None, line_norm[:200], raw_norm[:200]).ratio()
            score += sim * 0.3
        if score > best_score:
            best_score = score
            best_line = line

    # Seuil : 0.55 sans year OU 0.7 avec year (réduit faux positifs)
    threshold = 0.55 if not year_str else 0.7
    if best_line is None or best_score < threshold:
        return False

    # Position d'insertion : juste devant le lastname dans la ligne, pas en
    # tête. Permet de gérer correctement les bullets multi-citations type
    # "- **Local** : Hopcroft FR, Sipser FR, Carton FR".
    # Si année connue : on cherche `lastname...year` proches pour éviter
    # de matcher le lastname dans un nom composé (ex "Cocke-Younger-Kasami").
    insert_pos = None
    if year_str:
        m = re.search(
            rf"\b{re.escape(lastname)}\b[^\n]{{0,80}}{re.escape(year_str)}",
            best_line, re.IGNORECASE,
        )
        if m:
            insert_pos = m.start()
    if insert_pos is None:
        m = re.search(rf"\b{re.escape(lastname)}\b", best_line, re.IGNORECASE)
        if m:
            insert_pos = m.start()

    if insert_pos is not None:
        new_line = (
            best_line[:insert_pos] + f"{wikilink} — " + best_line[insert_pos:]
        )
    else:
        # Fallback : préfixe en tête de ligne (préserve marker de liste)
        new_line = _prefix_line_with_wikilink(best_line, wikilink)
    if new_line == best_line:
        return False
    new_text = text.replace(best_line, new_line, 1)
    if new_text == text:
        return False
    sota_path.write_text(new_text, encoding="utf-8")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrateur principal
# ─────────────────────────────────────────────────────────────────────────────

def ingest_citations(
    sota_path: Path,
    citations: list[ParsedCitation],
    apply: bool = False,
    skip_low_confidence: bool = True,
) -> IngestResult:
    """Orchestrateur : pour chaque citation parsée par le sub-agent,
    résout DOI, dédup contre registre, crée ou réutilise une ref, et
    (si apply=True) substitue le texte par un wikilink dans le SOTA.

    Args:
      sota_path: chemin du SOTA (les substitutions s'y appliquent)
      citations: liste de ParsedCitation déjà parsées par citation-parser
      apply: True = applique les substitutions et crée les refs.
             False = dry-run, ne touche à rien.
      skip_low_confidence: si True, skip les citations confidence=low

    Retourne un IngestResult.
    """
    import time
    result = IngestResult(sota_path=sota_path)
    result.citations_total = len(citations)
    t_start = time.time()
    for cit in citations:
        if skip_low_confidence and cit.confidence == "low":
            result.skipped_low_confidence.append(cit.raw[:60])
            continue
        try:
            doi = _identify_doi(cit)
            cit.resolved_doi = doi
            if doi:
                result.doi_resolved += 1
            existing_slug = _reconcile_with_registry(cit, doi)
            if existing_slug:
                cit.matched_slug = existing_slug
                slug = existing_slug
                result.reused_refs.append(slug)
                if doi:
                    result.matched_by_doi += 1
                else:
                    result.matched_by_fuzzy += 1
            else:
                if apply:
                    slug = _create_ref(cit, doi, sota_path)
                    cit.created_slug = slug
                    result.new_refs.append(slug)
                    # Détecter PDF orphelin trouvé + validé page 1 via state final
                    try:
                        ref_path = REFS / f"{slug}.md"
                        if ref_path.exists():
                            ref = load_ref(ref_path)
                            if ref and ref.frontmatter.get("pdf_path"):
                                result.orphan_pdfs_found += 1
                            if ref and ref.frontmatter.get("state") == "page1_validated":
                                result.page1_validated += 1
                    except Exception:
                        pass
                else:
                    # Dry-run : on calcule juste le slug qui serait créé
                    slug = _make_slug(cit.author, cit.year, cit.title)
                    result.new_refs.append(f"{slug} (dry-run)")
                    continue
            if apply:
                if _substitute_to_wikilink(sota_path, cit, slug):
                    result.substitutions += 1
        except Exception as e:
            result.errors.append(
                f"{type(e).__name__}: {e} (citation: {cit.raw[:60]!r})"
            )
    result.duration_seconds = time.time() - t_start
    return result


def ingest_citations_from_json(
    sota_path: Path,
    citations_json_path: Path,
    apply: bool = False,
) -> IngestResult:
    """Charge les citations depuis un fichier JSON et lance ingest_citations.

    Le JSON est le format de sortie du sub-agent `citation-parser` :
        [{"author": "...", "year": "...", "title": "...", ...}, ...]
    """
    raw = citations_json_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"Le JSON doit être une liste, pas {type(data).__name__}")
    citations = [ParsedCitation(**c) for c in data]
    return ingest_citations(sota_path, citations, apply=apply)
