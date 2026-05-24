"""Chemins et constantes."""
from pathlib import Path
import sys

VAULT = Path("/mnt/d/Obsidian/Articles/Projets/Ontologie musicale")
SOURCES = VAULT / "10_SOURCES"
REGISTRY = SOURCES / "_registry"
REFS = REGISTRY / "refs"
JOURNAL = REGISTRY / "_journal"
QUARANTINE = REGISTRY / "_quarantine"

PLUGIN_LIB = Path.home() / ".claude/plugins/source-collector/lib"
TOOLS = REGISTRY / "tools"

# Insère le plugin lib dans sys.path pour que `import validate_pdf_content` marche.
if str(PLUGIN_LIB) not in sys.path:
    sys.path.insert(0, str(PLUGIN_LIB))

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
