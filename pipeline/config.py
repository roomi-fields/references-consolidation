"""Chemins et constantes."""
from pathlib import Path
import sys

VAULT = Path("/mnt/d/Obsidian/Articles/Projets/Ontologie musicale")
SOURCES = VAULT / "10_SOURCES"
REGISTRY = SOURCES / "_registry"
REFS = REGISTRY / "refs"
JOURNAL = REGISTRY / "_journal"
QUARANTINE = REGISTRY / "_quarantine"

# Helpers locaux au plugin (lib/ à la racine du repo).
# P0 refactor : helpers copiés depuis source-collector pour self-containment.
LIB_PATH = Path(__file__).parent.parent / "lib"
TOOLS = REGISTRY / "tools"

# DB RTFM du projet doctoral (utilisée par Couche 5 — corrélation des échecs).
# Hardcodée comme VAULT : ce repo ne tourne que sur la machine du doctorant.
# (à refactorer en env var en P1)
RTFM_DB = Path.home() / "dev/musicology-phd/.rtfm/library.db"

# Insère lib/ dans sys.path pour que `import validate_pdf_content`,
# `import s2_resolver`, etc. marchent (ces helpers sont sans package
# parent, ils s'importent au niveau racine).
if str(LIB_PATH) not in sys.path:
    sys.path.insert(0, str(LIB_PATH))

# Etats finaux / acceptés (le worker ne les fait pas progresser).
TERMINAL_STATES = {"sota_cited_confirmed", "retracted"}
WAITING_STATES = {"awaiting_rtfm_ocr"}
BLOCKED_PREFIX = "blocked_human"

# États non-finaux que le worker doit faire progresser.
ACTIVE_STATES = {
    "candidate", "uid_resolved", "pdf_acquired", "needs_reacquisition",
    "page1_validated",  # → curator domain; worker stops here
}

# Ordre canonique de la machine d'état (pour tri de progression).
STATE_ORDER = {
    "candidate": 0,
    "uid_resolved": 1,
    "pdf_acquired": 2,
    "needs_reacquisition": 2,
    "awaiting_rtfm_ocr": 3,
    "page1_validated": 4,
    "sota_cited_confirmed": 5,
    "retracted": 99,
}
