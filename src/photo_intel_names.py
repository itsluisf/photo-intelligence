"""Person-name canonicalization for photo-intel.

Phase 1 reads `persons` from `XMP:PersonInImage`, not from an Apple API.
That tag carries face names baked into imported files by other software as
well as Apple Photos' own, so the vocabulary drifts: case variants
("Jane"/"jane"), misspellings ("Jon Smtih", "janedoe"), and a long
tail of legacy tags from an earlier tagging pass on old scans
("Mommy", "Grandpa", bare first names).

Two mechanisms live here, deliberately kept separate:

  ALIASES — `person_aliases.json`. Rewrites a raw XMP name to a canonical
    spelling at ingest. Lookup is case-insensitive, which is what collapses
    the "Jane"/"jane" style duplicates. The file has two sections:
      "generated" — rebuilt by `photo_intel_names_admin.py build-aliases`.
                    Do not hand-edit; your changes will be overwritten.
      "curated"   — hand-maintained, and WINS over "generated" on conflict.
                    This is where judgment calls go.

  APPLE VOCAB — the `apple_persons` table: the people actually named in
    Apple Photos, refreshed from `osxphotos persons` on the export host. It decides
    which names the UI *suggests*. Names outside it stay fully searchable —
    the People control is a free-text input, so a legacy tag is still
    reachable by typing it. It just isn't offered in the typeahead.

Both degrade to no-ops when their backing data is missing: no alias file
means no rewriting, an empty `apple_persons` table means every name counts
as known. In either case the pipeline behaves exactly as it did before this
module existed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ALIAS_FILENAME = "person_aliases.json"


# ─────────────────────────────────────────────────────────────────────────────
# ALIASES
# ─────────────────────────────────────────────────────────────────────────────

def alias_path(base: Path | str | None = None) -> Path:
    """Where the alias file lives — next to the scripts by default."""
    base = Path(base) if base else Path(__file__).resolve().parent
    return base / ALIAS_FILENAME


def load_aliases(path: Path | str | None = None) -> dict[str, str]:
    """Flatten person_aliases.json into {lowercased raw name: canonical}.

    "curated" is layered over "generated" so a hand-written decision always
    beats the generator. A missing or malformed file yields {} — callers
    then pass names through untouched, which is the pre-alias behavior.
    """
    p = Path(path) if path else alias_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    out: dict[str, str] = {}
    for section in ("generated", "curated"):
        block = doc.get(section) or {}
        if not isinstance(block, dict):
            continue
        for raw, canon in block.items():
            if isinstance(raw, str) and isinstance(canon, str) and raw.strip() and canon.strip():
                out[raw.strip().lower()] = canon.strip()
    return out


def canonicalize(names, aliases: dict[str, str]) -> list[str]:
    """Apply the alias map to a list of raw XMP names.

    Order is preserved and duplicates are dropped — collapsing "Jane" and
    "jane" onto one canonical spelling would otherwise leave the same
    person listed twice on photos that carried both.
    """
    seen: set[str] = set()
    out: list[str] = []
    for n in names or []:
        if not isinstance(n, str):
            continue
        n = n.strip()
        if not n:
            continue
        canon = aliases.get(n.lower(), n)
        key = canon.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(canon)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# APPLE VOCAB
# ─────────────────────────────────────────────────────────────────────────────

APPLE_PERSONS_DDL = """
CREATE TABLE IF NOT EXISTS apple_persons (
    name         TEXT PRIMARY KEY,   -- exactly as Apple Photos spells it
    photo_count  INTEGER,            -- per `osxphotos persons`, for ranking
    refreshed_at TEXT
);
"""


def ensure_apple_persons(conn: sqlite3.Connection) -> None:
    conn.execute(APPLE_PERSONS_DDL)


def apple_person_names(conn: sqlite3.Connection) -> set[str]:
    """Lowercased Apple Photos person names, or an empty set if unpopulated.

    An empty set is the signal for "vocab unknown"; callers treat that as
    "every name is known" rather than hiding the whole list.
    """
    try:
        rows = conn.execute("SELECT name FROM apple_persons").fetchall()
    except sqlite3.Error:
        return set()
    return {r[0].lower() for r in rows if r[0]}
