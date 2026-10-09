#!/usr/bin/env python3
"""Maintenance for photo-intel's person-name vocabulary.

Subcommands
-----------
  refresh-vocab <file.json>   Load the Apple Photos person list (produced on
                              the export host by photo_intel_apple_vocab.py) into the
                              `apple_persons` table.
  build-aliases               Rebuild the "generated" section of
                              person_aliases.json from what is actually in the
                              DB. Purely mechanical: it only collapses names
                              that differ by letter case. "curated" is read,
                              preserved and never touched.
  backfill                    Apply the alias map to existing `persons` rows.
                              Dry-run unless --apply is given. Phase 1 applies
                              aliases to new rows on its own; this is for rows
                              ingested before the alias existed.
  report                      Print a Markdown review sheet of the non-Apple
                              names worth a human decision, with the evidence
                              needed to make it (photo count, date span, who
                              else is in the photo).

Why "generated" is case-only: an earlier heuristic pass matched bare first
names against Apple's list and confidently proposed Sam -> "Sam Rivera (shortstop)",
Pat -> "Pat Lee, stadium usher". Those Apple entries are strangers
photographed at events; the bare tags are ordinary family. Anything
requiring that kind of judgment belongs in "curated", decided by a human, and
`report` exists to make that decision cheap rather than to automate it.
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import photo_intel_names as names_mod

HERE = Path(__file__).resolve().parent
DEFAULT_CONF = HERE / "photo-intel.conf"


def db_path_from_conf(conf: Path) -> str:
    cp = configparser.ConfigParser()
    cp.read(conf)
    return cp.get("paths", "db_path")


def load_persons_rows(conn: sqlite3.Connection):
    """(uuid, [raw names], date) for every row carrying people."""
    sql = ("SELECT uuid, persons, date FROM photos "
           "WHERE persons IS NOT NULL AND persons != '[]'")
    for uuid, praw, date in conn.execute(sql):
        try:
            parsed = json.loads(praw) or []
        except (TypeError, ValueError):
            continue
        clean = [n.strip() for n in parsed if isinstance(n, str) and n.strip()]
        if clean:
            yield uuid, clean, date


# ─────────────────────────────────────────────────────────────────────────────
# refresh-vocab
# ─────────────────────────────────────────────────────────────────────────────

def cmd_refresh_vocab(args) -> int:
    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    people = doc.get("persons", doc)
    if not isinstance(people, dict) or not people:
        print("error: no persons in that file", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path_from_conf(Path(args.conf)))
    names_mod.ensure_apple_persons(conn)
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("DELETE FROM apple_persons")
        conn.executemany(
            "INSERT INTO apple_persons (name, photo_count, refreshed_at) VALUES (?,?,?)",
            [(n, int(c), now) for n, c in people.items()],
        )
    print(f"apple_persons: {len(people)} names loaded (source {doc.get('generated', 'n/a')})")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# build-aliases
# ─────────────────────────────────────────────────────────────────────────────

def pick_canonical(variants: list[str], counts: Counter, apple: dict[str, str]) -> str:
    """Choose the spelling a set of case-variants should collapse onto.

    Apple's own spelling wins outright when one exists — it is the only
    authoritative source here. Otherwise the most-used spelling wins, with
    a capitalized form preferred over a lowercase one on a tie so the list
    reads as names rather than as raw tags.
    """
    for v in variants:
        if v.lower() in apple:
            return apple[v.lower()]
    return sorted(variants, key=lambda v: (-counts[v], not v[:1].isupper(), v))[0]


def cmd_build_aliases(args) -> int:
    conn = sqlite3.connect(db_path_from_conf(Path(args.conf)))
    # Empty when the vocab has never been refreshed — the generator then falls
    # back to frequency alone and simply cannot enforce Apple's spelling.
    apple = ({r[0].lower(): r[0] for r in conn.execute("SELECT name FROM apple_persons")}
             if names_mod.apple_person_names(conn) else {})

    counts: Counter[str] = Counter()
    for _uuid, raw_names, _date in load_persons_rows(conn):
        counts.update(raw_names)

    groups: dict[str, list[str]] = defaultdict(list)
    for n in counts:
        groups[n.lower()].append(n)

    generated: dict[str, str] = {}
    for fold, variants in groups.items():
        canon = pick_canonical(variants, counts, apple)
        # Only record an entry that actually changes something: either the
        # group has several spellings, or the single spelling disagrees with
        # Apple's. Identity mappings would just bloat the file.
        if len(variants) > 1 or variants[0] != canon:
            generated[fold] = canon

    path = names_mod.alias_path(HERE)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    curated = doc.get("curated") or {}

    doc = {
        "_readme": (
            "Person-name aliases for photo-intel. Lookup is case-insensitive: "
            "keys are lowercased raw XMP names, values are the canonical "
            "spelling written to the persons column. 'generated' is rebuilt by "
            "photo_intel_names_admin.py build-aliases and must not be "
            "hand-edited. 'curated' is hand-maintained and wins on conflict."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "curated": curated,
        "generated": dict(sorted(generated.items())),
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    collapsed = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
    print(f"distinct names in DB        : {len(counts)}")
    print(f"case-variant groups         : {sum(1 for v in groups.values() if len(v) > 1)}"
          f"  ({collapsed} spellings collapse away)")
    print(f"generated entries           : {len(generated)}")
    print(f"curated entries (preserved) : {len(curated)}")
    print(f"written                     : {path}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# backfill
# ─────────────────────────────────────────────────────────────────────────────

def cmd_backfill(args) -> int:
    conn = sqlite3.connect(db_path_from_conf(Path(args.conf)))
    aliases = names_mod.load_aliases(names_mod.alias_path(HERE))
    if not aliases:
        print("no aliases loaded — nothing to do", file=sys.stderr)
        return 1

    changes: list[tuple[str, str]] = []
    examples: list[str] = []
    for uuid, raw_names, _date in load_persons_rows(conn):
        canon = names_mod.canonicalize(raw_names, aliases)
        if canon != raw_names:
            changes.append((json.dumps(canon), uuid))
            if len(examples) < 15:
                examples.append(f"    {raw_names}  ->  {canon}")

    print(f"rows needing rewrite: {len(changes)}")
    for line in examples:
        print(line)
    if len(changes) > 15:
        print(f"    ... {len(changes) - 15} more")

    if not args.apply:
        print("\ndry run — re-run with --apply to write. The FTS triggers keep "
              "photos_fts in sync automatically on UPDATE.")
        return 0

    with conn:
        conn.executemany("UPDATE photos SET persons = ? WHERE uuid = ?", changes)
    print(f"\napplied to {len(changes)} rows")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# report
# ─────────────────────────────────────────────────────────────────────────────

def cmd_report(args) -> int:
    conn = sqlite3.connect(db_path_from_conf(Path(args.conf)))
    if not names_mod.apple_person_names(conn):
        print("apple_persons is empty — run refresh-vocab first", file=sys.stderr)
        return 1
    apple_disp = {r[0].lower(): r[0] for r in conn.execute("SELECT name FROM apple_persons")}
    apple_lc = set(apple_disp)

    counts: Counter[str] = Counter()
    cooc: dict[str, Counter] = defaultdict(Counter)
    years: dict[str, list[str]] = defaultdict(list)
    for _uuid, raw_names, date in load_persons_rows(conn):
        in_apple = [apple_disp[n.lower()] for n in raw_names if n.lower() in apple_lc]
        for n in raw_names:
            if n.lower() in apple_lc:
                continue
            counts[n.lower()] += 1
            if date:
                years[n.lower()].append(date[:4])
            cooc[n.lower()].update(in_apple)

    def norm(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower())

    apple_by_norm = {}
    for lc, disp in apple_disp.items():
        apple_by_norm.setdefault(norm(disp), disp)

    cutoff = args.min_photos
    rows = [(n, c) for n, c in counts.most_common() if c >= cutoff]

    print("# Person-name review sheet\n")
    print(f"Generated {datetime.now(timezone.utc).date()} by "
          "`photo_intel_names_admin.py report`.\n")
    print(f"Non-Apple names on **{cutoff}+ photos** — {len(rows)} of "
          f"{len(counts)} total. The rest appear on one or two photos each "
          "and are not worth the review time.\n")
    print("To act on a decision, add it to the `curated` block of "
          "`person_aliases.json` as `\"raw name\": \"Canonical Name\"`, then run "
          "`build-aliases` (optional) and `backfill --apply`.\n")
    print("| Tag | Photos | Years | Appears with | Same-letters Apple name | Decision |")
    print("|---|---|---|---|---|---|")
    for n, c in rows:
        yrs = sorted(years[n])
        span = f"{yrs[0]}–{yrs[-1]}" if yrs else "—"
        with_who = ", ".join(f"{a} ({k})" for a, k in cooc[n].most_common(3)) or "*nobody named*"
        hit = apple_by_norm.get(norm(n), "")
        print(f"| `{n}` | {c} | {span} | {with_who} | {hit} | |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", default=str(DEFAULT_CONF))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh-vocab"); p.add_argument("file"); p.set_defaults(fn=cmd_refresh_vocab)
    p = sub.add_parser("build-aliases"); p.set_defaults(fn=cmd_build_aliases)
    p = sub.add_parser("backfill"); p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_backfill)
    p = sub.add_parser("report"); p.add_argument("--min-photos", type=int, default=3)
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
