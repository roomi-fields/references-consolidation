"""Invariants I1-I15 — sur-couche worker (orthogonale au linter R1-R10).

Chaque fonction `check_I<n>(ref)` (ou `check_I<n>(refs)` pour les invariants
registry-level) retourne une liste de Violation (vide si pas de violation).

Sévérités :
  - ERROR : I1, I2, I3, I5, I6, I7, I8, I10, I14
  - WARN  : I4, I9, I11, I12, I13
  - INFO  : I15

Auto-fix : I4 (R8 strip prefix), I6 (recompute sha256), I9 (renumber attempts).

Toutes les fonctions sont **read-only** sur la ref. Les `fix_fn` retournés
dans les Violation mutent + save_ref().

Cf. plans/plan-design.md §1 et §8 (Couche 1).
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import SOURCES, VAULT, BLOCKED_PREFIX, TERMINAL_STATES
from .registry import Ref, save_ref
from .transitions import _normalize_pdf_path_inplace


# ─────────────────────────────────────────────────────────────────────────────
# Constantes — états et préfixes UID acceptés
# ─────────────────────────────────────────────────────────────────────────────

# Énumération canonique cf. plan §1 / FSM 8 états
VALID_STATES = {
    "candidate",
    "uid_resolved",
    "pdf_acquired",
    "awaiting_rtfm_ocr",
    "needs_reacquisition",
    "page1_validated",
    "sota_cited_confirmed",
    "retracted",
}

# Préfixes UID acceptés (cf. plan §1 I3)
VALID_UID_PREFIXES = ("doi:", "arxiv:", "isbn:", "url:", "openalex:", "bibkey:")

# États impliquant qu'un PDF a été acquis sur disque
STATES_WITH_PDF = {
    "pdf_acquired",
    "awaiting_rtfm_ocr",
    "needs_reacquisition",
    "page1_validated",
    "sota_cited_confirmed",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9_]+)\]\]")


def _parse_iso(value) -> datetime | None:
    """Parse une date ou datetime ISO en datetime UTC-naive. None si invalide."""
    if value is None:
        return None
    s = str(value)
    # Tolérer le Z final, l'absence d'offset, et le format date-only
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _is_iso_date_like(value) -> bool:
    """True si la valeur ressemble à une date ISO (YYYY-MM-DD ou full)."""
    if value is None:
        return False
    s = str(value)
    return bool(_ISO_DATE_RE.match(s))


def _compute_sha256(path: Path) -> str | None:
    """sha256 hex d'un fichier. None si lecture impossible."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Violation — déclarée dans doctor.py pour API publique, mais on import-cycle
# évité en typant ici en str/None/Callable. La construction des Violation se
# fait dans le module doctor (orchestrateur).
# ─────────────────────────────────────────────────────────────────────────────

# Pour éviter import cycle on déclare ici une factory simple (typed dict-like)
# que doctor.py convertira en `Violation` dataclass.
def _viol(invariant: str, ref_slug: str | None, severity: str,
          message: str, auto_fixable: bool = False,
          fix_fn: Callable[[Ref], None] | None = None) -> dict:
    return {
        "invariant": invariant,
        "ref_slug": ref_slug,
        "severity": severity,
        "message": message,
        "auto_fixable": auto_fixable,
        "fix_fn": fix_fn,
    }


# ─────────────────────────────────────────────────────────────────────────────
# I1 — state ∈ enum FSM ou blocked_human:* (ERROR, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def check_I1(ref: Ref) -> list[dict]:
    state = ref.state
    if state in VALID_STATES:
        return []
    if state.startswith(BLOCKED_PREFIX + ":"):
        return []
    return [_viol(
        "I1", ref.slug, "ERROR",
        f"state inconnu : {state!r} (attendu : {sorted(VALID_STATES)} ou blocked_human:*)",
        auto_fixable=False,
    )]


# ─────────────────────────────────────────────────────────────────────────────
# I2 — slug unique sur l'ensemble du registre (ERROR, non auto-fix)
# Note : registry-level. La fonction prend un iterable de refs.
# ─────────────────────────────────────────────────────────────────────────────

