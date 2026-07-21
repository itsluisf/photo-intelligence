#!/usr/bin/env python3
"""
photo_intel_export.py
Export photos from Apple Photos library via osxphotos, chunked by year,
then rsync each chunk to the processing host (split mode) or leave in
place (local mode).

Key design points:
  - exportdb lives at paths.exportdb (persistent, never inside staging).
  - staging is a PERSISTENT local mirror (not cleared) — osxphotos
    --update consults the destination's file presence, so wiping
    staging forces a full re-export. See incident 2026-05-26.
  - Each year window is a separate osxphotos invocation scoped with
    --from-date / --to-date so a crash mid-window loses only that window.
  - --not-shared: shared-album assets never enter the pipeline.
  - Idempotent: re-running any window re-exports only changed/new photos
    because --update consults the persistent exportdb AND the persistent
    staging tree.

Usage:
    python3 photo_intel_export.py [--config PATH] [--window WINDOW] [--dry-run]

    --config   path to photo-intel.conf (default: ./photo-intel.conf)
    --window   export only this window, e.g. "2024" or "pre1980"
               (default: all windows in config order)
    --dry-run  print osxphotos + rsync commands without running them
"""

import argparse
import configparser
import logging
import resource
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("photo_intel_export")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    read = cfg.read(path)
    if not read:
        log.error("Config file not found: %s", path)
        sys.exit(1)
    return cfg


def assert_volume_mounted(p: Path) -> None:
    """Abort if p lives on a volume that isn't currently mounted.

    staging is the osxphotos --update anchor; if its USB volume is
    absent, mkdir would silently recreate an empty staging tree on the
    boot volume and every photo would re-export. Fail loud instead.
    """
    parts = p.resolve().parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        mount = Path("/") / parts[1] / parts[2]
        if not mount.is_mount():
            log.error("Staging volume not mounted: %s", mount)
            log.error("Refusing to run — would re-export the whole library.")
            sys.exit(1)


def parse_windows(cfg: configparser.ConfigParser) -> list[str]:
    raw = cfg.get("export", "windows", fallback="")
    return [w.strip() for w in raw.splitlines() if w.strip()]


def window_dates(window: str) -> tuple[str, str]:
    """
    Return (from_date, to_date) as YYYY-MM-DD strings for the given window.
    'pre1980' → 1800-01-01 … 1980-01-01
    '2024'    → 2024-01-01 … 2025-01-01
    """
    if window == "pre1980":
        return ("1800-01-01", "1980-01-01")
    try:
        y = int(window)
    except ValueError:
        log.error("Unknown window format: %r", window)
        sys.exit(1)
    return (f"{y:04d}-01-01", f"{y+1:04d}-01-01")


def year_to_window(year: str, windows: list[str]) -> str | None:
    """Map a manifest year string ('2024', '1975', 'unknown') to a configured
    export window. Pre-1980 collapses to 'pre1980'. Returns None for years that
    don't map to any configured window (e.g. 'unknown' / undated assets) — those
    are left to the weekly full sweep, which is the gate's backstop anyway.
    """
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    window = "pre1980" if y < 1980 else str(y)
    return window if window in windows else None


