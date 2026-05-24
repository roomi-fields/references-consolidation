"""Wrapper Python autour de `rtfm check` CLI.

Le worker B utilise `rtfm check --path <pdf_path>` (recommandé par l'agent
RTFM 2026-05-24) pour interroger l'état d'un document dans l'index local
sans dépendre de la nomenclature slugs (registry slugs ≠ RTFM slugs).

Retourne un dict normalisé avec les champs utiles pour la FSM :
  - matches: int (0 = pas dans l'index, 1+ = trouvé)
  - searchable: bool (chunks et index OK)
  - ocr_pending: bool (OCR pas encore fait)
  - ocr_attempted: bool (OCR essayé, distingue jamais-tenté vs en-cours)
  - ocr_failed: bool (OCR essayé et échoué — anomalie)
  - ingest_pending: bool (chunks pas encore indexés)
  - chunks: int
  - embeddings: int
  - slug: str (le slug RTFM, pour debug/log)
  - filename: str
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path


def rtfm_check_path(path: str | Path, timeout: int = 15) -> dict:
    """Appel `rtfm check --path <path>` et retourne le JSON parsé.

    Note `rtfm check` exit code :
      - 0 : match trouvé
      - 2 : pas de match (mais JSON valide avec matches=0)
      - autre : vraie erreur
    On accepte exit code 0 ET 2 du moment que le stdout est parseable.

    Retourne toujours un dict — en cas d'erreur CLI réelle, dict avec
    `error` et `matches: 0`.
    """
    proc = subprocess.run(
        ["rtfm", "check", "--path", str(path), "--format", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    # rtfm check exit=2 = "no match" mais JSON quand même valide
    if proc.returncode not in (0, 2):
        return {"matches": 0, "error": f"rtfm_check_rc={proc.returncode}",
                "stderr": (proc.stderr or "")[:200]}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {"matches": 0, "error": f"json_decode:{e}",
                "stdout_head": (proc.stdout or "")[:200]}
    return data


def rtfm_status_for_ref(pdf_path: str | Path, sources_root: Path | None = None) -> tuple[str, dict]:
    """Status normalisé pour la FSM.

    Combine `rtfm check` avec une interprétation FSM-friendly :
      - "ok"               : OCR + indexation OK, prêt pour validation page 1
      - "still_pending"    : OCR/ingest en cours OU RTFM pas encore scanné le
                             fichier (matches=0 mais fichier sur disque)
      - "missing_in_index" : matches=0 ET fichier n'existe pas sur disque
                             (vraie anomalie : pdf_path invalide)
      - "anomaly"          : OCR done mais 0 chunks (rare, anomalie)
      - "ocr_failed"       : OCR a été essayé et a échoué (bascule needs_reacq)

    `sources_root` (optionnel) : chemin racine pour résoudre `pdf_path`
    relatif. Si fourni et matches=0, on vérifie l'existence physique du
    fichier pour distinguer "RTFM pas encore scanné" vs "vraie anomalie".
    """
    result = rtfm_check_path(pdf_path)
    if result.get("error"):
        return "missing_in_index", {"reason": "rtfm_cli_error", **result}
    if result.get("matches", 0) == 0:
        # Distinguer "RTFM pas encore scanné" vs "vraie anomalie"
        on_disk = None
        if sources_root is not None:
            p = Path(pdf_path)
            abs_path = p if p.is_absolute() else (sources_root / pdf_path)
            on_disk = abs_path.exists()
        if on_disk is True:
            return "still_pending", {"reason": "on_disk_but_not_in_rtfm_index_yet",
                                     "note": "rtfm_scan_pas_encore_passe_sur_ce_fichier"}
        if on_disk is False:
            return "missing_in_index", {"reason": "pdf_path_does_not_exist_on_disk",
                                        "anomaly": True}
        return "missing_in_index", {"reason": "no_match_in_rtfm_index"}
    books = result.get("books") or []
    if not books:
        return "missing_in_index", {"reason": "empty_books_array"}
    b = books[0]
    info = {
        "rtfm_slug": b.get("slug"),
        "filename": b.get("filename"),
        "chunks": b.get("chunks", 0),
        "embeddings": b.get("embeddings", 0),
        "searchable": b.get("searchable", False),
        "ocr_pending": b.get("ocr_pending", False),
        "ocr_attempted": b.get("ocr_attempted", False),
        "ocr_failed": b.get("ocr_failed", False),
        "ingest_pending": b.get("ingest_pending", False),
        "ingest_failed": b.get("ingest_failed", False),
    }
    if info["ocr_failed"] or info["ingest_failed"]:
        return "ocr_failed", info
    if info["ocr_pending"] or info["ingest_pending"]:
        return "still_pending", info
    if info["chunks"] == 0:
        return "anomaly", {**info, "reason": "ocr_done_but_zero_chunks"}
    return "ok", info
