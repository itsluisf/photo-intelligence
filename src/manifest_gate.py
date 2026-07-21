#!/usr/bin/env python3
"""
manifest_gate.py — fast change-detection gate for the photo-intel export.

Enumerates the Apple Photos library directly from Photos.sqlite (read-only
WAL-aware open, no copy) in ~7 s for ~200k assets, and diffs it
against a saved manifest to find new/changed photos. This lets the 2-hourly
export skip osxphotos entirely on quiet runs, instead of paying osxphotos's
>5-min-per-window PhotosDB load across all 48 year windows (~9 h/sweep).

Change key per asset: (ZMODIFICATIONDATE, face_n, computed_n, scene_n), where
the last three are per-asset row-counts from ZDETECTEDFACE,
ZCOMPUTEDASSETATTRIBUTES, and ZSCENECLASSIFICATION — the tables backing
osxphotos's --person-keyword / {searchinfo.activity} / {searchinfo.venue_type}
keyword templates. Round-2 shadow validation (2026-07-15) found the original
mod-date-only key missed 99.5% of what the real sweep re-exported, because
Apple's photoanalysisd/mediaanalysisd populate those tables independently of
ZMODIFICATIONDATE. A new UUID, or a change in ANY of the four values, marks
the asset for export — deliberately over-inclusive (a false positive costs
one harmless re-export; a false negative is the bug being fixed).
Deletions are ignored — the pipeline is additive (no rsync --delete).

The osxphotos full --update sweep remains the weekly backstop: it reconciles
edits this gate's mod-date heuristic might miss, and doubles as a schema-drift
detector (Apple changes the Photos.sqlite schema across macOS versions; this
gate fails loud if the expected tables/columns disappear — see _assert_schema).

Outputs (live mode):
  --changed-out FILE   TSV "uuid<TAB>year" of new/changed assets, grouped-by-year
                       downstream. Empty file => nothing to export.
  --manifest-out FILE  freshly built manifest TSV; the caller promotes it to the
                       canonical manifest ONLY after a successful export, so a
                       failed export doesn't swallow the pending changes.

Shadow mode (--shadow): validation only. Maintains its OWN baseline
(manifest/shadow-manifest.tsv), advances it each run, and appends the per-run
delta to manifest/shadow.log. Never touches the canonical manifest or the
pipeline. Used to confirm the gate's "changed" set covers what the live
osxphotos sweep actually re-exports, before cutover.

Usage:
    python3 manifest_gate.py [--config PATH] [--manifest PATH]
                             [--changed-out FILE] [--manifest-out FILE]
                             [--shadow]
"""

import argparse
import configparser
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("manifest_gate")

# Core Data epoch (2001-01-01 UTC) offset from Unix epoch, in seconds.
COREDATA_EPOCH_OFFSET = 978307200

# Manifest TSV line: "<uuid>\t<year>\t<changekey>\t<filename>"
# changekey = repr((moddate_or_None, face_n, computed_n, scene_n)).


def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    return cfg


def photos_db_path(library: str) -> Path:
    db = Path(library) / "database" / "Photos.sqlite"
    if not db.exists():
        log.error("Photos.sqlite not found at %s", db)
        sys.exit(1)
    return db


def _assert_schema(conn: sqlite3.Connection) -> None:
    """Fail loud if the expected schema is gone (macOS Photos upgrades change
    it). The weekly osxphotos sweep keeps working regardless; this just refuses
    to emit a wrong (silently-empty) changed set."""
    need = {
        "ZASSET": {"Z_PK", "ZUUID", "ZDATECREATED",
                   "ZMODIFICATIONDATE", "ZTRASHEDSTATE"},
        "ZADDITIONALASSETATTRIBUTES": {"Z_PK", "ZASSET", "ZORIGINALFILENAME"},
        "ZDETECTEDFACE": {"ZASSETFORFACE"},
        "ZCOMPUTEDASSETATTRIBUTES": {"ZASSET"},
        "ZSCENECLASSIFICATION": {"ZASSETATTRIBUTES"},
    }
    for table, cols in need.items():
        try:
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error as e:
            log.error("Schema check failed for %s: %s", table, e)
            sys.exit(2)
        missing = cols - have
        if missing:
            log.error(
                "Photos.sqlite schema drift: %s missing %s. Raw-SQL gate is "
                "unsafe; rely on the weekly osxphotos sweep and update "
                "manifest_gate.py for the new schema.", table, sorted(missing))
            sys.exit(2)


