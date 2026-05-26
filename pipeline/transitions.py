"""Fonctions de transition de la FSM.

Chaque fonction `<from>_to_<to>(ref)` :
  1. lit l'état actuel de la ref (déjà validé par le dispatcher)
  2. exécute le travail technique
  3. mute le frontmatter (state, state_history, acquisition_attempts, ...)
  4. sauve atomiquement via registry.save_ref
  5. retourne un TransitionResult pour le journal
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

from .registry import Ref, save_ref, append_state_history, append_acquisition_attempt
from .config import SOURCES


# ─────────────────────────────────────────────────────────────────────────────
# R8 auto-fix — drift detection sur `pdf_path`
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_pdf_path_inplace(ref: Ref) -> dict | None:
    """Détecte et corrige les anomalies connues sur `pdf_path` (drift R8).

    Anomalie #1 : pdf_path préfixé par `10_SOURCES/` alors qu'il devrait être
    relatif depuis 10_SOURCES (la racine SOURCES). Le résultat est un chemin
    inexistant `.../10_SOURCES/10_SOURCES/...`. Cas observés :
    `lerdahl_2001`, `polak_2014` (peut-être d'autres).

    Si une correction est appliquée :
      - mute `ref.frontmatter["pdf_path"]`
      - append state_history avec note R8 auto-fix
      - sauve atomiquement
      - retourne {"fix": str, "old": str, "new": str}
    Sinon retourne None.
    """
    pdf_rel = ref.frontmatter.get("pdf_path") or ""
    if not pdf_rel:
        return None

    # Anomalie #1 : préfixe `10_SOURCES/` à retirer
    if pdf_rel.startswith("10_SOURCES/"):
        candidate = pdf_rel[len("10_SOURCES/"):]
        candidate_abs = SOURCES / candidate
        bad_abs = SOURCES / pdf_rel
        if candidate_abs.exists() and not bad_abs.exists():
            ref.frontmatter["pdf_path"] = candidate
            append_state_history(
                ref, ref.state, by="worker_b_r8_autofix",
                meta={"r8_fix": "stripped_10_sources_prefix",
                      "old_pdf_path": pdf_rel,
                      "new_pdf_path": candidate}
            )
            save_ref(ref)
            return {"fix": "stripped_10_sources_prefix",
                    "old": pdf_rel, "new": candidate}

    return None


@dataclass
class TransitionResult:
    succeeded: bool
    from_state: str
    to_state: str | None
    via: str
    meta: dict | None = None
    blocked_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# P2 — candidate → uid_resolved (F1 — title-fallback strict + bibkey fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _crossref_title_search(title: str, author: str, year, n: int = 3) -> list[dict]:
    """Query Crossref par titre + auteur, retourne les items bruts."""
    import urllib.request
    import urllib.parse
    import json
    try:
        params = urllib.parse.urlencode({
            "query.title": title[:200],
            "query.author": author[:80],
            "rows": str(n),
        })
        req = urllib.request.Request(
            f"https://api.crossref.org/works?{params}",
            headers={"User-Agent": "musicology-pipeline/0.1 (mailto:claude@liance.art)"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return (data.get("message") or {}).get("items") or []
    except Exception:
        return []


def _pick_crossref_strict(items: list, ref_title: str, ref_author: str, ref_year) -> tuple:
    """Filtre Crossref strict pour F1 (anti-homonymie P9α v1).

    Filtres durs combinés :
      - title_similarity ≥ 0.7 (vs ≥ 0.6 en V0)
      - author_match (premier nom d'auteur normalisé apparaît dans authors S2-like)
      - year_match ±1 an de tolérance (vs strict en V0)

    Retourne (best_item, candidates_rejected_log) où best_item est None si
    aucun ne passe, et candidates_rejected_log liste les candidats avec leur
    similarité et la raison du rejet (G5 — pas de no_source muet).
    """
    from s2_resolver import title_similarity, author_match
    best = None
    best_sim = 0
    rejected = []
    for it in items:
        cand_title = (it.get("title") or [""])[0]
        sim = title_similarity(ref_title, cand_title)
        # Author check : Crossref retourne authors comme list de dicts avec 'family'
        cr_authors = it.get("author") or []
        authors_normalized = [
            {"name": f"{a.get('given','')} {a.get('family','')}".strip()}
            for a in cr_authors if a.get("family")
        ]
        a_ok = author_match(ref_author, authors_normalized) if authors_normalized else False
        cand_year = ((it.get("published-print") or {}).get("date-parts") or [[0]])[0][0] \
            or ((it.get("issued") or {}).get("date-parts") or [[0]])[0][0]
        try:
            y_diff = abs(int(cand_year) - int(ref_year)) if (cand_year and ref_year) else 99
        except (ValueError, TypeError):
            y_diff = 99
        y_ok = (not ref_year) or (y_diff <= 1)
        # Décision
        if sim >= 0.7 and a_ok and y_ok and it.get("DOI"):
            if sim > best_sim:
                best_sim = sim
                best = (it, sim, cand_year)
        else:
            reason = []
            if sim < 0.7:
                reason.append(f"title_sim={round(sim, 3)}<0.7")
            if not a_ok:
                reason.append(f"author_mismatch:{ref_author!r}!={[(a.get('family') or '') for a in cr_authors]}")
            if not y_ok:
                reason.append(f"year_diff={y_diff}>1")
            rejected.append({
                "doi": it.get("DOI"),
                "title": cand_title[:80],
                "sim": round(sim, 3),
                "year": cand_year,
                "reason": " ; ".join(reason),
            })
    return best, rejected


def _make_bibkey_fallback(author: str, year, title: str) -> str:
    """Construit un uid `bibkey:` provisoire pour ref sans DOI/ISBN/arXiv.

    Format : `bibkey:authoryearshortword` lowercase, alpha-only.
    Ex: `bibkey:arnold1982shruti`.
    """
    import re
    a = re.sub(r"[^a-z]", "", (author or "unknown").lower().split()[0])[:15]
    y = str(year or "nd")
    t = re.sub(r"[^a-z\s]", "", (title or "").lower())
    word = next((w for w in t.split() if len(w) >= 5), "ref")[:15]
    return f"bibkey:{a}{y}{word}"


def candidate_to_uid_resolved(ref: Ref) -> TransitionResult:
    """Résout un UID via Crossref strict (F1) puis S2, sinon bibkey fallback.

    F1 (2026-05-24) — corrections après mensonge "livré" :
      - Seuil renforcé title_similarity ≥ 0.7 (vs 0.6 en V0)
      - Filtre auteur strict via `author_match` du helper plugin
      - Tolérance année ±1 an (vs strict en V0)
      - Log explicite des candidats rejetés dans `acquisition_attempts[]` (G5)
      - **bibkey fallback** : si aucun match, on attribue `bibkey:auteurAnnéeMot`
        au lieu de `blocked_by`. Permet à la cascade d'utiliser F3 archive.org
        et F2 AA title-search, qui sont précisément faits pour les refs sans
        DOI (Bel, Arnold, livres indiens).
    """
    fm = ref.frontmatter
    author = (fm.get("author") or "").strip()
    title = (fm.get("title") or "").strip()
    year = fm.get("year")

    if not author or not title:
        ref.frontmatter["blocked_by"] = "missing_author_or_title_for_uid_resolution"
        save_ref(ref)
        return TransitionResult(False, "candidate", None, "no_query_data",
                                blocked_reason="missing_author_or_title")

    # Stratégie 1 : Crossref title-search avec filtre strict (F1)
    items = _crossref_title_search(title, author, year, n=3)
    if items:
        best, rejected = _pick_crossref_strict(items, title, author, year)
        # Log les candidats rejetés (G5 visibilité)
        for r in rejected:
            append_acquisition_attempt(
                ref, "crossref_title_search", "rejected_below_threshold",
                info={"candidate": r["doi"], "title_sim": r["sim"],
                      "reason": r["reason"]}
            )
        if best:
            it, sim, cand_year = best
            uid = "doi:" + it["DOI"]
            ref.frontmatter["uid"] = uid
            ref.frontmatter["uid_source"] = "crossref.title_match_strict"
            # Enrichir si Crossref donne mieux
            cand_title = (it.get("title") or [""])[0]
            if cand_title and len(cand_title) > len(title):
                ref.frontmatter["title"] = cand_title
            venue = it.get("container-title") or [""]
            if venue and venue[0] and not fm.get("venue"):
                ref.frontmatter["venue"] = venue[0]
            append_state_history(ref, "uid_resolved", by="worker_b_f1",
                                 meta={"uid_source": "crossref.title_match_strict",
                                       "title_match": round(sim, 3),
                                       "year_crossref": cand_year})
            save_ref(ref)
            return TransitionResult(True, "candidate", "uid_resolved",
                                    "crossref_title_match_strict",
                                    meta={"uid": uid, "title_sim": sim})

    # Stratégie 2 : S2 fallback (même seuils stricts)
    try:
        from s2_resolver import s2_search, pick_best_match, title_similarity, author_match
        results = s2_search(author, title, str(year) if year else None, limit=5)
    except Exception:
        results = []

    if results:
        best, score = pick_best_match(results, author, title, str(year) if year else None)
        sim = title_similarity(title, best.get("title", "")) if best else 0
        a_ok = author_match(author, best.get("authors", [])) if best else False
        if best and sim >= 0.7 and a_ok:
            ext = best.get("externalIds") or {}
            doi = ext.get("DOI")
            arxiv = ext.get("ArXiv")
            if doi:
                uid = "doi:" + doi
                src = "s2.title_match_strict"
            elif arxiv:
                uid = "arxiv:" + arxiv
                src = "s2.title_match_arxiv"
            else:
                pid = best.get("paperId")
                uid = "openalex:" + pid if pid else None
                src = "s2.paperId"
            if uid:
                ref.frontmatter["uid"] = uid
                ref.frontmatter["uid_source"] = src
                append_state_history(ref, "uid_resolved", by="worker_b_f1",
                                     meta={"uid_source": src, "score": score,
                                           "title_match": round(sim, 3)})
                save_ref(ref)
                return TransitionResult(True, "candidate", "uid_resolved", src,
                                        meta={"uid": uid, "title_sim": sim})
        elif best:
            append_acquisition_attempt(
                ref, "s2_title_search", "rejected_below_threshold",
                info={"title_sim": round(sim, 3),
                      "author_match": a_ok,
                      "reason": f"sim={round(sim, 3)}<0.7 or author_mismatch"}
            )

    # Aucun match strict : bibkey fallback (permet à la cascade de tourner)
    bibkey = _make_bibkey_fallback(author, year, title)
    ref.frontmatter["uid"] = bibkey
    ref.frontmatter["uid_source"] = "bibkey_fallback_no_crossref_s2_match"
    append_state_history(ref, "uid_resolved", by="worker_b_f1",
                         meta={"uid_source": "bibkey_fallback",
                               "note": "no_doi_no_arxiv_resolved — bibkey:"
                                       " provisoire pour que la cascade puisse"
                                       " tenter F2 (AA title) et F3 (IA)"})
    save_ref(ref)
    return TransitionResult(True, "candidate", "uid_resolved",
                            "bibkey_fallback",
                            meta={"uid": bibkey,
                                  "note": "no_doi_resolved — cascade attempted"
                                          " via title/author"})


# ─────────────────────────────────────────────────────────────────────────────
# P3 — uid_resolved → pdf_acquired (cascade)
# ─────────────────────────────────────────────────────────────────────────────

def uid_resolved_to_pdf_acquired(ref: Ref) -> TransitionResult:
    """Lance la cascade 9 niveaux. Premier succès → pdf_acquired."""
    from .cascade import run_cascade

    verdict, attempts = run_cascade(ref)

    # Logger toutes les tentatives
    for a in attempts:
        info = {k: v for k, v in a.items() if k not in ("source", "verdict")}
        append_acquisition_attempt(ref, a["source"], a["verdict"], info=info)

    if verdict == "success":
        # Dernière tentative = succès, contient pdf_path et sha256
        success = attempts[-1]
        ref.frontmatter["pdf_path"] = success["pdf_path"]
        ref.frontmatter["pdf_sha256"] = success["pdf_sha256"]
        append_state_history(ref, "pdf_acquired", by="worker_b",
                             meta={"via": success["source"]})
        save_ref(ref)
        return TransitionResult(True, "uid_resolved", "pdf_acquired",
                                success["source"],
                                meta={"pdf_path": success["pdf_path"]})

    if verdict == "scan_needs_ocr":
        # PDF local trouvé mais scan-only — route direct vers awaiting_rtfm_ocr
        info = attempts[-1]
        ref.frontmatter["pdf_path"] = info["pdf_path"]
        from datetime import datetime, timezone
        ref.frontmatter["ocr_pending_since"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        append_state_history(ref, "awaiting_rtfm_ocr", by="worker_b",
                             meta={"via": info["source"],
                                   "matched_via": info.get("matched_via")})
        save_ref(ref)
        return TransitionResult(True, "uid_resolved", "awaiting_rtfm_ocr",
                                info["source"],
                                meta={"pdf_path": info["pdf_path"]})

    # Cascade épuisée — on bascule en cascade_exhausted_needs_manual
    ref.frontmatter["blocked_by"] = "cascade_exhausted_needs_manual"
    save_ref(ref)
    return TransitionResult(False, "uid_resolved", None, "cascade_exhausted",
                            blocked_reason="all_9_sources_failed_or_skipped")


# ─────────────────────────────────────────────────────────────────────────────
# P4 — pdf_acquired_dispatch (via probe_pdf_health)
# ─────────────────────────────────────────────────────────────────────────────

def pdf_acquired_dispatch(ref: Ref) -> TransitionResult:
    """Probe PDF health + dispatch :

    - ok_has_text / ok_epub → validate_pdf_against_ref → page1_validated ou
      uid_resolved (retry cascade) selon validation.
    - scan_needs_ocr → awaiting_rtfm_ocr.
    - corrupt_unreadable / wrong_format / missing / too_small →
      needs_reacquisition + doctor_flags.

    Inclut R8 auto-fix sur `pdf_path` en amont (drift detection).
    """
    import validate_pdf_content as v

    # R8 auto-fix : normaliser pdf_path avant tout (drift detection)
    _normalize_pdf_path_inplace(ref)

    pdf_abs = ref.pdf_path_abs
    if pdf_abs is None:
        ref.frontmatter["blocked_by"] = "no_pdf_path"
        save_ref(ref)
        return TransitionResult(False, "pdf_acquired", None, "no_pdf_path",
                                blocked_reason="no_pdf_path_in_yaml")

    category, detail = v.probe_pdf_health(pdf_abs)

    if category in ("ok_has_text", "ok_epub"):
        is_ok, reason = v.validate_pdf_against_ref(
            pdf_abs,
            expected_author=ref.frontmatter.get("author") or "",
            expected_year=str(ref.frontmatter.get("year") or ""),
            expected_title=ref.frontmatter.get("title") or "",
        )
        log = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "probe_category": category,
            "verdict": reason,
        }
        ref.frontmatter["page1_validation_log"] = log
        if is_ok:
            from .cascade import cleanup_quarantine_for_ref
            n_clean = cleanup_quarantine_for_ref(ref.slug)
            append_state_history(ref, "page1_validated", by="worker_b",
                                 meta={"probe": category, "verdict": reason,
                                       "quarantine_cleaned": n_clean})
            save_ref(ref)
            return TransitionResult(True, "pdf_acquired", "page1_validated",
                                    "probe_ok_validate_passed",
                                    meta={"probe": category,
                                          "quarantine_cleaned": n_clean})
        # Validation page 1 KO → quarantaine + retour cascade
        # On bascule en needs_reacquisition pour relancer la cascade.
        ref.frontmatter.setdefault("doctor_flags", []).append(
            f"page1_failed_post_acquisition:{reason}"
        )
        append_state_history(ref, "needs_reacquisition", by="worker_b",
                             meta={"probe": category, "validation_failure": reason})
        save_ref(ref)
        return TransitionResult(True, "pdf_acquired", "needs_reacquisition",
                                "page1_validation_failed",
                                meta={"reason": reason})

    if category == "scan_needs_ocr":
        ref.frontmatter["ocr_pending_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        append_state_history(ref, "awaiting_rtfm_ocr", by="worker_b",
                             meta={"probe": "scan_needs_ocr", "detail": detail})
        save_ref(ref)
        return TransitionResult(True, "pdf_acquired", "awaiting_rtfm_ocr",
                                "scan_needs_ocr",
                                meta={"detail": detail})

    # corrupt / wrong_format / missing / too_small
    flags = ref.frontmatter.setdefault("doctor_flags", [])
    flags.append(f"{category}:{detail}")
    append_state_history(ref, "needs_reacquisition", by="worker_b",
                         meta={"probe": category, "detail": detail})
    save_ref(ref)
    return TransitionResult(True, "pdf_acquired", "needs_reacquisition",
                            f"probe_{category}",
                            meta={"category": category, "detail": detail})


# ─────────────────────────────────────────────────────────────────────────────
# P5 — awaiting_rtfm_ocr → page1_validated (via rtfm check --path)
# ─────────────────────────────────────────────────────────────────────────────

def awaiting_rtfm_ocr_dispatch(ref: Ref) -> TransitionResult:
    """Re-évalue une ref `awaiting_rtfm_ocr` via `rtfm check --path`.

    Cas gérés :
      - "ok"               : OCR done + indexé → tentative validate_pdf_against_ref.
                             Si OK : transition awaiting_rtfm_ocr → page1_validated.
                             Si KO : awaiting_rtfm_ocr → needs_reacquisition
                             (le contenu OCR ne correspond pas au titre/auteur,
                             homonymie post-OCR).
      - "still_pending"    : reste en awaiting_rtfm_ocr. Update `last_rtfm_check_at`
                             pour tracking. PAS de changement d'état.
      - "missing_in_index" : PDF pas indexé par RTFM (anomalie). On garde l'état
                             mais on log l'anomalie + on update `last_rtfm_check_at`.
      - "anomaly"          : OCR done mais 0 chunks (rare). Anomalie loggée,
                             on garde l'état awaiting_rtfm_ocr.
      - "ocr_failed"       : OCR a été tenté et a échoué. Bascule needs_reacq.
    """
    from datetime import datetime, timezone
    from .rtfm_helper import rtfm_status_for_ref

    # R8 auto-fix : normaliser pdf_path avant tout (drift detection)
    r8_fix = _normalize_pdf_path_inplace(ref)

    pdf_abs = ref.pdf_path_abs
    if pdf_abs is None:
        ref.frontmatter["blocked_by"] = "no_pdf_path_for_rtfm_check"
        save_ref(ref)
        return TransitionResult(False, "awaiting_rtfm_ocr", None, "no_pdf_path",
                                blocked_reason="no_pdf_path")

    pdf_rel = ref.frontmatter.get("pdf_path")  # path relatif depuis 10_SOURCES
    verdict, info = rtfm_status_for_ref(pdf_rel, sources_root=SOURCES)
    if r8_fix:
        info["r8_autofix"] = r8_fix

    # Toujours update timestamp dernier check (tracking visibilité)
    ref.frontmatter["last_rtfm_check_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ref.frontmatter["last_rtfm_check_verdict"] = verdict

    if verdict == "still_pending":
        # Cas explicite "Encore en attente RTFM, à retester plus tard"
        save_ref(ref)
        return TransitionResult(
            False, "awaiting_rtfm_ocr", None, "rtfm_still_pending",
            meta={"info": info, "note": "retest_later"},
            blocked_reason="rtfm_ocr_or_ingest_still_in_progress",
        )

    if verdict == "missing_in_index":
        save_ref(ref)
        return TransitionResult(
            False, "awaiting_rtfm_ocr", None, "rtfm_missing_in_index",
            meta={"info": info, "anomaly": True},
            blocked_reason="pdf_not_in_rtfm_index_despite_being_on_disk",
        )

    if verdict == "anomaly":
        save_ref(ref)
        return TransitionResult(
            False, "awaiting_rtfm_ocr", None, "rtfm_anomaly_zero_chunks",
            meta={"info": info, "anomaly": True},
            blocked_reason="ocr_done_but_zero_chunks_in_index",
        )

    if verdict == "ocr_failed":
        # Bascule needs_reacquisition : OCR a échoué, re-télécharger
        ref.frontmatter.setdefault("doctor_flags", []).append(
            f"rtfm_ocr_failed:retry_acquisition"
        )
        append_state_history(ref, "needs_reacquisition", by="worker_b_p5",
                             meta={"reason": "rtfm_ocr_failed", "info": info})
        save_ref(ref)
        return TransitionResult(
            True, "awaiting_rtfm_ocr", "needs_reacquisition",
            "rtfm_ocr_failed",
            meta={"info": info},
        )

    # verdict == "ok" : OCR done, indexé. Le PDF reste scan-only sur disque
    # (RTFM ne réécrit pas le fichier). On valide page 1 via le texte OCR
    # extrait depuis l'index RTFM, pas via pdftotext sur le PDF brut.
    import validate_pdf_content as v
    from .rtfm_helper import rtfm_first_chunks_text
    rtfm_slug = info.get("rtfm_slug") or ""
    ocr_text = rtfm_first_chunks_text(rtfm_slug, n_chunks=10) if rtfm_slug else ""
    if ocr_text:
        # Validation page 1 sur le texte OCR de RTFM
        is_ok, reason = v.validate_text_against_ref(
            ocr_text,
            expected_author=ref.frontmatter.get("author") or "",
            expected_year=str(ref.frontmatter.get("year") or ""),
            expected_title=ref.frontmatter.get("title") or "",
        )
    else:
        # Fallback : pas de texte OCR récupérable, retomber sur la validation
        # classique (qui échouera probablement si le PDF est scan-only)
        is_ok, reason = v.validate_pdf_against_ref(
            pdf_abs,
            expected_author=ref.frontmatter.get("author") or "",
            expected_year=str(ref.frontmatter.get("year") or ""),
            expected_title=ref.frontmatter.get("title") or "",
        )
        reason = f"{reason} [fallback_no_rtfm_chunks]"
    log = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "post_rtfm_ocr": True,
        "verdict": reason,
        "rtfm_chunks": info["chunks"],
    }
    ref.frontmatter["page1_validation_log"] = log
    if is_ok:
        from .cascade import cleanup_quarantine_for_ref
        n_clean = cleanup_quarantine_for_ref(ref.slug)
        append_state_history(ref, "page1_validated", by="worker_b_p5",
                             meta={"via": "rtfm_ocr_completion",
                                   "rtfm_chunks": info["chunks"],
                                   "quarantine_cleaned": n_clean})
        save_ref(ref)
        return TransitionResult(
            True, "awaiting_rtfm_ocr", "page1_validated",
            "rtfm_ocr_completion",
            meta={"chunks": info["chunks"],
                  "quarantine_cleaned": n_clean},
        )
    # Validation page 1 échoue MÊME après OCR : probable homonymie (mauvais
    # contenu acquis), bascule needs_reacq.
    ref.frontmatter.setdefault("doctor_flags", []).append(
        f"page1_failed_post_ocr:{reason}"
    )
    append_state_history(ref, "needs_reacquisition", by="worker_b_p5",
                         meta={"reason": "page1_failed_post_ocr",
                               "validation_reason": reason,
                               "rtfm_chunks": info["chunks"]})
    save_ref(ref)
    return TransitionResult(
        True, "awaiting_rtfm_ocr", "needs_reacquisition",
        "page1_failed_post_ocr",
        meta={"reason": reason, "info": info},
    )


# ─────────────────────────────────────────────────────────────────────────────
# P6 — needs_reacquisition → uid_resolved (re-cascade)
# ─────────────────────────────────────────────────────────────────────────────

def needs_reacquisition_to_uid_resolved(ref: Ref) -> TransitionResult:
    """Bascule pour relance cascade. Les attempts déjà loggés évitent les
    sources déjà tentées (logique `already_tried` dans cascade.py)."""
    append_state_history(ref, "uid_resolved", by="worker_b",
                         meta={"reason": "ready_for_cascade_retry",
                               "doctor_flags": ref.frontmatter.get("doctor_flags", [])})
    save_ref(ref)
    return TransitionResult(True, "needs_reacquisition", "uid_resolved",
                            "ready_for_retry")


# Registre des transitions disponibles.
REGISTRY: dict = {
    "candidate_to_uid_resolved": candidate_to_uid_resolved,
    "uid_resolved_to_pdf_acquired": uid_resolved_to_pdf_acquired,
    "pdf_acquired_dispatch": pdf_acquired_dispatch,
    "needs_reacquisition_to_uid_resolved": needs_reacquisition_to_uid_resolved,
}


# Exception conservée pour compat avec cli.py
class NotImplementedYet(Exception):
    """Conservée pour signaler une transition non-branchée (aucune actuellement)."""