# ---------------------------------------------------------------------------
# Export one year window
# ---------------------------------------------------------------------------
def export_window(
    window: str,
    library: str,
    staging_dir: Path,
    exportdb: Path,
    dry_run: bool,
    uuid_file: Path | None = None,
) -> bool:
    """
    Run osxphotos export for one year window into staging_dir.
    Returns True on success, False on failure.

    If uuid_file is given, the export is scoped to exactly those UUIDs
    (--uuid-from-file) — used by the manifest-gated incremental path so a
    window with a handful of changed photos still produces the SAME staging
    path layout (staging/<window>/<month>/<uuid>.ext) as the full sweep,
    keeping osxphotos --update / the shared exportdb consistent across both
    modes. The --from-date/--to-date bound is kept (redundant with the UUID
    filter, but it preserves an identical invocation shape).
    """
    from_date, to_date = window_dates(window)
    window_staging = staging_dir / window
    window_staging.mkdir(parents=True, exist_ok=True)

    cmd = [
        "osxphotos", "export",
        str(window_staging),
        "--db", library,
        "--exportdb", str(exportdb),
        "--update",
        "--not-shared",
        "--skip-edited",
        "--sidecar", "json",
        "--exiftool",
        "--person-keyword",
        "--keyword-template", "{label}",
        "--keyword-template", "{searchinfo.activity}",
        "--keyword-template", "{searchinfo.venue_type}",
        "--directory", "{created.month}",
        "--filename", "{uuid}",
        "--from-date", from_date,
        "--to-date", to_date,
        "--verbose",
    ]
    if uuid_file is not None:
        cmd += ["--uuid-from-file", str(uuid_file)]

    log.info("Window %-8s  from %s to %s", window, from_date, to_date)
    log.info("Command: %s", shlex.join(cmd))

    if dry_run:
        return True

    # Raise open-file limit for exiftool's parallel temp-file usage.
    # macOS default (256) is too low for large exports.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(4096, hard)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        log.info("Raised RLIMIT_NOFILE %d → %d", soft, target)

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        log.error("osxphotos failed for window %s (exit %d)", window, result.returncode)
        return False

    return True


# ---------------------------------------------------------------------------
# rsync one window's staging dir to dest
# ---------------------------------------------------------------------------
def rsync_window(
    window: str,
    staging_dir: Path,
    rsync_dest: str,
    ssh_key: str,
    dry_run: bool,
) -> bool:
    """
    rsync window_staging/ → rsync_dest/window/
    Returns True on success, False on failure.
    """
    window_staging = staging_dir / window
    # Trailing slash on source = contents, not the directory itself
    src = str(window_staging) + "/"
    dest = rsync_dest.rstrip("/") + f"/{window}/"

    ssh_opts = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    # No --delete: the pipeline is additive-only. With it, an emptied
    # staging window (unmounted volume, manual clear) would mass-delete
    # that window on the processing host on the next scheduled run.
    cmd = [
        "rsync", "-av",
        "-e", ssh_opts,
        src, dest,
    ]

    log.info("rsync  %s → %s", src, dest)
    log.info("Command: %s", shlex.join(cmd))

    if dry_run:
        return True

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        log.error("rsync failed for window %s (exit %d)", window, result.returncode)
        return False

    return True


# ---------------------------------------------------------------------------
# Clear staging for one window
# ---------------------------------------------------------------------------
# NOTE: no longer called. staging is a persistent local mirror and is the
# osxphotos --update anchor — clearing it forces a full re-export every
# run (incident 2026-05-26). Kept defined for ad-hoc/manual use only.
def clear_staging(window: str, staging_dir: Path, dry_run: bool) -> None:
    window_staging = staging_dir / window
    if not window_staging.exists():
        return
    log.info("Clearing staging: %s", window_staging)
    if not dry_run:
        shutil.rmtree(window_staging)
        window_staging.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Manifest-gated incremental export
