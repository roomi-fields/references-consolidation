"""Cascade 9 niveaux pour `uid_resolved → pdf_acquired`.

Chaque `try_<source>` retourne un tuple (verdict, info_dict) :
  - "success"      : PDF téléchargé, page 1 validée, prêt à être moved to dest.
  - "page1_failed" : PDF téléchargé mais validation page 1 KO (homonymie probable).
                     Le tmp est quarantained, on continue la cascade.
  - "no_source"    : la source n'est pas applicable (ex: pas de DOI pour Crossref).
  - "failed"       : applicable mais erreur réseau / 404 / pas de PDF dispo.

La fonction `run_cascade(ref)` orchestre les 9 étapes et retourne le premier
succès ou un verdict "cascade_exhausted".
"""
from __future__ import annotations
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml  # noqa: F401

from .config import SOURCES, QUARANTINE, PLUGIN_LIB  # noqa: F401
from .registry import Ref
from .breakers import BreakerRegistry

# Imports différés des helpers — ils ont des side effects (logs, sessions).
# On les importe lazy depuis le plugin lib via sys.path déjà inséré par config.

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
EMAIL = "claude@liance.art"


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires partagés
# ─────────────────────────────────────────────────────────────────────────────

def _doi(ref: Ref) -> str | None:
    uid = ref.frontmatter.get("uid") or ""
    return uid[4:].strip() if uid.startswith("doi:") else None


def _arxiv_id(ref: Ref) -> str | None:
    uid = ref.frontmatter.get("uid") or ""
    return uid[6:].strip() if uid.startswith("arxiv:") else None


def _http_get(url: str, timeout: int = 30, headers: dict | None = None) -> bytes | None:
    h = {"User-Agent": UA, "Accept": "application/pdf,*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _is_valid_pdf(data: bytes) -> bool:
    return bool(data) and data[:5] == b"%PDF-" and len(data) > 3000


def _make_dest_path(ref: Ref) -> Path:
    """Chemin de destination dans `10_SOURCES/<domain>/Sources/`.

    Le domaine est inféré depuis le frontmatter (champ `domain`) ou par défaut
    `11_Biblio_MIR` (le pré-SOTA Bernard Bel y va).
    """
    domain = ref.frontmatter.get("domain")
    if not domain:
        # Heuristique : utiliser le suffixe du slug s'il contient `biblio_*`
        slug = ref.slug
        if "biblio_informatique" in slug:
            domain = "11_Biblio_Informatique"
        elif "biblio_mir" in slug:
            domain = "11_Biblio_MIR"
        elif "biblio_ethno" in slug:
            domain = "12_Biblio_Ethno"
        elif "biblio_maths" in slug:
            domain = "13_Biblio_Maths"
        else:
            domain = "11_Biblio_MIR"
    author = (ref.frontmatter.get("author") or "Unknown").split()[0].capitalize()
    year = ref.frontmatter.get("year") or "nd"
    title = (ref.frontmatter.get("title") or "untitled")[:50]
    # sanitize
    import re
    title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50] or "untitled"
    fname = f"{author}_{year}_{title}.pdf"
    return SOURCES / domain / "Sources" / fname


def _quarantine(tmp: Path, ref: Ref, reason: str) -> Path:
    """Déplace un PDF rejeté en quarantaine. Retourne le path final."""
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    import hashlib
    sha8 = hashlib.sha256(tmp.read_bytes()).hexdigest()[:8]
    qpath = QUARANTINE / f"{ref.slug}_{sha8}.pdf"
    shutil.move(str(tmp), str(qpath))
    return qpath


def _validate_page1(pdf_path: Path, ref: Ref) -> tuple[bool, str]:
    """Wrapper sur validate_pdf_against_ref du plugin."""
    import validate_pdf_content as v
    return v.validate_pdf_against_ref(
        pdf_path,
        expected_author=ref.frontmatter.get("author") or "",
        expected_year=str(ref.frontmatter.get("year") or ""),
        expected_title=ref.frontmatter.get("title") or "",
    )