def check_I2(refs: list[Ref]) -> list[dict]:
    seen: dict[str, list[Path]] = {}
    for r in refs:
        seen.setdefault(r.slug, []).append(r.path)
    violations = []
    for slug, paths in seen.items():
        if len(paths) > 1:
            paths_str = " ; ".join(str(p) for p in paths)
            violations.append(_viol(
                "I2", slug, "ERROR",
                f"slug en doublon : {len(paths)} fichiers ({paths_str})",
                auto_fixable=False,
            ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I3 — uid préfixe valide (ERROR, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def check_I3(ref: Ref) -> list[dict]:
    uid = ref.frontmatter.get("uid")
    if uid is None or uid == "":
        return []  # None est valide
    if not isinstance(uid, str):
        return [_viol(
            "I3", ref.slug, "ERROR",
            f"uid n'est pas une chaîne : {type(uid).__name__}",
            auto_fixable=False,
        )]
    if not any(uid.startswith(pre) for pre in VALID_UID_PREFIXES):
        return [_viol(
            "I3", ref.slug, "ERROR",
            f"uid sans préfixe valide : {uid!r} (attendu : {VALID_UID_PREFIXES})",
            auto_fixable=False,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I4 — pdf_path relatif depuis 10_SOURCES (WARN, auto-fix R8)
# ─────────────────────────────────────────────────────────────────────────────

def _fix_I4(ref: Ref) -> None:
    """Auto-fix R8 : strip le préfixe `10_SOURCES/` (réutilise transitions._normalize_pdf_path_inplace)."""
    _normalize_pdf_path_inplace(ref)


def check_I4(ref: Ref) -> list[dict]:
    pp = ref.frontmatter.get("pdf_path")
    if not pp:
        return []
    if pp.startswith("10_SOURCES/"):
        return [_viol(
            "I4", ref.slug, "WARN",
            f"pdf_path préfixé '10_SOURCES/' : {pp!r} (auto-fixable R8)",
            auto_fixable=True,
            fix_fn=_fix_I4,
        )]
    if pp.startswith("/"):
        return [_viol(
            "I4", ref.slug, "WARN",
            f"pdf_path absolu : {pp!r} (doit être relatif depuis 10_SOURCES)",
            auto_fixable=False,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I5 — Si state implique PDF, alors le fichier existe (ERROR, semi auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def _fix_I5(ref: Ref) -> None:
    """Semi auto-fix : bascule state en needs_reacquisition + flag doctor."""
    from .registry import append_state_history
    flags = ref.frontmatter.setdefault("doctor_flags", [])
    flags.append("pdf_missing_on_disk")
    append_state_history(ref, "needs_reacquisition", by="doctor_i5_autofix",
                         meta={"reason": "pdf_missing_on_disk"})
    save_ref(ref)


def check_I5(ref: Ref) -> list[dict]:
    state = ref.state
    if state not in STATES_WITH_PDF:
        return []
    pp = ref.frontmatter.get("pdf_path")
    if not pp:
        return [_viol(
            "I5", ref.slug, "ERROR",
            f"state={state!r} mais pdf_path absent",
            auto_fixable=False,
        )]
    pdf_abs = ref.pdf_path_abs
    if pdf_abs is None or not pdf_abs.exists():
        return [_viol(
            "I5", ref.slug, "ERROR",
            f"state={state!r} mais pdf_path inexistant sur disque : {pp}",
            auto_fixable=True,  # semi : bascule needs_reacquisition
            fix_fn=_fix_I5,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I6 — pdf_sha256 valide 64 hex (ERROR, auto-fix recompute)
# ─────────────────────────────────────────────────────────────────────────────

def _fix_I6(ref: Ref) -> None:
    """Recompute sha256 depuis le fichier et update."""
    pdf_abs = ref.pdf_path_abs
    if pdf_abs is None or not pdf_abs.exists():
        # Sans fichier on ne peut pas recompute. C'est I5 qui sera levé.
        return
    sha = _compute_sha256(pdf_abs)
    if sha is None:
        return
    ref.frontmatter["pdf_sha256"] = sha
    save_ref(ref)


def check_I6(ref: Ref) -> list[dict]:
    state = ref.state
    if state not in STATES_WITH_PDF:
        return []
    sha = ref.frontmatter.get("pdf_sha256")
    if sha is None or sha == "":
        return [_viol(
            "I6", ref.slug, "ERROR",
            f"state={state!r} mais pdf_sha256 absent",
            auto_fixable=True,
            fix_fn=_fix_I6,
        )]
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return [_viol(
            "I6", ref.slug, "ERROR",
            f"pdf_sha256 invalide : {sha!r} (attendu 64 hex)",
            auto_fixable=True,
            fix_fn=_fix_I6,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I7 — page1_validated cohérent avec page1_validation_log (ERROR, semi auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def _fix_I7(ref: Ref) -> None:
    """Semi auto-fix : bascule en needs_reacquisition."""
    from .registry import append_state_history
    flags = ref.frontmatter.setdefault("doctor_flags", [])
    flags.append("page1_validation_log_inconsistent")
    append_state_history(ref, "needs_reacquisition", by="doctor_i7_autofix",
                         meta={"reason": "page1_validation_log_inconsistent"})
    save_ref(ref)


def check_I7(ref: Ref) -> list[dict]:
    if ref.state != "page1_validated":
        return []
    log = ref.frontmatter.get("page1_validation_log")
    if not isinstance(log, dict):
        return [_viol(
            "I7", ref.slug, "ERROR",
            f"state=page1_validated mais page1_validation_log absent ou invalide",
            auto_fixable=True,
            fix_fn=_fix_I7,
        )]
    verdict = log.get("verdict") or log.get("validator_reason") or ""
    if "validated" not in str(verdict).lower():
        return [_viol(
            "I7", ref.slug, "ERROR",
            f"page1_validation_log.verdict ne contient pas 'validated' : {verdict!r}",
            auto_fixable=True,
            fix_fn=_fix_I7,
        )]
    at = log.get("at")
    if not _is_iso_date_like(at):
        return [_viol(
            "I7", ref.slug, "ERROR",
            f"page1_validation_log.at non ISO : {at!r}",
            auto_fixable=True,
            fix_fn=_fix_I7,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I8 — state_history monotone et dernier = state actuel (ERROR, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def check_I8(ref: Ref) -> list[dict]:
    hist = ref.frontmatter.get("state_history") or []
    if not hist:
        return []  # vide est acceptable (refs candidate jamais touchées)
    violations = []
    # Vérifier monotonie temporelle
    prev_dt = None
    for i, entry in enumerate(hist):
        at = entry.get("at") if isinstance(entry, dict) else None
        dt = _parse_iso(at)
        if dt is None:
            # Tolérance : un at illisible ne bloque pas la monotonie, on saute
            continue
        if prev_dt is not None and dt < prev_dt:
            violations.append(_viol(
                "I8", ref.slug, "ERROR",
                f"state_history[{i}].at={at!r} antérieur à entry précédente",
                auto_fixable=False,
            ))
            break
        prev_dt = dt
    # Vérifier que le dernier état = state actuel
    last = hist[-1] if isinstance(hist[-1], dict) else None
    if last is not None:
        last_state = last.get("state")
        if last_state != ref.state:
            violations.append(_viol(
                "I8", ref.slug, "ERROR",
                f"state_history[-1].state={last_state!r} ≠ frontmatter.state={ref.state!r}",
                auto_fixable=False,
            ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I9 — acquisition_attempts.n monotone 1..N (WARN, auto-fix renumber)
# ─────────────────────────────────────────────────────────────────────────────

def _fix_I9(ref: Ref) -> None:
    """Renumber les n en 1..N dans l'ordre d'apparition."""
    attempts = ref.frontmatter.get("acquisition_attempts") or []
    for i, a in enumerate(attempts):
        if isinstance(a, dict):
            a["n"] = i + 1
    ref.frontmatter["acquisition_attempts"] = attempts
    save_ref(ref)


def check_I9(ref: Ref) -> list[dict]:
    attempts = ref.frontmatter.get("acquisition_attempts") or []
    if not attempts:
        return []
    expected = 1
    for a in attempts:
        if not isinstance(a, dict):
            continue
        n = a.get("n")
        if n != expected:
            return [_viol(
                "I9", ref.slug, "WARN",
                f"acquisition_attempts non monotone : attendu n={expected}, trouvé n={n!r}",
                auto_fixable=True,
                fix_fn=_fix_I9,
            )]
        expected += 1
    return []


# ─────────────────────────────────────────────────────────────────────────────
# I10 — blocked_human:* requiert blocked_reason + blocked_since (ERROR, non auto-fix)
# DÉTECTE MAIS NE FIXE PAS — décision humaine (anti-heuristique)
# ─────────────────────────────────────────────────────────────────────────────

def check_I10(ref: Ref) -> list[dict]:
    state = ref.state
    if not state.startswith(BLOCKED_PREFIX + ":"):
        return []
    reason = ref.frontmatter.get("blocked_reason")
    since = ref.frontmatter.get("blocked_since")
    violations = []
    if not reason or (isinstance(reason, str) and not reason.strip()):
        violations.append(_viol(
            "I10", ref.slug, "ERROR",
            f"state={state!r} mais blocked_reason vide ou absent (passe humaine requise)",
            auto_fixable=False,
        ))
    if not _is_iso_date_like(since):
        violations.append(_viol(
            "I10", ref.slug, "ERROR",
            f"state={state!r} mais blocked_since absent ou non ISO : {since!r}",
            auto_fixable=False,
        ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I11 — cited_in pointe vers un SOTA/Paper existant (WARN, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def _sota_or_paper_exists(name: str, vault_root: Path = VAULT) -> bool:
    """Cherche un fichier dans Publications/ ou Articles/ avec ce nom."""
    if not name:
        return False
    # Le name peut être tel quel (sans .md) ou avec .md
    base = name[:-3] if name.endswith(".md") else name
    candidates = [
        vault_root / "Publications" / f"{base}.md",
        vault_root / "Articles" / f"{base}.md",
    ]
    return any(c.exists() for c in candidates)


def check_I11(ref: Ref, vault_root: Path = VAULT) -> list[dict]:
    citations = ref.frontmatter.get("cited_in") or []
    if not isinstance(citations, list):
        return []
    violations = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        if not _sota_or_paper_exists(name, vault_root):
            violations.append(_viol(
                "I11", ref.slug, "WARN",
                f"cited_in:{name!r} introuvable dans Publications/ ou Articles/",
                auto_fixable=False,
            ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I12 — Réciprocité SOTA → cited_in (WARN, non auto-fix, registry-level)
# ─────────────────────────────────────────────────────────────────────────────

def check_I12(refs: list[Ref], vault_root: Path = VAULT) -> list[dict]:
    """Pour chaque SOTA/Paper du vault, vérifie que les wikilinks vers refs
    sont déclarés dans cited_in du côté ref.

    Lecture passive — scan SOTA → ref.
    """
    violations: list[dict] = []
    refs_by_slug = {r.slug: r for r in refs}

    sota_dirs = [vault_root / "Publications", vault_root / "Articles"]
    for sd in sota_dirs:
        if not sd.exists():
            continue
        for sota_path in sd.glob("*.md"):
            try:
                body = sota_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sota_name = sota_path.stem
            for m in _WIKILINK_RE.finditer(body):
                slug = m.group(1)
                if slug not in refs_by_slug:
                    continue  # ce wikilink ne vise pas une ref (peut viser autre chose)
                ref = refs_by_slug[slug]
                citations = ref.frontmatter.get("cited_in") or []
                declared_names = {c.get("name") for c in citations
                                  if isinstance(c, dict)}
                if sota_name not in declared_names:
                    violations.append(_viol(
                        "I12", slug, "WARN",
                        f"SOTA/Paper {sota_name!r} cite [[{slug}]] mais ref.cited_in ne contient pas {sota_name!r}",
                        auto_fixable=False,
                    ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I13 — pdf_sha256 unique sur le registre (WARN, non auto-fix, registry-level)
# ─────────────────────────────────────────────────────────────────────────────

def check_I13(refs: list[Ref]) -> list[dict]:
    by_sha: dict[str, list[str]] = {}
    for r in refs:
        sha = r.frontmatter.get("pdf_sha256")
        if not sha or not isinstance(sha, str) or not _SHA256_RE.match(sha):
            continue
        by_sha.setdefault(sha, []).append(r.slug)
    violations = []
    for sha, slugs in by_sha.items():
        if len(slugs) > 1:
            slugs_str = ", ".join(slugs)
            # Émettre une violation par ref impliquée (le slug en attribut est
            # celui de la ref, pour permettre un filtrage par slug côté caller).
            for slug in slugs:
                violations.append(_viol(
                    "I13", slug, "WARN",
                    f"pdf_sha256 partagé avec {len(slugs)-1} autre(s) ref(s) : {slugs_str} (sha={sha[:12]}…)",
                    auto_fixable=False,
                ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I14 — Aucune transition sortante depuis un état terminal (ERROR, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def check_I14(ref: Ref) -> list[dict]:
    hist = ref.frontmatter.get("state_history") or []
    if len(hist) < 2:
        return []
    violations = []
    for i in range(len(hist) - 1):
        prev = hist[i] if isinstance(hist[i], dict) else None
        nxt = hist[i + 1] if isinstance(hist[i + 1], dict) else None
        if prev is None or nxt is None:
            continue
        prev_state = prev.get("state")
        nxt_state = nxt.get("state")
        if prev_state in TERMINAL_STATES and nxt_state != prev_state:
            violations.append(_viol(
                "I14", ref.slug, "ERROR",
                f"state_history[{i}]={prev_state!r} (terminal) → [{i+1}]={nxt_state!r} (transition interdite)",
                auto_fixable=False,
            ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# I15 — awaiting_rtfm_ocr retard (INFO, non auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def check_I15(ref: Ref, now: datetime | None = None) -> list[dict]:
    if ref.state != "awaiting_rtfm_ocr":
        return []
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    ocr_since = _parse_iso(ref.frontmatter.get("ocr_pending_since"))
    last_check = _parse_iso(ref.frontmatter.get("last_rtfm_check_at"))
    if ocr_since is None:
        return []
    days_pending = (now - ocr_since).days
    if days_pending <= 30:
        return []
    # Si last_check récent, on n'alerte pas — RTFM est encore actif sur ce dossier
    if last_check is not None:
        days_since_check = (now - last_check).days
        if days_since_check <= 7:
            return []
    return [_viol(
        "I15", ref.slug, "INFO",
        f"awaiting_rtfm_ocr depuis {days_pending}j, "
        f"last_rtfm_check_at={'jamais' if last_check is None else f'il y a {(now - last_check).days}j'}"
        " — suggérer `pipeline reactivate-ocr`",
        auto_fixable=False,
    )]


# ─────────────────────────────────────────────────────────────────────────────
# Registre des checks (pour doctor.run_all_checks)
# ─────────────────────────────────────────────────────────────────────────────

# Checks ref-level : prennent un Ref, retournent list[dict]
REF_LEVEL_CHECKS: list[tuple[str, Callable[[Ref], list[dict]]]] = [
    ("I1", check_I1),
    ("I3", check_I3),
    ("I4", check_I4),
    ("I5", check_I5),
    ("I6", check_I6),
    ("I7", check_I7),
    ("I8", check_I8),
    ("I9", check_I9),
    ("I10", check_I10),
    ("I11", check_I11),
    ("I14", check_I14),
    ("I15", check_I15),
]

# Checks registry-level : prennent list[Ref], retournent list[dict]
REGISTRY_LEVEL_CHECKS: list[tuple[str, Callable[[list[Ref]], list[dict]]]] = [
    ("I2", check_I2),
    ("I12", check_I12),
    ("I13", check_I13),
]


# Index des sévérités pour affichage rapide
SEVERITY_BY_INVARIANT = {
    "I1": "ERROR", "I2": "ERROR", "I3": "ERROR", "I5": "ERROR", "I6": "ERROR",
    "I7": "ERROR", "I8": "ERROR", "I10": "ERROR", "I14": "ERROR",
    "I4": "WARN", "I9": "WARN", "I11": "WARN", "I12": "WARN", "I13": "WARN",
    "I15": "INFO",
}
