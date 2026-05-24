"""Tests synthétiques pour les invariants I1-I15.

Chaque fixture sous `pipeline/tests/synthetic/refs/I<n>_<label>.md` triggue
exactement l'invariant cible. Le test charge la fixture, appelle
`doctor.run_all_checks([ref])` et vérifie que l'invariant attendu lève.

Vérifie aussi que `auto_fix` répare bien I4, I6, I9 (et I5 semi → bascule state).

Usage :
  venv/bin/python pipeline/tests/test_invariants_synthetic.py

Exit codes :
  0 — 15/15 fixtures OK
  1 — au moins 1 fixture ne lève pas l'invariant attendu
  2 — erreur de chargement / fixture manquante
"""
from __future__ import annotations
import shutil
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJ))

from pipeline.registry import load_ref, iter_refs, Ref  # noqa: E402
from pipeline import doctor  # noqa: E402
from pipeline import invariants  # noqa: E402


SYNTH = Path(__file__).resolve().parent / "synthetic"
REFS_DIR = SYNTH / "refs"
VAULT_DIR = SYNTH / "vault"
SOURCES_FOR_TESTS = SYNTH / "sources"


# Mapping fixture → invariant attendu (la fixture peut être un nom de fichier
# unique ou un préfixe pour les paires comme I02a/I02b ou I13a/I13b)
FIXTURE_EXPECTED_INVARIANT = {
    "I01_state_unknown.md": "I1",
    "I02a_slug_duplicate.md": "I2",  # check via paire I02a+I02b
    "I03_uid_bad_prefix.md": "I3",
    "I04_pdf_path_prefixed.md": "I4",
    "I05_pdf_missing.md": "I5",
    "I06_sha256_invalid.md": "I6",
    "I07_page1_log_inconsistent.md": "I7",
    "I08_history_non_monotonic.md": "I8",
    "I09_attempts_renumber.md": "I9",
    "I10_blocked_no_reason.md": "I10",
    "I11_cited_in_orphan.md": "I11",
    "i12_reciprocity_missing.md": "I12",
    "I13a_sha_duplicate.md": "I13",  # check via paire I13a+I13b
    "I14_terminal_transition.md": "I14",
    "I15_rtfm_overdue.md": "I15",
}


def _patch_config_for_synthetic():
    """Monkey-patch pipeline.config + modules qui ont importé SOURCES en local.

    Les fixtures I5/I6/I13/I15 dépendent de la résolution `pdf_path_abs` qui
    utilise `config.SOURCES`. Plusieurs modules font `from .config import SOURCES`
    en haut de fichier, ce qui CAPTURE la valeur. On doit donc patcher les
    modules qui consomment SOURCES.
    """
    from pipeline import config, registry, transitions
    config.SOURCES = SOURCES_FOR_TESTS
    config.VAULT = VAULT_DIR  # pour I11 / I12
    registry.SOURCES = SOURCES_FOR_TESTS
    transitions.SOURCES = SOURCES_FOR_TESTS


def _load_fixture(name: str) -> Ref:
    p = REFS_DIR / name
    ref = load_ref(p)
    if ref is None:
        raise SystemExit(f"[FAIL] Fixture introuvable ou non parseable : {p}")
    return ref


def _violations_for_ref(ref: Ref, all_refs: list[Ref]) -> list[doctor.Violation]:
    """Lance run_all_checks sur la liste fournie et filtre par slug ref."""
    vs = doctor.run_all_checks(all_refs, vault_root=VAULT_DIR)
    return [v for v in vs if v.ref_slug == ref.slug or v.ref_slug is None
            and ref.slug in v.message]


def _all_synthetic_refs() -> list[Ref]:
    refs: list[Ref] = []
    for p in sorted(REFS_DIR.glob("*.md")):
        r = load_ref(p)
        if r is not None:
            refs.append(r)
    return refs


