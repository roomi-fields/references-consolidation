"""CLI argparse — `python -m pipeline <subcommand> [options]`."""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter

from .config import REFS, STATE_ORDER, TERMINAL_STATES, WAITING_STATES, BLOCKED_PREFIX
from .registry import iter_refs
from .dispatcher import plan_for, IllegalTransition
from .transitions import REGISTRY as TRANSITIONS, NotImplementedYet
from .journal import append_event, append_blocked
from .linter_wrapper import run_lint
from .doctor import run_doctor_for_cli
from .lock import WorkerLock, LockBusyError
from . import events as events_mod


def cmd_status(args: argparse.Namespace) -> int:
    """Affiche les comptes par état + un échantillon des refs actives."""
    counter: Counter[str] = Counter()
    blocked_kinds: Counter[str] = Counter()
    total = 0
    for ref in iter_refs():
        total += 1
        s = ref.state
        counter[s] += 1
        if s.startswith(BLOCKED_PREFIX):
            blocked_kinds[s] += 1

    print(f"# Registry status — {total} refs")
    print()
    print(f"{'État':<40} {'Count':>6}  {'Catégorie':<20}")
    print("-" * 70)

    def cat(state: str) -> str:
        if state in TERMINAL_STATES:
            return "terminal"
        if state in WAITING_STATES:
            return "waiting"
        if state.startswith(BLOCKED_PREFIX):
            return "blocked_human"
        return "active"

    for state, n in sorted(counter.items(),
                           key=lambda kv: (STATE_ORDER.get(kv[0], 50), -kv[1])):
        print(f"{state:<40} {n:>6}  {cat(state):<20}")

    print()
    active = sum(counter[s] for s in counter if cat(s) == "active")
    waiting = sum(counter[s] for s in counter if cat(s) == "waiting")
    blocked = sum(counter[s] for s in counter if cat(s) == "blocked_human")
    terminal = sum(counter[s] for s in counter if cat(s) == "terminal")
    print(f"Récap : active={active}  waiting={waiting}  blocked_human={blocked}  terminal={terminal}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Lance le linter (lint_registry.py existant) et affiche le rapport."""
    rc, out = run_lint(verbose=True)
    if rc != 0:
        print(f"\n[lint] returncode={rc} — invariants violés", file=sys.stderr)
    return rc


def cmd_run(args: argparse.Namespace) -> int:
    """Boucle principale : pour chaque ref active, dispatch + transition + journal.

    En mode --dry-run, n'effectue aucune mutation.
    """
    n_planned = 0
    n_done = 0
    n_blocked = 0
    n_skip = 0
    n_pending = 0

    for ref in sorted(iter_refs(), key=lambda r: STATE_ORDER.get(r.state, 50)):
        # Filtres
        if args.state and ref.state != args.state:
            continue
        if args.ref and ref.slug != args.ref:
            continue
        if args.cited_in:
            consumers = {c.get("name") for c in ref.cited_in}
            if not set(args.cited_in) & consumers:
                continue
        if args.limit and n_planned >= args.limit:
            break

        try:
            plan = plan_for(ref)
        except IllegalTransition as e:
            print(f"[ILLEGAL] {ref.slug}: {e}", file=sys.stderr)
            append_blocked(ref.slug, ref.state, f"illegal_state:{e}")
            n_blocked += 1
            continue

        if plan is None:
            n_skip += 1
            continue

        n_planned += 1
        if args.dry_run:
            print(f"[plan] {ref.slug:<60} {ref.state:<25} → {plan.fn_name}  # {plan.reason}")
            continue

        fn = TRANSITIONS.get(plan.fn_name)
        if fn is None:
            print(f"[BUG] transition {plan.fn_name!r} absente du registre", file=sys.stderr)
            continue

        from_state = ref.state
        try:
            res = fn(ref)
        except NotImplementedYet as e:
            n_pending += 1
            if args.verbose:
                print(f"[pending] {ref.slug:<60} {from_state:<25} → {plan.fn_name}  ({e})")
            continue
        except Exception as e:
            n_blocked += 1
            append_blocked(ref.slug, from_state, f"worker_crash:{type(e).__name__}:{e}")
            print(f"[CRASH] {ref.slug}: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        if res.succeeded:
            append_event(ref.slug, res.from_state, res.to_state, res.via, res.meta)
            n_done += 1
            print(f"[done] {ref.slug:<60} {res.from_state:<25} → {res.to_state}")
        else:
            append_blocked(ref.slug, from_state, res.blocked_reason or "unknown")
            n_blocked += 1

    print()
    print(f"Récap session : planned={n_planned}  done={n_done}  pending={n_pending}  "
          f"blocked={n_blocked}  skipped_terminal={n_skip}")

    rc_lint = 0
    rc_doctor = 0

    if not args.no_lint and not args.dry_run:
        print()
        print("# Lint final")
        rc_lint, _out = run_lint(verbose=True)
        if rc_lint != 0:
            print(f"[lint] returncode={rc_lint} — invariants R1-R10 violés",
                  file=sys.stderr)

    # Doctor en fin de session (Couche 1) : invariants I1-I15. Jamais --fix auto.
    # Miroir de --no-lint : --no-doctor pour skip.
    if not getattr(args, "no_doctor", False) and not args.dry_run:
        print()
        print("# Doctor final (invariants I1-I15)")
        rc_doctor, out_doctor = run_doctor_for_cli(
            refs=None, apply_fix=False, min_severity="info", as_json=False,
        )
        print(out_doctor)
        if rc_doctor != 0:
            print(f"[doctor] returncode={rc_doctor} — invariants I1-I15 violés",
                  file=sys.stderr)

    return max(rc_lint, rc_doctor)


def cmd_reactivate_ocr(args: argparse.Namespace) -> int:
    """Re-évalue les refs `awaiting_rtfm_ocr` via `rtfm check --path`.

    Boucle dédiée séparée de `run` : on cible explicitement ce state.
    Pour chaque ref :
      - rtfm check --path <pdf_path>
      - dispatch selon verdict (ok / still_pending / missing / anomaly / ocr_failed)
      - mute le frontmatter (last_rtfm_check_at, state si transition, journal append)

    Sortie : récap compté par verdict.
    """
    from .transitions import awaiting_rtfm_ocr_dispatch

    counts = {"converted": 0, "still_pending": 0, "missing_in_index": 0,
              "anomaly": 0, "ocr_failed": 0, "needs_reacq_post_ocr": 0,
              "error": 0}
    total = 0
    verbose = getattr(args, "verbose", False) or not getattr(args, "quiet", False)

    for ref in iter_refs():
        if ref.state != "awaiting_rtfm_ocr":
            continue
        total += 1
        try:
            res = awaiting_rtfm_ocr_dispatch(ref)
        except Exception as e:
            counts["error"] += 1
            append_blocked(ref.slug, ref.state, f"reactivate_ocr_crash:{type(e).__name__}:{e}")
            if verbose:
                print(f"[crash] {ref.slug}: {type(e).__name__}: {e}",
                      file=sys.stderr)
            continue

        if res.to_state == "page1_validated":
            counts["converted"] += 1
            append_event(ref.slug, "awaiting_rtfm_ocr", "page1_validated",
                         res.via, res.meta)
            if verbose:
                print(f"[converted] {ref.slug:<55} → page1_validated  "
                      f"(chunks={res.meta.get('chunks') if res.meta else '?'})")
        elif res.to_state == "needs_reacquisition":
            if res.via == "rtfm_ocr_failed":
                counts["ocr_failed"] += 1
            else:
                counts["needs_reacq_post_ocr"] += 1
            append_event(ref.slug, "awaiting_rtfm_ocr", "needs_reacquisition",
                         res.via, res.meta)
            if verbose:
                print(f"[reacq] {ref.slug:<55} → needs_reacquisition "
                      f"({res.via})")
        else:
            # Pas de transition — still_pending, missing_in_index, anomaly
            via = res.via or "unknown"
            if "still_pending" in via:
                counts["still_pending"] += 1
            elif "missing" in via:
                counts["missing_in_index"] += 1
            elif "anomaly" in via:
                counts["anomaly"] += 1
            append_blocked(ref.slug, "awaiting_rtfm_ocr",
                           res.blocked_reason or via)
            if verbose:
                print(f"[wait] {ref.slug:<55} {via}")

    print()
    print(f"# reactivate-ocr — {total} refs en awaiting_rtfm_ocr scannées")
    for k in ("converted", "still_pending", "missing_in_index", "anomaly",
              "ocr_failed", "needs_reacq_post_ocr", "error"):
        print(f"  {k:<25} {counts[k]:>4}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Lance les checks d'invariants I1-I19 et affiche le rapport.

    Options :
      --fix             : applique les fix_fn auto_fixable (I4 R8, I6 sha
                          recompute, I9 renumber, I5 semi semi → needs_reacquisition)
      --severity X      : filtre min "info" / "warn" / "error" (défaut: info)
      --json            : sortie JSON machine-readable
      --correlate-rtfm  : active Couche 5 — I16/I17/I19 (corrélation RTFM,
                          appels CLI rtfm)
      --check-sha       : active I18 (recompute sha256 sur les PDFs, lent)
    """
    severity = getattr(args, "severity", None) or "info"
    rc, out = run_doctor_for_cli(
        refs=None,
        apply_fix=getattr(args, "fix", False),
        min_severity=severity,
        as_json=getattr(args, "json", False),
        correlate_rtfm=getattr(args, "correlate_rtfm", False),
        check_sha=getattr(args, "check_sha", False),
    )
    print(out)
    if rc != 0:
        print(f"\n[doctor] returncode={rc} — au moins 1 ERROR détecté",
              file=sys.stderr)
    return rc


def cmd_events(args: argparse.Namespace) -> int:
    """Lit le journal JSONL et affiche les transitions filtrées.

    Filtres :
      --since DATE       (ISO date, journée UTC inclusive)
      --to STATE         (état cible exact dans la transition)
      --cited-in SOTA    (intersection avec refs dont cited_in[].name == SOTA)
      --json             (sortie machine-readable)
    """
    since_date = None
    if args.since:
        try:
            since_date = events_mod._parse_iso_date(args.since)
        except ValueError:
            print(f"[events] --since invalide : {args.since!r} "
                  f"(attendu YYYY-MM-DD)", file=sys.stderr)
            return 2

    raw = events_mod.iter_events(since=since_date)
    filtered = events_mod.filter_events(
        raw,
        to_state=args.to,
        cited_in=args.cited_in,
    )

    if args.json:
        print(json.dumps(filtered, ensure_ascii=False, indent=2))
    else:
        print(events_mod.render_text(
            filtered, since_date, args.to, args.cited_in,
        ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="Worker FSM stricte pour pipeline SOTA — voir plans/B_worker_FSM_pipeline.md",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pst = sub.add_parser("status", help="Compte les refs par état")
    pst.set_defaults(func=cmd_status)

    pln = sub.add_parser("lint", help="Lance lint_registry.py (invariants R1-R10)")
    pln.set_defaults(func=cmd_lint)

    prn = sub.add_parser("run", help="Pousse les refs actives vers leur prochain état")
    prn.add_argument("--state", help="Filtre : ne traite qu'un état particulier")
    prn.add_argument("--ref", help="Filtre : ne traite qu'une ref (par slug)")
    prn.add_argument("--cited-in", action="append", default=[],
                     help="Filtre OR : refs citées par ce SOTA/Paper (répétable)")
    prn.add_argument("--limit", type=int, default=0,
                     help="Max refs traitées (0 = pas de limite)")
    prn.add_argument("--dry-run", action="store_true",
                     help="Affiche les plans sans muter")
    prn.add_argument("--no-lint", action="store_true",
                     help="Skip le lint final")
    prn.add_argument("--no-doctor", action="store_true",
                     help="Skip les invariants doctor I1-I15 en fin de run")
    prn.add_argument("-v", "--verbose", action="store_true")
    prn.set_defaults(func=cmd_run)

    pra = sub.add_parser("reactivate-ocr",
                         help="Re-évalue les awaiting_rtfm_ocr via rtfm check")
    pra.add_argument("--quiet", action="store_true")
    pra.set_defaults(func=cmd_reactivate_ocr)

    pdo = sub.add_parser("doctor",
                         help="Lance les invariants I1-I19 (sur-couche worker)")
    pdo.add_argument("--fix", action="store_true",
                     help="Applique les fix_fn auto-fixable (I4, I6, I9, I5 semi)")
    pdo.add_argument("--severity", choices=("info", "warn", "error"),
                     default="info",
                     help="Filtre min de sévérité (défaut: info = tout afficher)")
    pdo.add_argument("--json", action="store_true",
                     help="Sortie JSON machine-readable")
    pdo.add_argument("--correlate-rtfm", action="store_true",
                     dest="correlate_rtfm",
                     help="Active Couche 5 — I16/I17/I19 (corrélation RTFM, "
                          "appel `rtfm failed` CLI)")
    pdo.add_argument("--check-sha", action="store_true",
                     dest="check_sha",
                     help="Active I18 — recompute sha256 sur tous les PDFs "
                          "concernés (lent, opt-in)")
    pdo.set_defaults(func=cmd_doctor)

    pev = sub.add_parser("events",
                         help="Lit le journal JSONL filtré (Couche 3)")
    pev.add_argument("--since",
                     help="Date ISO inclusive (YYYY-MM-DD), filtre par jour UTC")
    pev.add_argument("--to", dest="to",
                     help="État cible filtré (ex: page1_validated)")
    pev.add_argument("--cited-in", dest="cited_in",
                     help="Intersection avec refs dont cited_in[].name == valeur")
    pev.add_argument("--json", action="store_true",
                     help="Sortie machine-readable JSON")
    pev.set_defaults(func=cmd_events)

    return p


# Sous-commandes qui mutent le registre — protégées par WorkerLock pour
# éviter 2 sessions concurrentes. Les read-only (status, lint, doctor, events)
# ne sont PAS wrappées.
_MUTATING_CMDS = {"run", "reactivate-ocr"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in _MUTATING_CMDS:
        try:
            with WorkerLock():
                return args.func(args)
        except LockBusyError as e:
            print(f"[lock] {e}", file=sys.stderr)
            return 2
    return args.func(args)
