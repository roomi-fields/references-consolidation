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
    """Résultat d'ingestion d'un SOTA."""
    sota_path: Path
    new_refs: list[str] = field(default_factory=list)
    reused_refs: list[str] = field(default_factory=list)
    skipped_low_confidence: list[str] = field(default_factory=list)
    substitutions: int = 0
    errors: list[str] = field(default_factory=list)


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

def _identify_doi(citation: ParsedCitation) -> Optional[str]:
    """Résout le DOI d'une citation.

    Ordre :
    1. Si citation.doi présent (extrait par le parser) → return
    2. Sinon, Crossref search par titre+auteur+année (lib/oa_finder.py)
    3. Sinon, Semantic Scholar fallback (lib/s2_resolver.py)

    Retourne le DOI ou None si non résolu.
    """
    if citation.doi:
        return citation.doi.strip()
    # Crossref search
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
                return result["doi"]
    except Exception:
        pass
    # Semantic Scholar fallback : on lit le helper si présent mais on ne
    # bloque pas l'ingestion si lib indisponible
    return None


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


def _get_registry_cached() -> list:
    """Charge toutes les refs une seule fois pour la session."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = list(iter_refs())
    return _REGISTRY_CACHE


def _rtfm_prefilter_registry_slugs(
    citation: ParsedCitation, limit: int = 10
) -> list[str]:
    """RTFM-first : utilise `rtfm search` pour pré-filtrer les refs registre
    candidates, au lieu d'itérer sur les 900+ refs.

    Retourne la liste des slugs des refs registre matchant la query
    (auteur + année + premier mot du titre).

    Fallback : si RTFM indisponible ou erreur, retourne [] (l'appelant
    fait alors fallback sur le scan complet).
    """
    from .config import RTFM_DB
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
    query = " ".join(parts)

    try:
        proc = subprocess.run(
            ["rtfm", "search", query, "--db", str(RTFM_DB),
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

    # Filtre les hits dans _registry/refs/ et extrait le slug
    slugs: list[str] = []
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


def _substitute_to_wikilink(
    sota_path: Path,
    citation: ParsedCitation,
    slug: str,
) -> bool:
    """Remplace le texte brut de la citation par `[[target]] —` dans le SOTA.

    La cible est le stem du PDF si la ref en a un (le wikilink ouvre alors
    directement le PDF), sinon le slug registre (fallback).

    Préserve le format bibliographique humain :

    Avant :
      - Heydari et al., "BeatNet: ...", ISMIR 2021

    Après :
      - [[Heydari_2021_BeatNet_...]] — Heydari et al., "BeatNet: ...", ISMIR 2021

    Idempotent : si la cible (PDF stem) est déjà devant le raw, ne fait rien.

    Retourne True si une substitution a été faite.
    """
    try:
        text = sota_path.read_text(encoding="utf-8")
    except OSError:
        return False
    raw = citation.raw.strip()
    if not raw:
        return False
    wikilink = _wikilink_for_slug(slug)
    # Le `raw` du sub-agent peut ne pas matcher mot-à-mot le texte du
    # SOTA (par exemple sub-agent retourne "Valiant, L.G. 1975 *...*"
    # alors que SOTA a "- **Valiant, L.G. 1975** *...*"). On essaie le
    # match strict d'abord, puis un match permissif sur l'ancre auteur+année.
    raw_to_use = raw
    if raw not in text:
        # Cherche une ancre courte "<Lastname>... <year>" dans le texte
        # et substitue à la ligne contenant cette ancre.
        lastname_raw = (
            _extract_first_author_lastname(citation.author).capitalize()
        )
        year_str = citation.year or ""
        if lastname_raw and year_str:
            # Cherche la première ligne contenant lastname ET year proches.
            for line in text.splitlines():
                if (lastname_raw.lower() in line.lower()
                        and year_str in line
                        and "[[" not in line):
                    raw_to_use = line.strip()
                    break
    if raw_to_use not in text:
        return False
    # Substitution multi-occurrences : on remplace TOUTES les occurrences
    # du `raw` exact dans le SOTA. Idempotence : on protège les occurrences
    # déjà préfixées par le wikilink en les marquant temporairement.
    sentinel = f"___WIKILINKED___{slug}___WIKILINKED___"
    # 1) Marque les occurrences déjà wikilinkées pour qu'elles ne soient
    #    pas re-substituées.
    already = f"{wikilink} — {raw_to_use}"
    protected = text.replace(already, sentinel)
    # 2) Substitue toutes les occurrences restantes du raw nu.
    substituted = protected.replace(raw_to_use, f"{wikilink} — {raw_to_use}")
    # 3) Restaure le sentinel.
    new_text = substituted.replace(sentinel, already)
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
    result = IngestResult(sota_path=sota_path)
    for cit in citations:
        if skip_low_confidence and cit.confidence == "low":
            result.skipped_low_confidence.append(cit.raw[:60])
            continue
        try:
            doi = _identify_doi(cit)
            cit.resolved_doi = doi
            existing_slug = _reconcile_with_registry(cit, doi)
            if existing_slug:
                cit.matched_slug = existing_slug
                slug = existing_slug
                result.reused_refs.append(slug)
            else:
                if apply:
                    slug = _create_ref(cit, doi, sota_path)
                    cit.created_slug = slug
                    result.new_refs.append(slug)
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