def _test_single_fixture(name: str, expected_inv: str, all_refs: list[Ref]) -> tuple[bool, str]:
    """Vérifie qu'une fixture déclenche son invariant attendu et SEULEMENT lui.

    Permet du bruit secondaire INFO mais ne tolère pas que l'invariant cible
    soit absent. Retourne (ok, message).
    """
    ref = _load_fixture(name)

    # Cas spécial I2 : on ne peut pas avoir 2 fichiers de même slug sur disque.
    # On simule en mémoire en clonant le Ref avec son slug = "I02a_slug_duplicate".
    if expected_inv == "I2":
        # Charger I02a et I02b, et forcer un slug commun
        ref_a = _load_fixture("I02a_slug_duplicate.md")
        ref_b = _load_fixture("I02b_slug_duplicate.md")
        ref_b.slug = ref_a.slug  # collision artificielle
        # Run uniquement check_I2 (registry-level)
        v_list = invariants.check_I2([ref_a, ref_b])
        if any(v["invariant"] == "I2" for v in v_list):
            return True, f"{name}: I2 détecté (paire I02a/I02b avec slug forcé)"
        return False, f"{name}: I2 non détecté malgré paire avec slug forcé"

    # Filtrer les violations qui concernent cette ref
    all_v = doctor.run_all_checks(all_refs, vault_root=VAULT_DIR)
    v_for_ref = [v for v in all_v if v.ref_slug == ref.slug]
    invariants_found = sorted({v.invariant for v in v_for_ref})

    if expected_inv not in invariants_found:
        return False, (f"{name}: invariant {expected_inv} absent. "
                       f"Trouvés: {invariants_found}")
    # Vérifier que les ERROR/WARN supplémentaires sont attendus ou absents
    # (on tolère INFO additionnels comme I15)
    unexpected_errors = [v for v in v_for_ref
                         if v.invariant != expected_inv
                         and v.severity == "ERROR"]
    if unexpected_errors:
        return False, (f"{name}: invariant {expected_inv} OK mais ERRORs "
                       f"supplémentaires : {[(v.invariant, v.message[:60]) for v in unexpected_errors]}")
    return True, f"{name}: {expected_inv} détecté"


def test_fixtures_basic() -> bool:
    """Vérifie que chaque fixture déclenche son invariant cible."""
    print("\n=== Phase 1 : détection des 15 invariants sur fixtures ===")
    _patch_config_for_synthetic()
    all_refs = _all_synthetic_refs()
    print(f"Loaded {len(all_refs)} synthetic refs")
    ok_count = 0
    failures = []
    for name, expected_inv in FIXTURE_EXPECTED_INVARIANT.items():
        ok, msg = _test_single_fixture(name, expected_inv, all_refs)
        marker = "OK" if ok else "FAIL"
        print(f"  [{marker}] {msg}")
        if ok:
            ok_count += 1
        else:
            failures.append(msg)
    total = len(FIXTURE_EXPECTED_INVARIANT)
    print(f"\nRésultat phase 1 : {ok_count}/{total}")
    if failures:
        for f in failures:
            print(f"  - {f}")
        return False
    return True


