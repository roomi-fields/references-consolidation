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


def _reconcile_with_registry(
    citation: ParsedCitation,
    doi: Optional[str],
    title_threshold: float = 0.8,
) -> Optional[str]:
    """Cherche dans le registre une ref qui correspond à cette citation.

    Algorithme :
    1. DOI strict : si doi présent, scan registre pour `uid: doi:<X>`
    2. Fuzzy : auteur exact (lastname) + année exacte + Levenshtein
       titre ≥ `title_threshold`

    Retourne le slug d'une ref existante, ou None.
    """
    cite_lastname = _extract_first_author_lastname(citation.author)
    cite_year = re.sub(r"[^0-9]", "", citation.year or "")[:4]
    cite_title_norm = _normalize_title(citation.title)

    for ref in iter_refs():
        fm = ref.frontmatter
        # 1. DOI strict (uid peut être None explicite dans le YAML, pas
        # seulement absent — `or ""` couvre les deux cas)
        ref_uid = fm.get("uid") or ""
        if doi and ref_uid.startswith("doi:"):
            ref_doi = ref_uid[4:].strip().lower()
            if ref_doi == doi.lower():
                return ref.slug
        # 2. Fuzzy : auteur + année + titre
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


def _find_orphan_pdf_for_citation(
    citation: "ParsedCitation",
) -> Optional[Path]:
    """Scanne le dossier Sources pour trouver un PDF qui matche cette citation.

    Match (case-insensitive) :
    - `<lastname>_<year>` au début du nom de fichier
    - PDF non encore associé à une ref du registre

    Utilisé pour les refs "Local" mentionnées dans les SOTAs : leur PDF
    est déjà sur disque (le label "Local" signifie ça), mais pas encore
    lié à une ref registre.

    Retourne le Path absolu du PDF si trouvé, None sinon.
    """
    from .config import SOURCES
    lastname = _extract_first_author_lastname(citation.author)
    year = re.sub(r"[^0-9]", "", citation.year or "")[:4]
    if not lastname or lastname == "unknown" or not year:
        return None

    # PDFs déjà utilisés par d'autres refs
    used = set()
    for ref in iter_refs():
        pp = ref.frontmatter.get("pdf_path")
        if pp:
            used.add(str((SOURCES / pp).resolve()))

    # Patterns possibles (case-insensitive) : <Lastname>_<year>*.pdf
    if not SOURCES.exists():
        return None
    lname_lower = lastname.lower()
    for p in SOURCES.rglob("*.pdf"):
        stem_lower = p.stem.lower()
        if not stem_lower.startswith(f"{lname_lower}_{year}"):
            continue
        if str(p.resolve()) in used:
            continue
        return p
    return None


def _try_validate_page1(pdf_path: Path, citation: "ParsedCitation") -> tuple[bool, str]:
    """Valide la page 1 d'un PDF contre les attributs d'une citation.

    Réutilise validate_pdf_against_ref si dispo, sinon retourne (False, "...")
    pour laisser la ref en candidate.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
        import validate_pdf_content as v
        return v.validate_pdf_against_ref(
            pdf_path,
            expected_author=citation.author or "",
            expected_year=str(citation.year or ""),
            expected_title=citation.title or "",
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
    # Idempotence : si le wikilink complet (target|alias) est déjà juste
    # avant le raw, on ne re-substitue pas.
    if wikilink in text:
        idx = text.find(wikilink)
        if idx >= 0 and raw in text[idx:idx + len(wikilink) + len(raw) + 10]:
            return False
    new_text = text.replace(raw, f"{wikilink} — {raw}", 1)
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
