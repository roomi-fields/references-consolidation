"""Anna's Archive cascade — source d'acquisition opt-in.

Extrait de pipeline/cascade.py (_aa_md5_from_doi, _aa_md5_from_title,
_md5_download_cascade, try_annas_archive, lignes 420-560).

Activation : variable d'environnement RESEARCH_ENABLE_SHADOW_LIBS=1.

L'utilisation d'Anna's Archive peut violer le droit d'auteur dans
votre juridiction. Cf. DISCLAIMER.md à la racine du plugin.

Note : le helper `lib/shadow/annas_archive_helper.AnnasArchive.search_books`
(copié depuis source-collector) avait son parser BeautifulSoup cassé au
2026-05-24. On parse directement le HTML de la page de search ici.
L'anti-homonymie est garantie par la page 1 validation côté
`_save_and_validate`.
"""
from __future__ import annotations
import re
from urllib.parse import quote

from pipeline.registry import Ref


def _aa_md5_from_doi(doi: str) -> tuple[str | None, str]:
    """AA `/scidb/<doi>` → MD5. Retourne (md5_or_None, info_string)."""
    # Lazy import pour éviter cycle au module-load
    from pipeline.cascade import _http_get

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

    Le helper `lib/shadow/annas_archive_helper.AnnasArchive.search_books`
    retournait des BookData aux champs vides (parser BeautifulSoup cassé,
    observé 2026-05-24). Pour ne pas dépendre de ce parser, on fetch
    directement la page de search et on extrait les `<a href="/md5/...">`
    associés à leur contexte titre.

    Anti-homonymie : on filtre les hits dont le bloc HTML contient au moins
    un mot distinctif (≥ 5 lettres) du titre demandé. La sécurité finale
    reste la page 1 validation post-DL (`_save_and_validate`).

    Retourne (md5_or_None, info_string).
    """
    from pipeline.cascade import _http_get

    if not title:
        return None, "no_title_for_aa_search"
    query = f"{title} {author}".strip() if author else title
    search_url = f"https://annas-archive.gl/search?q={quote(query)}&ext=pdf"
    html_bytes = _http_get(search_url, timeout=30)
    if not html_bytes:
        return None, "aa_search_unreachable"
    html = html_bytes.decode("utf-8", errors="replace")

    parts = re.split(r'/md5/([0-9a-f]{32})', html)
    if len(parts) < 3:
        return None, "aa_no_md5_in_search_html"

    distinctive = [w.lower() for w in title.replace("-", " ").split()
                   if len(w) >= 5 and w.isalpha()]
    author_norm = (author or "").lower().split()[0] if author else None

    hits_examined = 0
    for i in range(1, len(parts) - 1, 2):
        md5 = parts[i]
        chunk = parts[i + 1][:2500]
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = re.sub(r'\s+', ' ', text).lower()
        hits_examined += 1
        if distinctive:
            matches = [w for w in distinctive if w in text]
            if not matches:
                continue
            if author_norm and author_norm not in text:
                continue
            return md5, f"aa_title_search_match:kw={matches[0]!r}"
        else:
            return md5, "aa_first_hit_no_distinctive_words"
    return None, f"aa_no_keyword+author_match_in_{hits_examined}_hits"


def _md5_download_cascade(md5: str, ref: Ref, via_label: str) -> tuple[str, dict]:
    """Cascade DL libgen.li → library.lol pour un MD5 donné."""
    from pipeline.cascade import _http_get, _save_and_validate

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
      2. Sinon (F2) : AA title-search → MD5
      3. Cascade DL : libgen.li → library.lol
      4. Anti-homonymie : `_save_and_validate` filtre via page 1 validation.
    """
    from pipeline.cascade import _doi

    doi = _doi(ref)
    md5 = None
    via_label = None

    if doi:
        md5, info = _aa_md5_from_doi(doi)
        if md5:
            via_label = "aa_scidb"
        elif info == "scidb_unreachable":
            return "failed", {"reason": info}

    if not md5:
        author = (ref.frontmatter.get("author") or "").strip()
        title = (ref.frontmatter.get("title") or "").strip()
        md5, info = _aa_md5_from_title(title, author)
        if md5:
            via_label = "aa_title"
        else:
            return "no_source", {"reason": info}

    return _md5_download_cascade(md5, ref, via_label)