def build_manifest(db_path: Path) -> dict[str, tuple[str, str, str]]:
    """uuid -> (changekey, year, filename), via read-only WAL-aware open.

    mode=ro, NOT immutable=1: immutable tells SQLite the file cannot change,
    so it ignores Photos.sqlite-wal entirely. Photos keeps the library in WAL
    mode and can go weeks between checkpoints (observed 2026-07-08: a 160 GB
    WAL with all writes since 06-16 in it), so an immutable open silently
    reads a stale snapshot and reports changed=0 forever. A plain read-only
    connection participates in WAL and sees current data."""
    uri = f"file:{db_path}?mode=ro"
    sql = """
        SELECT a.ZUUID,
               strftime('%Y', datetime(a.ZDATECREATED + ?, 'unixepoch')) AS yr,
               a.ZMODIFICATIONDATE,
               aa.ZORIGINALFILENAME,
               COALESCE(f.n, 0), COALESCE(c.n, 0), COALESCE(s.n, 0)
        FROM ZASSET a
        LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
        LEFT JOIN (SELECT ZASSETFORFACE AS pk, COUNT(*) AS n
                   FROM ZDETECTEDFACE GROUP BY ZASSETFORFACE) f ON f.pk = a.Z_PK
        LEFT JOIN (SELECT ZASSET AS pk, COUNT(*) AS n
                   FROM ZCOMPUTEDASSETATTRIBUTES GROUP BY ZASSET) c ON c.pk = a.Z_PK
        LEFT JOIN (SELECT aa2.ZASSET AS pk, COUNT(*) AS n
                   FROM ZSCENECLASSIFICATION sc
                   JOIN ZADDITIONALASSETATTRIBUTES aa2 ON aa2.Z_PK = sc.ZASSETATTRIBUTES
                   GROUP BY aa2.ZASSET) s ON s.pk = a.Z_PK
        WHERE a.ZTRASHEDSTATE = 0
    """
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        log.error("Cannot open Photos.sqlite (read-only): %s", e)
        sys.exit(1)
    manifest: dict[str, tuple[str, str, str]] = {}
    try:
        _assert_schema(conn)
        rows = conn.execute(sql, (COREDATA_EPOCH_OFFSET,))
        for uuid, yr, moddate, fname, face_n, computed_n, scene_n in rows:
            if not uuid:
                continue
            changekey = repr((moddate, face_n, computed_n, scene_n))
            manifest[uuid] = (changekey, yr or "unknown", fname or "")
    finally:
        conn.close()
    return manifest


def read_manifest(path: Path) -> dict[str, str]:
    """Saved manifest TSV -> uuid -> changekey (column 3)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                out[parts[0]] = parts[2]
    return out


def write_manifest(path: Path, manifest: dict[str, tuple[str, str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for uuid, (changekey, yr, fname) in manifest.items():
            fh.write(f"{uuid}\t{yr}\t{changekey}\t{fname}\n")
    tmp.replace(path)  # atomic promote


def diff_changed(old: dict[str, str],
                 new: dict[str, tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Return [(uuid, year), ...] for assets new or whose changekey changed."""
    changed = []
    for uuid, (changekey, yr, _fn) in new.items():
        prev = old.get(uuid)
        if prev is None or prev != changekey:
            changed.append((uuid, yr))
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="photo-intel manifest change gate")
    ap.add_argument("--config", default="photo-intel.conf")
    ap.add_argument("--manifest", default=None,
                    help="Canonical manifest TSV "
                         "(default: <exportdb dir>/manifest/manifest.tsv)")
    ap.add_argument("--changed-out",
                    help="Write new/changed assets here as 'uuid<TAB>year'")
    ap.add_argument("--manifest-out",
                    help="Write the freshly built manifest here (caller promotes "
                         "to canonical only after a successful export)")
    ap.add_argument("--shadow", action="store_true",
                    help="Validation only: maintain a separate baseline, advance "
                         "it each run, append per-run delta to shadow.log, touch "
                         "neither the canonical manifest nor the pipeline")
    args = ap.parse_args()

    cfg = load_config(args.config)
    library = cfg.get("paths", "library")
    exportdb = Path(cfg.get("paths", "exportdb"))

    manifest_dir = exportdb.parent / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    if args.shadow:
        baseline = manifest_dir / "shadow-manifest.tsv"
    else:
        baseline = Path(args.manifest) if args.manifest else manifest_dir / "manifest.tsv"

    db = photos_db_path(library)

    t0 = datetime.now()
    new_manifest = build_manifest(db)
    old = read_manifest(baseline)
    changed = diff_changed(old, new_manifest)
    dt = (datetime.now() - t0).total_seconds()

    log.info("assets=%d  prior_manifest=%d  changed=%d  (%.1fs)",
             len(new_manifest), len(old), len(changed), dt)

    if args.shadow:
        ts = datetime.now(timezone.utc).isoformat()
        uuids = [u for u, _ in changed]
        with (manifest_dir / "shadow.log").open("a") as fh:
            fh.write(f"{ts}\tassets={len(new_manifest)}\tprior={len(old)}\t"
                     f"changed={len(changed)}\t"
                     f"uuids={','.join(uuids[:50])}"
                     f"{'...' if len(uuids) > 50 else ''}\n")
        write_manifest(baseline, new_manifest)  # advance shadow baseline
        log.info("Shadow run — baseline advanced, no export triggered.")
        return

    if args.changed_out:
        with Path(args.changed_out).open("w") as fh:
            for uuid, yr in changed:
                fh.write(f"{uuid}\t{yr}\n")
    if args.manifest_out:
        write_manifest(Path(args.manifest_out), new_manifest)

    log.info("No changes — export can be skipped." if not changed
             else f"{len(changed)} assets to export.")


if __name__ == "__main__":
    main()
