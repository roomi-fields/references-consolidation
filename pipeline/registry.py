"""Chargement / mutation atomique des fichiers refs du registry."""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import REFS, SOURCES


@dataclass
class Ref:
    """Une référence du registry."""
    slug: str
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @property
    def state(self) -> str:
        return self.frontmatter.get("state", "candidate")

    @property
    def uid(self) -> str | None:
        return self.frontmatter.get("uid")

    @property
    def pdf_path_abs(self) -> Path | None:
        pp = self.frontmatter.get("pdf_path")
        if not pp:
            return None
        p = Path(pp)
        return p if p.is_absolute() else (SOURCES / pp)

    @property
    def cited_in(self) -> list[dict]:
        return self.frontmatter.get("cited_in") or []


def parse_frontmatter_md(text: str) -> tuple[dict | None, str]:
    """Parse un fichier .md avec frontmatter YAML séparateur `---`.

    Retourne (frontmatter_dict, body) ou (None, text) si parse impossible.
    """
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    try:
        return yaml.safe_load(parts[1]) or {}, parts[2]
    except yaml.YAMLError:
        return None, text


def load_ref(path: Path) -> Ref | None:
    """Charge une ref depuis son fichier .md. Renvoie None si parse échoue."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_frontmatter_md(text)
    if fm is None:
        return None
    return Ref(slug=path.stem, path=path, frontmatter=fm, body=body)


def iter_refs(refs_dir: Path = REFS):
    """Itère sur toutes les refs du registry."""
    for p in sorted(refs_dir.glob("*.md")):
        ref = load_ref(p)
        if ref is not None:
            yield ref


def save_ref(ref: Ref) -> None:
    """Écrit la ref atomiquement (tempfile + os.replace).

    Le body est préservé tel quel. Le frontmatter est re-dumpé en YAML.
    """
    fm_yaml = yaml.safe_dump(
        ref.frontmatter,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    )
    content = f"---\n{fm_yaml}---{ref.body}"
    dir_ = ref.path.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=ref.path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, ref.path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_state_history(ref: Ref, new_state: str, by: str, meta: dict | None = None) -> None:
    """Ajoute une entrée state_history et met à jour state. Mutation in-place."""
    hist = ref.frontmatter.setdefault("state_history", [])
    entry: dict[str, Any] = {
        "state": new_state,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by": by,
    }
    if meta:
        entry["meta"] = meta
    hist.append(entry)
    ref.frontmatter["state"] = new_state


def append_acquisition_attempt(
    ref: Ref, source: str, verdict: str, info: dict | None = None
) -> int:
    """Ajoute une entrée acquisition_attempts. Retourne le nouveau numéro."""
    attempts = ref.frontmatter.setdefault("acquisition_attempts", [])
    n = len(attempts) + 1
    entry: dict[str, Any] = {
        "n": n,
        "source": source,
        "verdict": verdict,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if info:
        entry.update(info)
    attempts.append(entry)
    return n
