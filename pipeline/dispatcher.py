"""Dispatcher — décide la prochaine transition pour une ref selon son état courant.

Seules les transitions définies dans la machine d'état canonique
(STATE_MACHINE_DESIGN_2026-05-17.md §3) sont autorisées. Tout autre saut
est un IllegalTransition.
"""
from __future__ import annotations
from dataclasses import dataclass

from .config import TERMINAL_STATES, WAITING_STATES, BLOCKED_PREFIX
from .registry import Ref


class IllegalTransition(Exception):
    """Levée quand un dispatch suggère une transition non-adjacente."""


@dataclass
class Plan:
    """Plan d'action pour une ref : la fonction à appeler + un motif lisible."""
    fn_name: str             # nom de la transition à invoquer dans transitions.py
    reason: str              # une phrase explicative loggable


def plan_for(ref: Ref) -> Plan | None:
    """Retourne le plan d'action pour cette ref, ou None si rien à faire.

    None = état terminal, en attente externe, ou bloqué humain — le worker
    n'a rien à faire dans cette session.
    """
    state = ref.state

    if state in TERMINAL_STATES:
        return None  # terminal — curator a tranché
    if state in WAITING_STATES:
        return None  # awaiting_rtfm_ocr — géré par reactivate-ocr séparément
    if state.startswith(BLOCKED_PREFIX):
        return None  # blocked_human — décision utilisateur

    # Si la ref a un blocked_by non transitoire, on ne re-tente pas la cascade
    # à chaque session. Le blocked_by doit être levé explicitement (curator)
    # avant que le worker re-attaque cette ref.
    blocked_by = ref.frontmatter.get("blocked_by")
    if blocked_by and blocked_by not in ("", None):
        return None

    if state == "candidate":
        return Plan("candidate_to_uid_resolved",
                    "résoudre UID universel avec homonymy guard")
    if state == "uid_resolved":
        return Plan("uid_resolved_to_pdf_acquired",
                    "lancer cascade 9 niveaux depuis première source non tentée")
    if state == "pdf_acquired":
        return Plan("pdf_acquired_dispatch",
                    "probe PDF health + dispatch (page1 / awaiting_ocr / reacq)")
    if state == "needs_reacquisition":
        return Plan("needs_reacquisition_to_uid_resolved",
                    "marquer pour re-cascade depuis source suivante")
    if state == "page1_validated":
        return None  # curator domain — claim_verification + sota_cited_confirmed

    raise IllegalTransition(f"État inconnu pour ref {ref.slug!r}: {state!r}")