def _save_and_validate(data: bytes, ref: Ref) -> tuple[str, dict]:
    """Écrit data dans un tmp, valide page 1, retourne (verdict, info).

    Si OK : déplace vers dest_path, info contient pdf_path et sha256.
    Si KO : quarantaine, info contient quarantine_path et reason.
    """
    if not _is_valid_pdf(data):
        return "failed", {"reason": "not_a_pdf"}
    tmp = Path(tempfile.mkstemp(suffix=".pdf", prefix="cascade_")[1])
    tmp.write_bytes(data)
    is_ok, reason = _validate_page1(tmp, ref)
    if not is_ok:
        qpath = _quarantine(tmp, ref, reason)
        return "page1_failed", {"reason": reason, "quarantine": str(qpath)}
    dest = _make_dest_path(ref)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(dest))
    import hashlib
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    return "success", {
        "pdf_path": str(dest.relative_to(SOURCES)),
        "pdf_sha256": sha,
        "size_kb": dest.stat().st_size // 1024,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — Crossref OA URL (REST direct)
# ─────────────────────────────────────────────────────────────────────────────

def try_crossref_oa(ref: Ref) -> tuple[str, dict]:
    doi = _doi(ref)
    if not doi:
        return "no_source", {"reason": "no_doi"}
    api = f"https://api.crossref.org/works/{doi}"
    try:
        with urllib.request.urlopen(api, timeout=15) as r:
            import json
            data = json.loads(r.read())
    except Exception as e:
        return "failed", {"reason": f"crossref_api_error:{type(e).__name__}"}
    links = (data.get("message") or {}).get("link") or []
    pdf_urls = [L.get("URL") for L in links
                if L.get("content-type") == "application/pdf" and L.get("URL")]
    if not pdf_urls:
        return "no_source", {"reason": "no_oa_url_in_crossref"}
    for url in pdf_urls[:3]:
        pdf = _http_get(url, timeout=60)
        if pdf:
            return _save_and_validate(pdf, ref)
    return "failed", {"reason": "all_oa_urls_failed"}


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — arXiv direct
# ─────────────────────────────────────────────────────────────────────────────

def try_arxiv(ref: Ref) -> tuple[str, dict]:
    aid = _arxiv_id(ref)
    if not aid:
        return "no_source", {"reason": "no_arxiv_id"}
    url = f"https://arxiv.org/pdf/{aid}.pdf"
    pdf = _http_get(url, timeout=60)
    if not pdf:
        return "failed", {"reason": "arxiv_dl_failed"}
    return _save_and_validate(pdf, ref)


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — OpenAlex oa_url
# ─────────────────────────────────────────────────────────────────────────────

def try_openalex(ref: Ref) -> tuple[str, dict]:
    doi = _doi(ref)
    title = ref.frontmatter.get("title") or ""
    if not doi and not title:
        return "no_source", {"reason": "no_doi_no_title"}
    if doi:
        api = f"https://api.openalex.org/works/doi:{doi}"
    else:
        from urllib.parse import quote
        api = f"https://api.openalex.org/works?search={quote(title[:100])}&per-page=1"
    try:
        with urllib.request.urlopen(api, timeout=15) as r:
            import json
            data = json.loads(r.read())
    except Exception as e:
        return "failed", {"reason": f"openalex_api:{type(e).__name__}"}
    work = data.get("results", [data])[0] if "results" in data else data
    oa = (work.get("open_access") or {}).get("oa_url") or work.get("oa_url")
    if not oa:
        return "no_source", {"reason": "no_oa_url"}
    pdf = _http_get(oa, timeout=60)
    if not pdf:
        return "failed", {"reason": "openalex_oa_dl_failed"}
    return _save_and_validate(pdf, ref)


# ─────────────────────────────────────────────────────────────────────────────
# Source 4 — Unpaywall (via helper)
# ─────────────────────────────────────────────────────────────────────────────

def try_unpaywall(ref: Ref) -> tuple[str, dict]:
    doi = _doi(ref)
    if not doi:
        return "no_source", {"reason": "no_doi"}
    try:
        from oa_finder import get_unpaywall_pdf_urls
        urls = get_unpaywall_pdf_urls(doi)
    except Exception as e:
        return "failed", {"reason": f"unpaywall_helper:{type(e).__name__}"}
    if not urls:
        return "no_source", {"reason": "no_unpaywall_url"}
    for url in urls[:3]:
        pdf = _http_get(url, timeout=60)
        if pdf:
            return _save_and_validate(pdf, ref)
    return "failed", {"reason": "all_unpaywall_failed"}


# ─────────────────────────────────────────────────────────────────────────────
# Source 5 — HAL / CORE / Zenodo (via helper CORE + REST HAL)
# ─────────────────────────────────────────────────────────────────────────────

def try_hal(ref: Ref) -> tuple[str, dict]:
    title = ref.frontmatter.get("title") or ""
    author = ref.frontmatter.get("author") or ""
    if not title:
        return "no_source", {"reason": "no_title_for_hal_query"}
    from urllib.parse import quote
    q = quote(f'title_t:"{title[:100]}"')
    api = f"https://api.archives-ouvertes.fr/search/?q={q}&fl=halId_s,fileMain_s&rows=3&wt=json"
    try:
        with urllib.request.urlopen(api, timeout=15) as r:
            import json
            docs = (json.loads(r.read()).get("response") or {}).get("docs") or []
    except Exception as e:
        return "failed", {"reason": f"hal_api:{type(e).__name__}"}
    for d in docs:
        url = d.get("fileMain_s")
        if not url:
            continue
        pdf = _http_get(url, timeout=60)
        if pdf:
            r = _save_and_validate(pdf, ref)
            if r[0] in ("success", "page1_failed"):
                r[1]["hal_id"] = d.get("halId_s")
                return r
    return "no_source", {"reason": "hal_no_match_or_no_file"}


def try_core(ref: Ref) -> tuple[str, dict]:
    title = ref.frontmatter.get("title") or ""
    author = ref.frontmatter.get("author") or ""
    if not title:
        return "no_source", {"reason": "no_title"}
    try:
        from oa_finder import get_core_pdf_urls
        urls = get_core_pdf_urls(title, author)
    except Exception as e:
        return "failed", {"reason": f"core_helper:{type(e).__name__}"}
    for url in urls[:2]:
        pdf = _http_get(url, timeout=60)
        if pdf:
            return _save_and_validate(pdf, ref)
    return "no_source", {"reason": "no_core_match"}


# ─────────────────────────────────────────────────────────────────────────────
# Source 5c — Internet Archive (via helper archive_org_helper)
# ─────────────────────────────────────────────────────────────────────────────

def try_archive_org(ref: Ref) -> tuple[str, dict]:
    """Cascade Internet Archive : search (3 stratégies) → metadata → DL.

    Couvre les refs sans DOI publiées dans des journaux indiens / proceedings
    spécialisés non indexés Crossref (typiquement la production Bernard Bel
    et Arnold dans Sangit Natak Akademi).

    Stratégies de query enchaînées (vu que IA full-text indexing est strict) :
      Q1. titre complet + auteur + année
      Q2. mots distinctifs du titre (≥ 5 lettres) + auteur
      Q3. auteur + année (last resort — `best_search_match` filtre
          ensuite par title_similarity ≥ 0.5 + auteur match + score ≥ 4)

    Anti-homonymie : déléguée à `archive_org_helper.best_search_match` qui
    refuse tout match sans titre fourni et filtre score ≥ 4.

    G5 (pas de `no_source` muet) : chaque échec a un motif descriptif.
    """
    fm = ref.frontmatter
    author = (fm.get("author") or "").strip()
    title = (fm.get("title") or "").strip()
    year = str(fm.get("year") or "").strip()
    if not title:
        return "no_source", {"reason": "no_title_for_archive_org_search"}

    try:
        from archive_org_helper import (
            search_items, best_search_match, get_metadata,
            is_borrow_only, find_pdf_file,
        )
    except Exception as e:
        return "failed", {"reason": f"ia_helper_import:{type(e).__name__}:{str(e)[:60]}"}

    # Construire les 3 queries
    distinctive = " ".join(w for w in title.replace("-", " ").split()
                            if len(w) >= 5 and w.isalpha())[:120]
    queries = [
        f"{title} {author} {year}".strip(),
        f"{distinctive} {author}".strip() if distinctive else None,
        f"{author} {year}".strip() if author and year else None,
    ]
    queries = [q for q in queries if q]

    # Collecter les résultats des 3 queries, déduper par identifier
    all_results = []
    seen = set()
    tried = []
    for q in queries:
        results = search_items(q, limit=5, mediatype="texts")
        tried.append({"q": q, "hits": len(results)})
        for r in results:
            ident = r.get("identifier")
            if ident and ident not in seen:
                seen.add(ident)
                all_results.append(r)
        if all_results:
            # On a au moins quelques candidats — best_search_match va filtrer.
            # Pas besoin d'épuiser les 3 queries si on a déjà du matériel.
            break

    if not all_results:
        return "no_source", {"reason": "ia_no_results_3_queries", "queries_tried": tried}

    best = best_search_match(all_results, author, title, year)
    if not best:
        return "no_source", {"reason": "ia_no_match_above_threshold",
                              "candidates": len(all_results),
                              "queries_tried": tried}

    identifier = best.get("identifier", "")
    meta = get_metadata(identifier)
    if is_borrow_only(meta):
        return "failed", {"reason": "ia_borrow_only_needs_login",
                          "detail": f"archive.org/details/{identifier}"}
    pdf_name = find_pdf_file(meta)
    if not pdf_name:
        return "failed", {"reason": "ia_item_found_but_no_pdf",
                          "detail": f"archive.org/details/{identifier}"}

    # DL direct : on quote le nom de fichier (peut contenir des '%' littéraux
    # comme dans dli.ministry.16926/JSNA%2867%2929-41.pdf — les '%' doivent
    # devenir '%25' pour l'URL. Le helper download_public_pdf ne le fait pas.)
    from urllib.parse import quote
    dl_url = f"https://archive.org/download/{identifier}/{quote(pdf_name, safe='')}"
    pdf_bytes = _http_get(dl_url, timeout=180,
                          headers={"Referer": f"https://archive.org/details/{identifier}"})
    if not pdf_bytes or not _is_valid_pdf(pdf_bytes):
        return "failed", {"reason": "ia_dl_returned_invalid_pdf",
                          "detail": f"archive.org/details/{identifier}",
                          "url": dl_url}
    data = pdf_bytes
    verdict, save_info = _save_and_validate(data, ref)
    if verdict in ("success", "page1_failed"):
        save_info["via"] = "archive_org"
        save_info["ia_identifier"] = identifier
        save_info["ia_pdf"] = pdf_name
    return verdict, save_info


# ─────────────────────────────────────────────────────────────────────────────
# Source 6 — Sci-Hub multi-mirror (via helper s2_resolver)
# ─────────────────────────────────────────────────────────────────────────────

def try_scihub(ref: Ref) -> tuple[str, dict]:
    doi = _doi(ref)
    if not doi:
        return "no_source", {"reason": "no_doi"}
    tmp = Path(tempfile.mkstemp(suffix=".pdf", prefix="scihub_")[1])
    try:
        from s2_resolver import try_scihub as helper_scihub
        ok = helper_scihub(doi, tmp)
        if not ok or not tmp.exists() or tmp.stat().st_size < 3000:
            return "failed", {"reason": "scihub_no_pdf"}
        data = tmp.read_bytes()
    except Exception as e:
        return "failed", {"reason": f"scihub_helper:{type(e).__name__}"}
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return _save_and_validate(data, ref)


# ─────────────────────────────────────────────────────────────────────────────
# Source 7 — Anna's Archive
# ─────────────────────────────────────────────────────────────────────────────

def _aa_md5_from_doi(doi: str) -> tuple[str | None, str]:
    """AA `/scidb/<doi>` → MD5. Retourne (md5_or_None, info_string)."""
    from urllib.parse import quote
    import re
    scidb_url = f"https://annas-archive.gl/scidb/{quote(doi, safe=':/')}"
    html = _http_get(scidb_url, timeout=30)
    if not html:
        return None, "scidb_unreachable"
    m = re.search(rb"/md5/([0-9a-f]{32})", html or b"")
    if not m:
        return None, "scidb_no_md5"
    return m.group(1).decode(), "scidb_match"


def _aa_md5_from_title(title: str, author: str) -> tuple[str | None, str]:
    """F2 — title-search AA, extraction MD5 directe depuis HTML.

    Le helper `lib/annas_archive.AnnasArchive.search_books` retourne actuellement
    des BookData aux champs vides (parser BeautifulSoup cassé ou HTML AA modifié
    — observé 2026-05-24). Pour ne pas dépendre de ce parser, on fetch
    directement la page de search et on extrait les `<a href="/md5/...">`
    associés à leur contexte titre.

    Anti-homonymie : on filtre les hits dont le bloc HTML contient au moins
    un mot distinctif (≥ 5 lettres) du titre demandé. La sécurité finale
    reste la page 1 validation post-DL (`_save_and_validate`).

    Retourne (md5_or_None, info_string).
    """
    if not title:
        return None, "no_title_for_aa_search"
    from urllib.parse import quote
    import re
    query = f"{title} {author}".strip() if author else title
    # Forcer recherche dans les contenus "main" (livres+articles), pas "journals"
    search_url = f"https://annas-archive.gl/search?q={quote(query)}&ext=pdf"
    html_bytes = _http_get(search_url, timeout=30)
    if not html_bytes:
        return None, "aa_search_unreachable"
    html = html_bytes.decode("utf-8", errors="replace")

    # Extraction structurée : `re.split` avec capture donne
    #   [head, md5_1, chunk_1, md5_2, chunk_2, ...].
    # On strip les tags HTML de chaque chunk pour avoir du texte plain
    # (titre + auteur visible).
    parts = re.split(r'/md5/([0-9a-f]{32})', html)
    if len(parts) < 3:
        return None, "aa_no_md5_in_search_html"

    distinctive = [w.lower() for w in title.replace("-", " ").split()
                   if len(w) >= 5 and w.isalpha()]
    author_norm = (author or "").lower().split()[0] if author else None

    # Itérer sur les paires (md5, chunk)
    hits_examined = 0
    for i in range(1, len(parts) - 1, 2):
        md5 = parts[i]
        chunk = parts[i + 1][:2500]  # bornes raisonnables
        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = re.sub(r'\s+', ' ', text).lower()
        hits_examined += 1
        if distinctive:
            matches = [w for w in distinctive if w in text]
            if not matches:
                continue
            if author_norm and author_norm not in text:
                # Mot distinctif présent mais pas l'auteur — homonymie probable
                continue
            return md5, f"aa_title_search_match:kw={matches[0]!r}"
        else:
            # Pas de mot distinctif — accepter le 1er hit, page 1 valide en aval
            return md5, "aa_first_hit_no_distinctive_words"
    return None, f"aa_no_keyword+author_match_in_{hits_examined}_hits"


def _md5_download_cascade(md5: str, ref: Ref, via_label: str) -> tuple[str, dict]:
    """Cascade DL libgen.li → library.lol pour un MD5 donné. Retourne tuple."""
    import re
    # libgen.li
    libgen_landing = f"https://libgen.li/ads.php?md5={md5}"
    landing = _http_get(libgen_landing, timeout=30)
    if landing:
        m2 = re.search(rb'(get\.php\?[^"\']+)', landing)
        if m2:
            dl_url = "https://libgen.li/" + m2.group(1).decode()
            pdf = _http_get(dl_url, timeout=180, headers={"Referer": libgen_landing})
            if pdf:
                r = _save_and_validate(pdf, ref)
                if r[0] in ("success", "page1_failed"):
                    r[1]["md5"] = md5
                    r[1]["via"] = f"{via_label}_libgen"
                    return r
    # library.lol fallback
    lib_url = f"https://library.lol/main/{md5.upper()}"
    pdf = _http_get(lib_url, timeout=60)
    if pdf:
        r = _save_and_validate(pdf, ref)
        if r[0] in ("success", "page1_failed"):
            r[1]["md5"] = md5
            r[1]["via"] = f"{via_label}_library_lol"
            return r
    return "failed", {"reason": "aa_md5_found_but_no_dl", "md5": md5,
                       "via_attempted": [f"{via_label}_libgen", f"{via_label}_library_lol"]}


def try_annas_archive(ref: Ref) -> tuple[str, dict]:
    """AA cascade (F2 — title-fallback en plus de scidb DOI).

    Ordre :
      1. Si DOI : AA `/scidb/<doi>` → MD5
      2. Sinon (F2) : AA `search_books(title + author)` → MD5
      3. Cascade DL : libgen.li → library.lol
      4. Anti-homonymie : `_save_and_validate` filtre via page 1 validation.

    Playwright `try_annas_slow` (Turnstile sur AA `/slow_download`) **non
    branché en V1** : Playwright sur WSL2 instable, ~5-10 refs concernées
    historiquement, gérables en queue manuelle.
    """
    doi = _doi(ref)
    md5 = None
    via_label = None

    if doi:
        md5, info = _aa_md5_from_doi(doi)
        if md5:
            via_label = "aa_scidb"
        elif info == "scidb_unreachable":
            return "failed", {"reason": info}
        # Si scidb_no_md5 et qu'on a un titre, on retombe sur title-search.

    if not md5:
        author = (ref.frontmatter.get("author") or "").strip()
        title = (ref.frontmatter.get("title") or "").strip()
        md5, info = _aa_md5_from_title(title, author)
        if md5:
            via_label = "aa_title"
        else:
            return "no_source", {"reason": info}

    return _md5_download_cascade(md5, ref, via_label)


# ─────────────────────────────────────────────────────────────────────────────
# Source 8 — WebSearch (skipped en CLI worker) — flag pour Claude Code
# ─────────────────────────────────────────────────────────────────────────────

def try_websearch(ref: Ref) -> tuple[str, dict]:
    """F4 — append la query au manifest `_websearch_queue.md`.

    Décision V1 : abandon des scrapers DDG/Scholarly (fragiles, exactement le
    piège L18). À la place, on **délègue** à une session Claude Code
    interactive (qui a accès au tool WebSearch MCP) : le worker ajoute la
    query à un manifest markdown, et l'utilisateur ou un agent les consomme
    en batch.

    Retourne toujours `no_source` car aucun PDF acquis ici — l'étape suivante
    (manual queue) sera prise.
    """
    from .config import REGISTRY
    from datetime import datetime, timezone

    fm = ref.frontmatter
    title = (fm.get("title") or "").strip()
    author = (fm.get("author") or "").strip()
    year = fm.get("year")

    if not title and not author:
        return "no_source", {"reason": "no_title_author_for_websearch_query"}

    query_parts = []
    if title:
        query_parts.append(f'"{title}"')
    if author:
        query_parts.append(f'"{author}"')
    query_parts.append("filetype:pdf")
    query = " ".join(query_parts)

    queue_file = REGISTRY / "_websearch_queue.md"
    if not queue_file.exists():
        queue_file.write_text(
            "# WebSearch queue — refs sans hit dans la cascade automatique\n\n"
            "Ces refs ont épuisé Crossref/arXiv/OpenAlex/Unpaywall/HAL/CORE/"
            "archive.org/Sci-Hub/AA sans trouver de PDF. Elles attendent une "
            "recherche WebSearch manuelle via Claude Code interactif.\n\n"
            "Format : `| slug | query | created_at | status |`\n\n"
            "| slug | query | created_at | status |\n"
            "|------|-------|-----------|--------|\n",
            encoding="utf-8",
        )

    line = (
        f"| `{ref.slug}` "
        f"| `{query[:200]}` "
        f"| {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')} "
        f"| pending |\n"
    )
    # Append-only, vérification que la ref n'est pas déjà dans la queue
    existing = queue_file.read_text(encoding="utf-8")
    if f"`{ref.slug}`" in existing:
        return "no_source", {"reason": "websearch_already_queued",
                              "manifest": str(queue_file.name)}
    with queue_file.open("a", encoding="utf-8") as f:
        f.write(line)
    return "no_source", {"reason": "queued_for_manual_websearch",
                          "manifest": str(queue_file.name),
                          "query": query[:160]}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrateur
# ─────────────────────────────────────────────────────────────────────────────

CASCADE: list[tuple[str, Callable[[Ref], tuple[str, dict]]]] = [
    ("crossref_oa", try_crossref_oa),
    ("arxiv", try_arxiv),
    ("openalex_oa", try_openalex),
    ("unpaywall", try_unpaywall),
    ("hal", try_hal),
    ("core", try_core),
    ("archive_org", try_archive_org),  # F3 — étape 5c (2026-05-24)
    ("scihub", try_scihub),
    ("annas_archive", try_annas_archive),
    ("websearch", try_websearch),
]


def already_tried(ref: Ref, source: str) -> bool:
    """Vrai si la source a déjà un attempt loggé avec verdict ≠ no_source."""
    for a in ref.frontmatter.get("acquisition_attempts") or []:
        if a.get("source") == source and a.get("verdict") not in (
            "no_source", None, "skipped", ""
        ):
            return True
    return False


# Registry global de breakers — singleton process-wide, partagé par les
# appels successifs à run_cascade dans la même session worker.
_BREAKERS: BreakerRegistry | None = None


def get_breakers() -> BreakerRegistry:
    """Accès au registre global de breakers (init paresseuse)."""
    global _BREAKERS
    if _BREAKERS is None:
        _BREAKERS = BreakerRegistry(fail_threshold=5, window_s=60.0)
    return _BREAKERS


def reset_breakers() -> None:
    """Réinitialise le registre global. Utile pour les tests."""
    global _BREAKERS
    _BREAKERS = None


def run_cascade(ref: Ref, breakers: BreakerRegistry | None = None
                ) -> tuple[str, list[dict]]:
    """Lance la cascade. Retourne ('success'|'cascade_exhausted', attempts_log).

    Les attempts sont retournés pour que le caller les append au YAML.

    Si `breakers` n'est pas fourni, utilise le registre global de la session.
    Une source dont le breaker est ouvert est skippée avec verdict
    `skipped_breaker_open` (et reason explicite).
    """
    if breakers is None:
        breakers = get_breakers()

    attempts: list[dict] = []
    for source, fn in CASCADE:
        if already_tried(ref, source):
            attempts.append({"source": source, "verdict": "skipped_already_tried"})
            continue
        if breakers[source].is_open():
            attempts.append({
                "source": source,
                "verdict": "skipped_breaker_open",
                "reason": "5_consecutive_fails_in_60s",
            })
            continue
        verdict, info = fn(ref)
        entry = {"source": source, "verdict": verdict, **info}
        attempts.append(entry)
        # On considère la source OK si elle a su livrer (success) OU livrer
        # un PDF qui a échoué la validation page 1 (page1_failed = source a
        # délivré, la ref est juste un mauvais hit — pas la faute de la source).
        # failed / no_source comptent comme un échec côté breaker.
        breakers[source].record(success=verdict in ("success", "page1_failed"))
        if verdict == "success":
            return "success", attempts
        # En cas de page1_failed, on quarantine et on continue (homonymie probable).
        # En cas de failed/no_source, on continue.
        time.sleep(1)  # courtoisie réseau
    return "cascade_exhausted", attempts