# ---------------------------------------------------------------------------
def run_gated(
    changed_file: Path,
    windows: list[str],
    mode: str,
    library: str,
    staging_dir: Path,
    exportdb: Path,
    rsync_dest: str,
    ssh_key: str,
    dry_run: bool,
) -> None:
    """Export only the UUIDs in changed_file (TSV 'uuid<TAB>year'), grouping by
    year window so each osxphotos invocation matches the full-sweep layout.

    On any failure exits non-zero WITHOUT signalling success, so the calling
    wrapper does not promote the pending manifest and the next run retries.
    """
    if not changed_file.exists():
        log.error("Changed-file not found: %s", changed_file)
        sys.exit(1)

    by_window: dict[str, list[str]] = {}
    skipped = 0
    with changed_file.open() as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if not parts or not parts[0]:
                continue
            uuid = parts[0]
            year = parts[1] if len(parts) > 1 else "unknown"
            window = year_to_window(year, windows)
            if window is None:
                skipped += 1
                continue
            by_window.setdefault(window, []).append(uuid)

    if skipped:
        log.warning("%d changed asset(s) had no mappable window (e.g. undated) "
                    "— left to the weekly full sweep.", skipped)

    if not by_window:
        log.info("Gated: nothing to export.")
        return

    total = sum(len(v) for v in by_window.values())
    log.info("Gated: %d changed asset(s) across %d window(s): %s",
             total, len(by_window), ", ".join(sorted(by_window)))

    # Per-window UUID lists live next to the exportdb (persistent, fast disk).
    changed_dir = exportdb.parent / "changed"
    changed_dir.mkdir(parents=True, exist_ok=True)

    errors = []
    for window in sorted(by_window):
        uuid_file = changed_dir / f"{window}.txt"
        uuid_file.write_text("\n".join(by_window[window]) + "\n")

        ok = export_window(window, library, staging_dir, exportdb, dry_run,
                            uuid_file=uuid_file)
        if not ok:
            errors.append(window)
            continue

        if mode == "split":
            ok = rsync_window(window, staging_dir, rsync_dest, ssh_key, dry_run)
            if not ok:
                errors.append(window)

    if errors:
        log.error("Gated: failed window(s): %s", ", ".join(errors))
        sys.exit(1)
    log.info("Gated: all %d changed asset(s) exported.", total)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="photo-intel export script")
    parser.add_argument("--config", default="photo-intel.conf",
                        help="Path to photo-intel.conf")
    parser.add_argument("--window",
                        help="Export only this window (e.g. '2024' or 'pre1980')")
    parser.add_argument("--changed-file",
                        help="Manifest-gated mode: TSV 'uuid<TAB>year' (from "
                             "manifest_gate.py --changed-out). Exports only these "
                             "UUIDs, one osxphotos pass per affected year window, "
                             "instead of sweeping all 48 windows.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without running them")
    args = parser.parse_args()

    cfg = load_config(args.config)
    mode = cfg.get("general", "mode", fallback="split")

    library     = cfg.get("paths", "library")
    staging_dir = Path(cfg.get("paths", "staging_dir"))
    exportdb    = Path(cfg.get("paths", "exportdb"))

    rsync_dest  = cfg.get("transfer", "rsync_dest", fallback="")
    ssh_key     = cfg.get("transfer", "ssh_key", fallback="")

    # exportdb must live outside staging — enforce it
    try:
        exportdb.relative_to(staging_dir)
        log.error(
            "exportdb (%s) is inside staging_dir (%s). "
            "This breaks --update across staging clears. "
            "Move exportdb outside staging_dir in photo-intel.conf.",
            exportdb, staging_dir,
        )
        sys.exit(1)
    except ValueError:
        pass  # exportdb is outside staging_dir — good

    # staging is the --update anchor; abort if its volume is absent.
    # Must run BEFORE the mkdir below, which would otherwise silently
    # recreate an empty staging tree on the boot volume.
    assert_volume_mounted(staging_dir)

    # Ensure persistent dirs exist
    exportdb.parent.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------
    # Manifest-gated incremental mode: export only the changed UUIDs,
    # one osxphotos pass per affected year window (typically 0-2), not 48.
    # -----------------------------------------------------------------
    if args.changed_file:
        run_gated(
            Path(args.changed_file), parse_windows(cfg), mode,
            library, staging_dir, exportdb, rsync_dest, ssh_key, args.dry_run,
        )
        return

    windows = [args.window] if args.window else parse_windows(cfg)
    if not windows:
        log.error("No export windows defined in config.")
        sys.exit(1)

    log.info("Mode: %s  |  Windows: %d  |  Dry-run: %s",
             mode, len(windows), args.dry_run)

    errors = []
    for window in windows:
        ok = export_window(
            window, library, staging_dir, exportdb, args.dry_run
        )
        if not ok:
            errors.append(window)
            continue

        if mode == "split":
            ok = rsync_window(
                window, staging_dir, rsync_dest, ssh_key, args.dry_run
            )
            if not ok:
                errors.append(window)
                continue
            # staging is NOT cleared — it is a persistent mirror and the
            # osxphotos --update anchor. See incident 2026-05-26.
        # local mode: leave files in staging_dir for Phase 1 to ingest

    if errors:
        log.error("Failed windows: %s", ", ".join(errors))
        sys.exit(1)
    else:
        log.info("All windows complete.")


if __name__ == "__main__":
    main()
