#!/usr/bin/env python3
"""
photo_intel_thumbs.py — pre-warm the web app's thumbnail cache.

Walks the photo store and generates the 400 px grid thumbnail for every
image and video that does not have one yet, so first-time browsing in
the web UI never waits on a synchronous Pillow/HEIC decode or ffmpeg
frame extraction. Video posters go through the web app's
extract_video_frame() (~0.5–2 s of ffmpeg each), so the first run over
a video backlog is slow; after that it's incremental. Idempotent —
already-cached thumbs are skipped, so the nightly run only pays for
new media.

Reuses the web app's own index/thumbnail code (photo_intel_web.py in the
same directory) rather than duplicating it; module globals are set here
the same way photo_intel_web.main() sets them.

Modal (800 px) and map-panel (200 px) sizes stay on-demand — the grid
view is the hot path.

Usage:
    ~/photo-intel/venv/bin/python photo_intel_thumbs.py            # full run
    ~/photo-intel/venv/bin/python photo_intel_thumbs.py --limit 500
    ~/photo-intel/venv/bin/python photo_intel_thumbs.py --workers 2
"""

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import photo_intel_web as w

THUMB_SIZE = w.THUMB_SIZE  # 400 — keep in lockstep with the web app


def _init_worker(dest_dir: str, thumb_dir: str):
    # Each worker process needs the module globals make_thumbnail reads.
    w.DEST_DIR = Path(dest_dir)
    w.THUMB_DIR = Path(thumb_dir)


def _gen_one(item):
    uuid, src = item
    try:
        data = w.make_thumbnail(uuid, Path(src), THUMB_SIZE)
        return (uuid, data is not None)
    except Exception:
        return (uuid, False)


def main():
    parser = argparse.ArgumentParser(description="photo-intel thumbnail pre-warm")
    parser.add_argument("--config",
                        default=str(Path.home() / "photo-intel" / "photo-intel.conf"))
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N generations (0 = no limit)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    cfg = w.load_config(Path(args.config))
    w.DEST_DIR  = Path(cfg["dest_dir"]).resolve()
    w.THUMB_DIR = w.DEST_DIR / ".thumb_cache"
    w.THUMB_DIR.mkdir(exist_ok=True)

    print("photo-intel thumbnail pre-warm")
    print(f"  Photos : {w.DEST_DIR}")
    print(f"  Thumbs : {w.THUMB_DIR}")
    print("  Indexing files ...", flush=True)

    index = w.build_file_index()
    pending = []
    for uuid, path in index.items():
        suffix = path.suffix.lower()
        if suffix not in w.IMAGE_EXTS and suffix not in w.VIDEO_EXTS:
            continue
        if not (w.THUMB_DIR / f"{uuid}_{THUMB_SIZE}.jpg").exists():
            pending.append((uuid, str(path)))
    # Images first: they run at hundreds/s, video posters at a few/s, so a
    # --limit run or an interrupted run still clears the cheap work.
    pending.sort(key=lambda item: Path(item[1]).suffix.lower() in w.VIDEO_EXTS)
    if args.limit:
        pending = pending[:args.limit]

    n_video = sum(1 for _, p in pending
                  if Path(p).suffix.lower() in w.VIDEO_EXTS)
    print(f"  Indexed {len(index):,} files; {len(pending):,} thumbs to generate "
          f"({len(pending) - n_video:,} images, {n_video:,} video posters)",
          flush=True)
    if not pending:
        print("  Nothing to do.")
        return

    t0 = time.time()
    ok = fail = 0
    with Pool(args.workers, initializer=_init_worker,
              initargs=(str(w.DEST_DIR), str(w.THUMB_DIR))) as pool:
        for i, (uuid, success) in enumerate(pool.imap_unordered(_gen_one, pending), 1):
            if success:
                ok += 1
            else:
                fail += 1
                print(f"  FAIL {uuid}", flush=True)
            if i % 1000 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i:,}/{len(pending):,} ({rate:.0f}/s)", flush=True)

    dt = time.time() - t0
    print(f"  Done: {ok:,} generated, {fail:,} failed in {dt/60:.1f} min")
    # Nonzero exit surfaces persistent failures in the systemd journal.
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
