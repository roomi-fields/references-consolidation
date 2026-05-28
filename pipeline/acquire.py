"""Module acquire — cascade PDF ciblée sur les refs d'un SOTA.

Phase 6 du plan refonte INGEST. Wrapper léger autour de la boucle
existante `plan_for + transitions worker B`, filtré aux slugs cibles
d'un SOTA donné.

Différence avec `pipeline run` :
- `run` itère sur TOUTES les refs actives du registre
- `acquire <sota>` itère uniquement sur les refs CITÉES (wikilinks
  existants) + à créer (depuis IdentifyReport) pour ce SOTA
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AcquireBatch:
    """Résultat d'un acquire sur un SOTA."""
    sota_path: Path
    target_slugs: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    skipped_terminal: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "sota": str(self.sota_path),
            "dry_run": self.dry_run,
            "n_targets": len(self.target_slugs),
            "n_succeeded": len(self.succeeded),
            "n_pending": len(self.pending),
            "n_blocked": len(self.blocked),
            "n_skipped_terminal": len(self.skipped_terminal),
            "target_slugs": self.target_slugs,
            "succeeded": self.succeeded,
            "pending": self.pending,
            "blocked": self.blocked,
            "errors": self.errors,
        }


def slugs_cited_by_sota(
    sota_path: Path,
    identify_report=None,
) -> list[str]:
    """Renvoie les slugs liés au SOTA.

    Source :
    1. Wikilinks existants dans le SOTA (parse_citations + alias-aware)
    2. Slugs à créer / matchés depuis IdentifyReport si fourni

    Déduplique. Ignore les paths techniques et les slugs non-bibliographiques.
    """
    from .sota_sync import _WIKILINK_PATTERN
    seen: set[str] = set()
    slugs: list[str] = []

    # 1. Wikilinks existants
    try:
        text = sota_path.read_text(encoding="utf-8")
    except OSError:
        text = ""

    for m in _WIKILINK_PATTERN.finditer(text):
        target = m.group(1)
        alias = m.group(2)
        # Skip paths techniques
        if any(target.startswith(p) for p in (
                "20_ATLAS/", "30_DEV/", "00_MANAGEMENT/")):
            continue
        if target.endswith(".canvas"):
            continue
        base = alias if alias else Path(target).stem
        base_lower = base.lower()
        # Skip slugs non-bib (TitleCase avec underscores)
        if not _looks_like_bib_slug(base):
            continue
        if base_lower not in seen:
            seen.add(base_lower)
            slugs.append(base_lower)

    # 2. Slugs depuis IdentifyReport (matched_slug ou would_create_slug)
    if identify_report is not None:
        for m in identify_report.mentions:
            if m.action_recommended == "skipped_low_confidence":
                continue
            slug = m.matched_slug or m.would_create_slug
            if slug and slug.lower() not in seen:
                seen.add(slug.lower())
                slugs.append(slug.lower())

    return slugs


def _looks_like_bib_slug(base: str) -> bool:
    """Heuristique : un slug bibliographique a la forme lastname_YYYY_word
    ou lastname_0000_word (zero-year), tout lowercase."""
    import re as _re
    if not base:
        return False
    # Tolère les variantes : `_` séparateurs, pas de tiret seul
    # Bib: lowercase + au moins un underscore
    if "_" not in base:
        return False
    # Si tout en lowercase avec underscores : probable bib
    if base == base.lower() and "_" in base:
        return True
    return False


def run_acquire_for_sota(
    sota_path: Path,
    target_slugs: list[str],
    apply: bool = False,
    max_iter_per_slug: int = 5,
) -> AcquireBatch:
    """Pour chaque slug cible, fait avancer la ref dans la FSM (cascade)
    jusqu'à terminal ou max_iter_per_slug.

    En mode dry_run, calcule le plan suivant sans l'exécuter.
    """
    from .registry import load_ref
    from .config import REFS
    from .dispatcher import plan_for, IllegalTransition
    from .transitions import REGISTRY as TRANSITIONS, NotImplementedYet

    batch = AcquireBatch(
        sota_path=sota_path,
        target_slugs=list(target_slugs),
        dry_run=not apply,
    )

    for slug in target_slugs:
        ref_path = REFS / f"{slug}.md"
        if not ref_path.exists():
            batch.errors.append(f"ref absente : {slug}")
            continue

        # Boucle de transitions jusqu'à terminal ou plus de plan
        for _ in range(max_iter_per_slug):
            try:
                ref = load_ref(ref_path)
            except Exception as e:
                batch.errors.append(f"load {slug}: {e}")
                break
            if ref is None:
                batch.errors.append(f"ref invalide : {slug}")
                break

            # Skip si déjà terminal
            if ref.state in ("page1_validated", "sota_cited_confirmed",
                             "retracted"):
                if ref.state == "page1_validated":
                    batch.succeeded.append(slug)
                else:
                    batch.skipped_terminal.append(slug)
                break

            try:
                plan = plan_for(ref)
            except IllegalTransition:
                batch.blocked.append(slug)
                break

            if plan is None:
                batch.skipped_terminal.append(slug)
                break

            if not apply:
                # Dry-run : on note juste qu'on aurait fait quelque chose
                batch.pending.append(slug)
                break

            fn = TRANSITIONS.get(plan.fn_name)
            if fn is None:
                batch.errors.append(
                    f"{slug}: transition {plan.fn_name!r} absente"
                )
                break

            try:
                res = fn(ref)
            except NotImplementedYet:
                batch.pending.append(slug)
                break
            except Exception as e:
                batch.errors.append(
                    f"{slug}: crash {type(e).__name__}: {e}"
                )
                batch.blocked.append(slug)
                break

            if not res.succeeded:
                batch.blocked.append(slug)
                break
            # Else : continue boucle, le state a changé

    return batch