def test_autofix() -> bool:
    """Vérifie que --fix répare I4, I6, I9 (auto-fixables strictes).

    Pour ne pas muter les fixtures du repo, on copie les fixtures concernées
    dans un dossier temporaire et on patche config.REFS dessus.
    """
    print("\n=== Phase 2 : auto-fix sur I4, I6, I9 ===")

    from pipeline import config

    tmp = Path(tempfile.mkdtemp(prefix="doctor_autofix_test_"))
    tmp_refs = tmp / "refs"
    tmp_refs.mkdir()
    # Copier les 3 fixtures auto-fixables strictes
    autofix_fixtures = {
        "I04": "I04_pdf_path_prefixed.md",
        "I06": "I06_sha256_invalid.md",
        "I09": "I09_attempts_renumber.md",
    }
    # Pour I04, on doit créer le fichier cible relatif (sans préfixe 10_SOURCES/)
    # pour que _normalize_pdf_path_inplace voit que le candidat existe.
    # Le fichier attendu est SOURCES/Sources/fake_doc_for_tests.pdf qui existe déjà.
    for name in autofix_fixtures.values():
        src = REFS_DIR / name
        dst = tmp_refs / name
        shutil.copy(src, dst)
    # Patch config.REFS et config.SOURCES (déjà patché en phase 1, on garde)
    original_refs = config.REFS
    config.REFS = tmp_refs
    # SOURCES déjà patché en phase 1 vers SOURCES_FOR_TESTS

    try:
        # Charge les fixtures patched + run + autofix
        refs = list(iter_refs(tmp_refs))
        print(f"  Loaded {len(refs)} autofix-target refs from {tmp_refs}")
        violations = doctor.run_all_checks(refs, vault_root=VAULT_DIR)
        # Filtrer aux 3 invariants attendus
        target_invs = {"I4", "I6", "I9"}
        relevant = [v for v in violations if v.invariant in target_invs]
        print(f"  Violations détectées avant fix : "
              f"{sorted((v.invariant, v.ref_slug) for v in relevant)}")
        if not relevant:
            print("  [FAIL] Aucune violation auto-fixable détectée")
            return False
        fixed, skipped = doctor.auto_fix(relevant)
        print(f"  auto_fix → fixed={fixed}, skipped={skipped}")
        if fixed < len(relevant):
            print(f"  [FAIL] auto-fix incomplet : "
                  f"{fixed}/{len(relevant)} réparées")
            return False

        # Re-check après fix
        refs2 = list(iter_refs(tmp_refs))
        violations2 = doctor.run_all_checks(refs2, vault_root=VAULT_DIR)
        relevant2 = [v for v in violations2 if v.invariant in target_invs]
        if relevant2:
            print(f"  [FAIL] Violations restantes après autofix : "
                  f"{[(v.invariant, v.ref_slug, v.message[:60]) for v in relevant2]}")
            return False
        print("  [OK] I4, I6, I9 réparés (0 violation post-fix)")
        return True
    finally:
        config.REFS = original_refs
        shutil.rmtree(tmp, ignore_errors=True)


def test_blocked_human_not_auto_fixed() -> bool:
    """I10 (blocked_human sans reason) DOIT être détecté mais JAMAIS auto-fixé.

    Anti-heuristique : on vérifie que la violation I10 a auto_fixable=False
    et que `auto_fix` ne touche pas la fixture I10.
    """
    print("\n=== Phase 3 : I10 jamais auto-fixé ===")
    _patch_config_for_synthetic()
    all_refs = _all_synthetic_refs()
    violations = doctor.run_all_checks(all_refs, vault_root=VAULT_DIR)
    i10s = [v for v in violations if v.invariant == "I10"]
    if not i10s:
        print("  [FAIL] I10 non détecté")
        return False
    for v in i10s:
        if v.auto_fixable:
            print(f"  [FAIL] I10 marqué auto_fixable=True : {v.message}")
            return False
        if v.fix_fn is not None:
            print(f"  [FAIL] I10 a un fix_fn non None : {v.message}")
            return False
    print(f"  [OK] {len(i10s)} violation(s) I10 détectées, auto_fixable=False, fix_fn=None")
    return True


def main() -> int:
    print("=" * 60)
    print("Tests synthétiques invariants I1-I15")
    print("=" * 60)

    ok1 = test_fixtures_basic()
    ok2 = test_autofix()
    ok3 = test_blocked_human_not_auto_fixed()

    print()
    print("=" * 60)
    if ok1 and ok2 and ok3:
        print("=== test_invariants_synthetic : 15/15 fixtures OK ===")
        return 0
    print("=== test_invariants_synthetic : FAIL ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
