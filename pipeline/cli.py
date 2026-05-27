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


def _run_one_pass(args: argparse.Namespace) -> dict:
    """Une passe de transitions sur les refs actives. Retourne les compteurs.

    Mêmes filtres et logique que `cmd_run`, mais isolé pour permettre la
    réexécution en boucle (mode `--loop`).
    """
    n_planned = 0
    n_done = 0
    n_blocked = 0
    n_skip = 0
    n_pending = 0

    for ref in sorted(iter_refs(), key=lambda r: STATE_ORDER.get(r.state, 50)):
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

    return {"planned": n_planned, "done": n_done, "pending": n_pending,
            "blocked": n_blocked, "skipped_terminal": n_skip}


def _set_memory_limit(max_gb: float = 1.5) -> None:
    """Borne la RAM virtuelle du process pour éviter de freezer la machine.

    Si un téléchargement géant ou une fuite mémoire pousse au-delà,
    Python lèvera MemoryError (capturé en CRASH) au lieu d'épuiser
    la mémoire système.
    """
    try:
        import resource
        max_bytes = int(max_gb * 1024 * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
    except (ImportError, ValueError, OSError):
        pass


def cmd_run(args: argparse.Namespace) -> int:
    """Boucle principale. Une passe par défaut, ou jusqu'à épuisement avec --loop.

    En mode --dry-run, n'effectue aucune mutation.
    En mode --loop, itère tant que au moins une transition est faite (done > 0)
    ou jusqu'à `--max-iterations` (default 10).
    """
    _set_memory_limit(1.5)
    loop = getattr(args, "loop", False)
    max_iter = getattr(args, "max_iterations", 10)

    if not loop:
        stats = _run_one_pass(args)
        print()
        print(f"Récap session : planned={stats['planned']}  done={stats['done']}  "
              f"pending={stats['pending']}  blocked={stats['blocked']}  "
              f"skipped_terminal={stats['skipped_terminal']}")
    else:
        total = {"planned": 0, "done": 0, "pending": 0, "blocked": 0,
                 "skipped_terminal": 0}
        iteration = 0
        while iteration < max_iter:
            iteration += 1
            print(f"\n# Loop iteration {iteration}/{max_iter}")
            stats = _run_one_pass(args)
            for k in total:
                total[k] += stats[k]
            print(f"  → iteration {iteration} : done={stats['done']}  "
                  f"blocked={stats['blocked']}  pending={stats['pending']}")
            if stats["done"] == 0:
                print(f"\n# Loop terminé : 0 transition à l'itération {iteration} "
                      f"→ épuisement atteint.")
                break
        else:
            print(f"\n# Loop arrêté : max_iterations={max_iter} atteint avec "
                  f"des transitions encore en cours. Relance `pipeline run --loop` "
                  f"pour continuer.")
        print()
        print(f"Récap CUMULÉ ({iteration} itération(s)) : "
              f"planned={total['planned']}  done={total['done']}  "
              f"pending={total['pending']}  blocked={total['blocked']}  "
              f"skipped_terminal={total['skipped_terminal']}")

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


def cmd_arbitrate(args: argparse.Namespace) -> int:
    """Décision humaine pour refs problématiques (cascade épuisée, etc.).

    3 décisions :
      - retract  : ref est un artefact, ne devrait pas exister.
                   state → `retracted` (terminal).
      - blocked  : ref existe mais inaccessible (paywall, hors-ligne).
                   state → `blocked_human:cascade_exhausted`.
      - investigate : besoin de corriger frontmatter (auteur, titre, doi)
                   puis relancer cascade. Retire `blocked_by` et appose
                   un flag `human_investigate`.

    Refuse les décisions sur refs déjà terminales (retracted ou validées).
    """
    from .registry import load_ref, save_ref, append_state_history
    from pathlib import Path

    slug = args.slug
    path = REFS / f"{slug}.md"
    if not path.exists():
        print(f"[ERR] ref introuvable : {slug}", file=sys.stderr)
        return 2
    ref = load_ref(path)
    if ref is None:
        print(f"[ERR] ref illisible : {slug}", file=sys.stderr)
        return 2

    if ref.state in ("retracted", "sota_cited_confirmed"):
        print(f"[NOOP] {slug} déjà terminal ({ref.state})", file=sys.stderr)
        return 1

    decision = args.decision
    reason = (args.reason or "").strip() or "manual_arbitration"
    from_state = ref.state

    if decision == "retract":
        append_state_history(ref, "retracted", by="human_arbitration",
                             meta={"reason": reason})
        ref.frontmatter["retracted_reason"] = reason
        from datetime import datetime, timezone
        ref.frontmatter["retracted_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    elif decision == "blocked":
        new_state = "blocked_human:cascade_exhausted"
        append_state_history(ref, new_state, by="human_arbitration",
                             meta={"reason": reason})
        ref.frontmatter["blocked_reason"] = reason
    elif decision == "investigate":
        flags = ref.frontmatter.setdefault("doctor_flags", [])
        flags.append(f"human_investigate:{reason}")
        ref.frontmatter.pop("blocked_by", None)
        # Pas de mutation de state. La transition normale reprendra.
    elif decision == "unblock":
        # Repasse la ref en uid_resolved pour relancer la cascade.
        # Utile quand une nouvelle source est dispo OU si l'utilisateur
        # veut retenter après correction frontmatter / réseau / proxy.
        if from_state not in ("candidate", "uid_resolved",
                              "needs_reacquisition"):
            target_state = "uid_resolved"
        else:
            target_state = from_state
        append_state_history(ref, target_state, by="human_arbitration",
                             meta={"reason": reason, "via": "unblock"})
        ref.frontmatter.pop("blocked_by", None)
        ref.frontmatter.pop("blocked_reason", None)
    elif decision == "reject-pdf":
        # L'utilisateur a identifié que le PDF acquis n'est pas la bonne
        # source (TOC au lieu du paper, mauvaise version, etc.). On :
        # 1. Ajoute le sha actuel à rejected_sha256 (anti-rebouclage)
        # 2. Bouge le PDF en quarantaine
        # 3. Repasse en needs_reacquisition pour relancer la cascade
        current_sha = ref.frontmatter.get("pdf_sha256")
        current_pdf = ref.frontmatter.get("pdf_path")
        if current_sha:
            rejected = ref.frontmatter.setdefault("rejected_sha256", [])
            if current_sha not in rejected:
                rejected.append(current_sha)
        # Quarantine le PDF (déplace hors du dossier Sources)
        quarantined = False
        if current_pdf:
            from .config import SOURCES
            from .cascade import QUARANTINE
            import shutil
            src = SOURCES / current_pdf
            if src.exists():
                QUARANTINE.mkdir(parents=True, exist_ok=True)
                qpath = QUARANTINE / f"{slug}_human_rejected_{src.name}"
                try:
                    shutil.move(str(src), str(qpath))
                    quarantined = True
                except OSError as e:
                    print(f"[WARN] quarantine échouée : {e}", file=sys.stderr)
        # Reset des champs PDF + state
        ref.frontmatter.pop("pdf_path", None)
        ref.frontmatter.pop("pdf_sha256", None)
        flags = ref.frontmatter.setdefault("doctor_flags", [])
        flags.append(f"human_rejected_pdf:{reason}")
        append_state_history(ref, "needs_reacquisition",
                             by="human_arbitration",
                             meta={"reason": reason,
                                   "via": "reject_pdf",
                                   "quarantined": quarantined,
                                   "rejected_sha_added": bool(current_sha)})
    else:
        print(f"[ERR] décision inconnue : {decision}", file=sys.stderr)
        return 2

    save_ref(ref)
    append_event(slug, from_state, ref.frontmatter["state"],
                 f"arbitrate:{decision}", {"reason": reason})
    print(f"[ok] {slug}  {from_state} → {ref.frontmatter['state']}  "
          f"({decision}: {reason[:50]})")
    return 0


def cmd_resolve_textbooks(args: argparse.Namespace) -> int:
    """Pour les refs textbooks ingérées sans year/title (slugs
    `_0000_untitled` ou `_untitled`), liste les candidates ou applique
    des décisions JSON (fusion, complétion, blocked).

    Modes :
      - `--list` : JSON sur stdout des refs à résoudre, avec siblings
        (refs même lastname) pour aider à la fusion
      - `--apply-from <decisions.json>` : applique les décisions

    Format decisions.json :
      [
        {"slug": "hopcroft_0000_untitled", "action": "merge_into",
         "target_slug": "hopcroft_2001_introduction"},
        {"slug": "sipser_0000_untitled", "action": "complete",
         "year": "2012", "title": "Introduction to the Theory of Computation"},
        {"slug": "wolper_0000_untitled", "action": "blocked",
         "reason": "textbook_unidentified"}
      ]
    """
    from .registry import load_ref, save_ref, append_state_history
    from pathlib import Path
    from collections import defaultdict
    from datetime import datetime, timezone

    if getattr(args, "list_candidates", False):
        candidates = []
        by_lastname = defaultdict(list)
        for ref in iter_refs():
            fm = ref.frontmatter
            year = str(fm.get("year") or "")
            title = fm.get("title") or ""
            slug = ref.slug
            is_candidate = (
                slug.endswith("_untitled")
                or year in ("", "0000", "nd", "None")
                or not title
            )
            if is_candidate and fm.get("state") in (
                "candidate", "page1_validated", "uid_resolved"
            ):
                candidates.append({
                    "slug": slug,
                    "author": fm.get("author") or "",
                    "year": year,
                    "title": title,
                    "state": fm.get("state"),
                    "ingest_source": fm.get("ingest_source") or "",
                    "pdf_path": fm.get("pdf_path") or "",
                })
            lname = slug.split("_")[0]
            by_lastname[lname].append({
                "slug": slug,
                "year": year,
                "title": title[:80],
                "state": fm.get("state"),
                "has_pdf": bool(fm.get("pdf_path")),
            })
        for cand in candidates:
            lname = cand["slug"].split("_")[0]
            cand["siblings"] = [
                s for s in by_lastname.get(lname, [])
                if s["slug"] != cand["slug"]
            ]
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0

    apply_from = getattr(args, "apply_from", None)
    if not apply_from:
        print("[ERR] Mode inconnu. Utilise --list ou --apply-from <path.json>",
              file=sys.stderr)
        return 2

    json_path = Path(apply_from)
    if not json_path.exists():
        print(f"[ERR] decisions JSON introuvable : {json_path}",
              file=sys.stderr)
        return 2
    decisions = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(decisions, list):
        print("[ERR] decisions.json doit être une liste", file=sys.stderr)
        return 2

    n_merged = n_completed = n_blocked = n_err = 0
    for d in decisions:
        slug = d.get("slug")
        action = d.get("action")
        if not slug or not action:
            n_err += 1
            continue
        ref_path = REFS / f"{slug}.md"
        if not ref_path.exists():
            print(f"[skip] {slug} introuvable", file=sys.stderr)
            n_err += 1
            continue
        ref = load_ref(ref_path)
        if action == "merge_into":
            target = d.get("target_slug")
            if not target or not (REFS / f"{target}.md").exists():
                print(f"[ERR] target {target} introuvable pour {slug}",
                      file=sys.stderr)
                n_err += 1
                continue
            target_ref = load_ref(REFS / f"{target}.md")
            # Transfert pdf_path éventuel
            if (ref.frontmatter.get("pdf_path")
                    and not target_ref.frontmatter.get("pdf_path")):
                target_ref.frontmatter["pdf_path"] = ref.frontmatter["pdf_path"]
                if ref.frontmatter.get("pdf_sha256"):
                    target_ref.frontmatter["pdf_sha256"] = ref.frontmatter["pdf_sha256"]
                save_ref(target_ref)
            # Marque la ref source comme fusionnée (retracted)
            append_state_history(ref, "retracted", by="resolve_textbooks",
                                 meta={"merged_into": target,
                                       "reason": "duplicate_textbook"})
            ref.frontmatter["retracted_reason"] = f"merged_into:{target}"
            ref.frontmatter["retracted_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            save_ref(ref)
            append_event(slug, "candidate", "retracted",
                         "resolve_textbooks:merge", {"merged_into": target})
            n_merged += 1
            print(f"[merge] {slug} → {target}")
        elif action == "complete":
            year = d.get("year")
            title = d.get("title")
            venue = d.get("venue")
            if year:
                ref.frontmatter["year"] = str(year)
            if title:
                ref.frontmatter["title"] = title
            if venue:
                ref.frontmatter["venue"] = venue
            from .ingest import _make_slug
            new_slug = _make_slug(
                ref.frontmatter.get("author") or "",
                str(ref.frontmatter.get("year") or ""),
                ref.frontmatter.get("title") or "",
            )
            i = 2
            while (REFS / f"{new_slug}.md").exists() and new_slug != slug:
                new_slug = f"{new_slug}_{i}"
                i += 1
            ref.frontmatter["slug"] = new_slug
            save_ref(ref)
            if new_slug != slug:
                (REFS / f"{slug}.md").rename(REFS / f"{new_slug}.md")
            n_completed += 1
            t_short = (title or "")[:40]
            print(f"[complete] {slug} → {new_slug} (year={year}, title={t_short})")
        elif action == "blocked":
            reason = d.get("reason") or "textbook_unidentified"
            append_state_history(ref, "blocked_human:textbook_unidentified",
                                 by="resolve_textbooks",
                                 meta={"reason": reason})
            ref.frontmatter["blocked_reason"] = reason
            save_ref(ref)
            n_blocked += 1
            print(f"[blocked] {slug}  reason={reason}")
        else:
            print(f"[ERR] action inconnue : {action!r} pour {slug}",
                  file=sys.stderr)
            n_err += 1

    print(f"\nResolved: merged={n_merged}  completed={n_completed}  "
          f"blocked={n_blocked}  errors={n_err}")
    return 1 if n_err else 0


def cmd_search(args: argparse.Namespace) -> int:
    """Recherche dans le registre validé (`page1_validated` ou
    `sota_cited_confirmed` selon `--include-pending`).

    Filtre par match insensible à la casse sur auteur + titre + year.
    Sortie : liste compacte avec slug, auteur, année, titre, état.
    """
    query = (args.query or "").strip().lower()
    if not query:
        print("[ERR] Query vide. Usage : pipeline search <terme>",
              file=sys.stderr)
        return 2

    include_pending = bool(getattr(args, "include_pending", False))
    valid_states = {"sota_cited_confirmed"}
    if include_pending:
        valid_states.add("page1_validated")

    matches = []
    for ref in iter_refs():
        if ref.state not in valid_states:
            continue
        fm = ref.frontmatter
        haystack = " ".join(str(fm.get(k) or "") for k in
                            ("author", "title", "year"))
        haystack += " " + ref.slug
        if query in haystack.lower():
            matches.append(ref)

    limit = getattr(args, "limit", 0) or 50
    matches = matches[:limit]
    if not matches:
        print(f"Aucune ref ne matche {query!r} parmi les refs validées.")
        return 0
    print(f"{len(matches)} refs validées match {query!r} :\n")
    for ref in matches:
        fm = ref.frontmatter
        author = (fm.get("author") or "?")[:25]
        year = fm.get("year") or "?"
        title = (fm.get("title") or "")[:60]
        print(f"  [{ref.state:<25}] {author:<25} ({year}) — {title}")
        print(f"     {ref.slug}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest les citations d'un SOTA (ou de tous les SOTAs) dans le registre.

    Convertit les citations en texte libre en wikilinks `[[slug]]` après
    avoir créé les refs correspondantes dans le registre.

    Modes :
      - `pipeline ingest --init-git` : initialise git dans le vault
      - `pipeline ingest <sota> --extract-only` : liste les sections
        bibliographiques candidates (pour orchestration par Claude)
      - `pipeline ingest <sota> --citations-json <path>` : applique
        l'ingestion avec un JSON déjà parsé par le sub-agent
      - `pipeline ingest --all --dry-run` : scan tous les SOTAs, montre
        ce qui serait ingéré, ne mute rien
    """
    from . import ingest as ingest_mod
    from adapters import get_adapter
    from .config import VAULT
    from pathlib import Path

    # Mode 1 : init git
    if getattr(args, "init_git", False):
        return 0 if ingest_mod.init_git_vault(VAULT) else 1

    # Mode 2 : extract-only (liste les sections)
    if getattr(args, "extract_only", False):
        sota = Path(args.sota)
        if not sota.exists():
            print(f"[ERR] SOTA introuvable : {sota}", file=sys.stderr)
            return 2
        adapter = get_adapter()
        sections = adapter.extract_bibliography_sections(sota)
        out = [
            {
                "header": s.header,
                "is_excluded": s.is_excluded,
                "start_offset": s.start_offset,
                "end_offset": s.end_offset,
                "raw_text": s.raw_text,
            }
            for s in sections
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # Mode 3 : ingest avec JSON de citations déjà parsées
    if args.citations_json:
        sota = Path(args.sota)
        json_path = Path(args.citations_json)
        if not sota.exists():
            print(f"[ERR] SOTA introuvable : {sota}", file=sys.stderr)
            return 2
        if not json_path.exists():
            print(f"[ERR] JSON citations introuvable : {json_path}",
                  file=sys.stderr)
            return 2
        apply = bool(getattr(args, "apply", False))
        if apply:
            if not ingest_mod._ensure_git_backup(
                VAULT, f"paper-trail ingest before modifying {sota.name}"
            ):
                print("[ERR] backup git impossible. Use --init-git d'abord "
                      "ou skip --apply pour dry-run.", file=sys.stderr)
                return 2
        result = ingest_mod.ingest_citations_from_json(
            sota, json_path, apply=apply
        )
        # Mode JSON structuré : pour scripting et fixtures (H6)
        if getattr(args, "json_output", False):
            print(json.dumps(result.to_metrics_dict(), ensure_ascii=False,
                             indent=2))
            return 1 if result.errors else 0
        # Mode texte humain
        m = result.to_metrics_dict()
        print(f"\n=== Ingest result : {sota.name} ===")
        print(f"  apply={apply}  duration={m['duration_seconds']}s")
        print(f"  citations    : {m['citations_total']} "
              f"(doi_resolved {m['doi_resolved']}, skipped_low "
              f"{m['skipped_low_confidence']})")
        print(f"  new_refs     : {m['new_refs_created']}")
        for s in result.new_refs[:20]:
            print(f"    + {s}")
        print(f"  reused_refs  : {m['reused_refs']} "
              f"(by_doi {m['matched_by_doi']}, by_fuzzy {m['matched_by_fuzzy']})")
        for s in result.reused_refs[:20]:
            print(f"    = {s}")
        print(f"  substitutions: {m['wikilinks_substituted']}")
        if m["orphan_pdfs_found"]:
            print(f"  orphan PDFs  : {m['orphan_pdfs_found']} "
                  f"({m['page1_validated']} validated page 1)")
        if result.errors:
            print(f"  errors : {len(result.errors)}")
            for e in result.errors[:5]:
                print(f"    ! {e}")
        return 1 if result.errors else 0

    # Mode 4 : --all (batch sur tout le vault, dry-run/apply)
    if getattr(args, "all_sotas", False):
        adapter = get_adapter()
        sotas = list(adapter.find_sotas())
        print(f"Scan de {len(sotas)} SOTAs pour sections bibliographiques...")
        total_sections = 0
        sotas_with_sections = 0
        for sota in sotas:
            sections = adapter.extract_bibliography_sections(sota)
            non_excl = [s for s in sections if not s.is_excluded]
            if non_excl:
                sotas_with_sections += 1
                total_sections += len(non_excl)
                print(f"  {sota.stem:<60} {len(non_excl)} section(s)")
        print(f"\n→ {sotas_with_sections}/{len(sotas)} SOTAs avec sections "
              f"candidates ({total_sections} sections au total)")
        print("\nProchaine étape : orchestrer le sub-agent citation-parser "
              "via /paper-trail:ingest-all (slash command) qui invoquera "
              "le sub-agent pour parser chaque section, puis appellera "
              "cette CLI avec le JSON résultat.")
        return 0

    print("[ERR] Mode inconnu. Voir `pipeline ingest --help`.", file=sys.stderr)
    return 2


def cmd_retract_uncited(args: argparse.Namespace) -> int:
    """Retract en lot toutes les refs actives non citées hors registre INDEX.

    Une ref `candidate`, `uid_resolved` ou `awaiting_rtfm_ocr` qui n'est
    citée dans aucune SOTA ni article du vault n'a aucun impact si on la
    retract. Empiriquement c'est 95% des cas problématiques.

    Mode dry-run par défaut : montre la liste et compte, ne mute rien.
    Avec --apply : exécute les retract avec une raison standard.
    """
    from .registry import load_ref, save_ref, append_state_history
    from tools.review_problems import build_citations_index
    from datetime import datetime, timezone

    active_states = {"candidate", "uid_resolved", "awaiting_rtfm_ocr"}
    print("Scan du vault pour citations...", file=sys.stderr)
    citations_idx = build_citations_index()

    candidates = []
    for ref in iter_refs():
        if ref.state not in active_states:
            continue
        cites = citations_idx.get(ref.slug, [])
        real_cites = [c for c in cites if "INDEX.md" not in str(c[0])]
        if real_cites:
            continue
        candidates.append(ref)

    print(f"\n{len(candidates)} refs actives non citées hors INDEX")
    for ref in candidates:
        author = ref.frontmatter.get("author") or "?"
        year = ref.frontmatter.get("year") or "?"
        print(f"  [{ref.state:<20}] {ref.slug:<50}  {author} ({year})")

    if not candidates:
        return 0

    if not getattr(args, "apply", False):
        print(f"\nDry-run (utilise --apply pour retract ces {len(candidates)} refs)")
        return 0

    reason = (getattr(args, "reason", None) or
              "auto-retract: not cited in any SOTA or article (only in registry INDEX)")
    n_ok = 0
    n_err = 0
    for ref in candidates:
        from_state = ref.state
        try:
            append_state_history(ref, "retracted", by="auto_retract_uncited",
                                 meta={"reason": reason})
            ref.frontmatter["retracted_reason"] = reason
            ref.frontmatter["retracted_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            save_ref(ref)
            append_event(ref.slug, from_state, "retracted",
                         "retract_uncited", {"reason": reason})
            n_ok += 1
        except Exception as e:
            print(f"[ERR] {ref.slug}: {type(e).__name__}: {e}", file=sys.stderr)
            n_err += 1
    print(f"\nRetracted: {n_ok}/{len(candidates)} (errors: {n_err})")
    return 1 if n_err else 0


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
    prn.add_argument("--loop", action="store_true",
                     help="Boucle jusqu'à épuisement : re-run tant que des "
                          "transitions sont possibles (max --max-iterations).")
    prn.add_argument("--max-iterations", type=int, default=10,
                     help="Plafond d'itérations en mode --loop (défaut 10).")
    prn.set_defaults(func=cmd_run)

    pra = sub.add_parser("reactivate-ocr",
                         help="Re-évalue les awaiting_rtfm_ocr via rtfm check")
    pra.add_argument("--quiet", action="store_true")
    pra.set_defaults(func=cmd_reactivate_ocr)

    prt = sub.add_parser("resolve-textbooks",
                         help="Résout les refs textbook ingérées sans year/title")
    prt.add_argument("--list", dest="list_candidates", action="store_true",
                     help="Liste sur stdout les refs candidates en JSON")
    prt.add_argument("--apply-from", dest="apply_from",
                     help="Path d'un JSON de décisions à appliquer")
    prt.set_defaults(func=cmd_resolve_textbooks)

    psr = sub.add_parser("search",
                         help="Recherche dans le registre validé")
    psr.add_argument("query", help="Terme à chercher (auteur, titre, année, slug)")
    psr.add_argument("--include-pending", action="store_true",
                     help="Inclure aussi les refs page1_validated (pas seulement sota_cited_confirmed)")
    psr.add_argument("--limit", type=int, default=50, help="Nb max de résultats")
    psr.set_defaults(func=cmd_search)

    pin = sub.add_parser("ingest",
                         help="Ingest citations d'un SOTA dans le registre")
    pin.add_argument("sota", nargs="?", default=None,
                     help="Chemin du SOTA à ingérer (sauf --init-git ou --all)")
    pin.add_argument("--init-git", action="store_true",
                     help="Initialise git dans le vault (1ère fois)")
    pin.add_argument("--extract-only", action="store_true",
                     help="Liste les sections bibliographiques en JSON sur stdout, n'ingère rien")
    pin.add_argument("--citations-json",
                     help="Chemin d'un JSON de citations déjà parsées par le sub-agent")
    pin.add_argument("--apply", action="store_true",
                     help="Applique l'ingestion (crée refs + substitue). Sans : dry-run.")
    pin.add_argument("--all", dest="all_sotas", action="store_true",
                     help="Scan tous les SOTAs du vault (dry-run par défaut)")
    pin.add_argument("--json", dest="json_output", action="store_true",
                     help="Sortie JSON structurée (métriques) au lieu du récap "
                          "humain. Pour scripting et fixtures de test.")
    pin.set_defaults(func=cmd_ingest)

    pru = sub.add_parser("retract-uncited",
                         help="Retract en lot les refs actives non citées hors INDEX")
    pru.add_argument("--apply", action="store_true",
                     help="Exécute les retract (défaut : dry-run)")
    pru.add_argument("--reason", default=None,
                     help="Raison personnalisée pour le journal")
    pru.set_defaults(func=cmd_retract_uncited)

    par = sub.add_parser("arbitrate",
                         help="Décision humaine sur une ref problématique")
    par.add_argument("slug", help="Slug de la ref à arbitrer")
    par.add_argument("--decision", required=True,
                     choices=("retract", "blocked", "investigate",
                              "unblock", "reject-pdf"),
                     help="retract: artefact; blocked: paywall/inaccessible; "
                          "investigate: corriger frontmatter puis relancer; "
                          "unblock: lever blocked_by et retenter cascade; "
                          "reject-pdf: mauvaise source identifiée (TOC, "
                          "mauvaise version) — quarantine + relance cascade")
    par.add_argument("--reason", default="",
                     help="Phrase courte justifiant la décision (loggée)")
    par.set_defaults(func=cmd_arbitrate)

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
