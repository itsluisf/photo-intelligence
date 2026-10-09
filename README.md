# Photo Intelligence

A local-first photo enrichment pipeline for Apple Photos libraries. Exports your library via [osxphotos](https://github.com/RhetTbull/osxphotos), enriches each photo with AI-generated descriptions using a locally-running Gemma vision model (via [Ollama](https://ollama.com)), and serves the results through a web app with full-text search, Smart Search, voice input, a map view, and library stats.

No cloud services. No API keys. Your photos stay on your hardware.

---

## A side effect worth knowing about: your library stops being locked in

Search is the point of this project, but there's a second thing you get for free.

Apple Photos keeps your library in a `.photoslibrary` bundle backed by a
private, undocumented SQLite schema. The photos are in there as real files, but
the metadata that makes them *useful* — who's in them, where they were taken,
what Apple's analyzer labelled them — is only reachable through Apple's own app.

Because this pipeline exports with `--exiftool` and `--sidecar json`, what lands
on the processing host is not just pixels:

- **Metadata is written into the image files themselves.** Keywords, detected
  persons, GPS coordinates and dates are embedded via exiftool. Point Lightroom,
  Immich, digiKam or anything else at that directory and the metadata comes with
  it — no import step, no conversion, no database.
- **A JSON sidecar sits beside every file** carrying the fuller Photos metadata,
  including album membership, for anything the EXIF fields can't express.
- **Plain files in a plain directory tree**, organized `year/month`, one file per
  asset. Live Photos keep both components. Videos are included.
- **Nothing is ever deleted downstream.** The pipeline is additive by design, so
  an accidental deletion in Apple Photos doesn't propagate.

That makes the export a genuinely portable copy of your library rather than a
derived cache — you could stop using this project tomorrow and the exported tree
would still be worth having.

### What it is not

It is **not** a complete replacement for your Photos library, and you should not
delete anything on the strength of it. Specifically:

- **Edited versions are not exported.** The export uses `--skip-edited`, so you
  get the *original* of every photo. Crops, exposure adjustments and retouching
  live only in Apple Photos. On a real library this is not a rounding error — in
  the setup this was built against, 14% of assets carry edits.
- **Shared-album photos never leave the Mac** (`--not-shared`). This is a
  deliberate privacy choice, but it means shared content is simply absent.
- **Filenames are UUIDs and the tree is `year/month`.** Your album structure is
  preserved in the sidecars, not in the directory layout, so another tool will
  need to read those sidecars to reconstruct it.
- **It is one copy on one disk.** A single exported tree is not a backup in any
  3-2-1 sense. Back it up like you would any other data you care about.

Those flags are tuned for feeding a vision model, where originals make more
consistent inputs. If your priority is archival rather than search, drop
`--skip-edited` in `photo_intel_export.py` — but note that osxphotos will then
write both `{uuid}` and `{uuid}_edited` files, and Phase 1's `pick_for_index()`
does not yet know how to disambiguate that pair.

---

## How This Project Evolved

The original version of this project was built around a simple observation: Apple Photos already does the hard work of organizing a library — faces, dates, albums, GPS — but that metadata is locked inside `Photos.sqlite`, a private schema Apple doesn't document or support for external reads. If you want to search your own library by description or scene content, you're stuck with whatever the Photos app gives you.

**Phase 1** walked that private schema directly. It joined across internal tables (`ZASSET`, `ZADDITIONALASSETATTRIBUTES`, `ZSCENECLASSIFICATION`) to extract the metadata Apple computed, and wrote one row per photo to a standalone SQLite database. The join for scene labels required runtime-probing `sqlite_master` to find the dynamically-named album link table — Apple's internal numbering changes across library migrations. This worked, but it meant the pipeline was one Photos.app upgrade away from a broken schema.

**Phase 2** enriched what Phase 1 couldn't get from Apple: a natural-language description, tags, a location guess, and OCR of visible text. Each photo was encoded and sent to a locally-running vision model (Gemma via Ollama) which returned a structured JSON response. The catch was that the model was running on the same Mac as the Photos library — an M2 Mac Mini — with no GPU acceleration. At roughly 13 seconds per photo and 130,000+ images to process, the initial backfill was a multi-week background job.

A Flask web app sat on top of the database and served search, thumbnails, and full-resolution viewing without duplicating any photo data. Paths in the database pointed directly into the `.photoslibrary` bundle; the app streamed bytes on demand.

This system worked. The bulk enrichment completed. But two structural problems emerged that motivated a redesign.

**Problem 1: The Photos.sqlite dependency.** Apple's internal schema is undocumented and has changed across major OS versions. Any Photos.app update could silently break Phase 1. More practically, the pipeline could only run on the same Mac that held the library — there was no clean way to split processing off to a machine with a GPU.

**Problem 2: The hardware ceiling.** Processing 130,000 photos at 13 seconds each on CPU-only hardware took weeks. A newer machine with a dedicated GPU was available — an RTX 5060 Ti running under Linux — but the existing pipeline had no path to it.

---

The redesign replaced the direct `Photos.sqlite` walk with **osxphotos**, an actively maintained open-source library that reads the same database but provides a stable, versioned API layer on top of it. When Apple changes an internal table name or column, osxphotos absorbs the change — our pipeline code doesn't have to. More importantly, osxphotos exports photos alongside per-photo JSON sidecars that capture all the relevant metadata. Phase 1 now ingests those JSON sidecars instead of querying the database directly, and everything from Phase 1 onward — Phase 2, the web app, the database itself — runs without any dependency on `Photos.sqlite` or even macOS. That's what makes the split-host deployment possible.

The export job runs on the Mac and chunks the library by year window, using `osxphotos --update` for incremental runs — only new or changed photos are re-exported. A persistent local staging directory serves as the `--update` anchor. (Clearing it between runs caused a full re-export of the entire library the first time the job ran as a scheduled task; that failure mode is documented in the gotchas.) Exported files transfer to the processing machine over rsync/SSH.

Phase 2 now runs on the GPU machine. Moving to hardware-accelerated inference dramatically reduced per-photo processing time, and as the pipeline matured the model was upgraded from the original `gemma4:e4b` variant to the larger `gemma4:12b-it-q8_0` — something that wouldn't have been practical on CPU. Several bugs were ironed out during the production run: Live Photos export two files per UUID (a still and a `.mov` clip), and the file index had to be taught to always prefer the still; the model occasionally emits structurally-valid JSON with unescaped interior quotes, requiring a fallback repair pass; and on text-dense images (museum plaques, foreign-language signs) the model could enter a repetition loop that consumed the entire token budget. Each of these was found, debugged, and fixed.

The web app moved to the GPU machine alongside the database and gained new capabilities: full-text search using FTS5 (replacing per-column LIKE queries), an Ollama-backed Smart Search that converts natural-language queries into structured filters plus synonym expansion, a Voice tab that uses the browser's Speech API to drive the same search pipeline hands-free, a Map view with clustered GPS markers, video support with ffmpeg-extracted poster thumbnails, and a nightly thumbnail pre-warm job so the grid loads instantly.

The two deployment modes — all-local (single Mac, CPU) and split (Mac for export, Linux GPU machine for processing) — share one codebase. A single `photo-intel.conf` file drives both.

The legacy pipeline is preserved in `legacy/` for reference and for anyone running a single-Mac setup. The export-driven system is `main`.

---

## Architecture

```
Mac (Apple Photos library)
  manifest_gate.py        — diff Photos.sqlite against a saved manifest,
                            emit only changed UUIDs (~7 s for 200k assets)
  photo_intel_export.py   — osxphotos export, chunked by year → staging/
                            rsync staging/ → processing host (split mode)
  photo_intel_legacy_kw.py — optional keyword template that keeps pre-upgrade
  build_legacy_keywords.py   keywords across a major macOS upgrade

Processing host (Mac in local mode, or Linux/GPU in split mode)
  photo_intel_phase1.py   — ingest sidecar JSON → photo-intel.db
  photo_intel_phase2.py   — Gemma enrichment via Ollama → photo-intel.db
  photo_intel_video.py    — Phase 2b: Qwen-VL video enrichment (optional)
  photo_intel_web.py      — Flask web app, port 5052
  photo_intel_thumbs.py   — nightly thumbnail pre-warm (optional)
```

### The export gate

A full osxphotos `--update` sweep loads the whole PhotosDB once per year
window — over 5 minutes each, roughly 9 hours across 48 windows. Running that
every 2 hours to catch a handful of new photos is almost entirely wasted work.

`manifest_gate.py` reads `Photos.sqlite` directly (read-only, WAL-aware, no
copy) and builds a manifest keyed on four signals per asset:

```
(ZMODIFICATIONDATE, face_n, computed_n, scene_n)
```

The last three are per-asset row counts from `ZDETECTEDFACE`,
`ZCOMPUTEDASSETATTRIBUTES` and `ZSCENECLASSIFICATION` — the tables behind
osxphotos's `--person-keyword`, `{searchinfo.activity}` and
`{searchinfo.venue_type}` templates. A mod-date-only key is **not** sufficient:
macOS's `photoanalysisd`/`mediaanalysisd` populate those tables asynchronously,
long after the modification date settles, so a face or scene added by the
analyzer changes what osxphotos would export while the mod-date stays frozen.

Changed UUIDs are handed to `photo_intel_export.py --changed-file`, which
exports exactly those assets via `--uuid-from-file`, one osxphotos pass per
affected year window (typically 0–2) rather than all 48. The manifest is
promoted to canonical **only** after a successful export, so a failed run
retries rather than swallowing pending changes.

The gate is deliberately over-inclusive — a false positive costs one harmless
re-export, a false negative silently loses a photo. Deletions are ignored; the
pipeline is additive.

**A weekly full sweep remains the backstop.** `run_export_full.sh` runs the
unchanged 48-window export to catch anything the gate structurally misses
(export-db lag across a manifest reseed, analyzer backfills the changekey
doesn't track) and doubles as a schema-drift detector — Apple moves the
`Photos.sqlite` schema between macOS releases, and the gate fails loud when
the tables it expects disappear. The two jobs share a lock file so they never
run concurrent osxphotos passes against the same export DB.

Validate the gate before trusting it. `manifest_gate.py --shadow` keeps its own
baseline and logs what it *would* have flagged, without touching the pipeline —
run it alongside the full sweep for a week and confirm its changed-set covers
everything the sweep actually re-exported.

### Deployment modes

| Mode | Export | Phase 1/2 + web app | When to use |
|------|--------|---------------------|-------------|
| `local` | Mac | Same Mac | Single machine, CPU-only |
| `split` | Mac | Separate Linux/GPU host | Dedicated GPU machine for processing |

Set `mode = local` or `mode = split` in `photo-intel.conf`.

---

## Requirements

- **Export host**: macOS with Apple Photos and [osxphotos](https://github.com/RhetTbull/osxphotos)
  **0.77.1 or later** — earlier releases cannot read a macOS 27 Photos library
  ([osxphotos#2221](https://github.com/RhetTbull/osxphotos/issues/2221)).
  Tested on macOS 26.7 and macOS 27.0. Upgrading an existing install across
  a major macOS release? Read [docs/upgrading-macos.md](docs/upgrading-macos.md) first.
- **Processing host**: Python 3.11+, [Ollama](https://ollama.com) with a Gemma 4 vision model
- **Recommended model**: `gemma4:12b-it-q8_0` (GPU); `gemma4:e4b` works on CPU
- **Video enrichment (optional)**: `ffmpeg`/`ffprobe` on PATH, plus a
  vision-language model such as `qwen3-vl:8b`

```bash
pip install -r requirements.txt
ollama pull gemma4:12b-it-q8_0

# optional, for Phase 2b video enrichment
ollama pull qwen3-vl:8b
sudo apt install ffmpeg        # macOS: brew install ffmpeg
```

---

## Configuration

```bash
cp photo-intel.conf.example photo-intel.conf
# Edit photo-intel.conf for your paths, hostnames, and Ollama model
```

Key settings:

| Setting | Description |
|---------|-------------|
| `[general] mode` | `local` or `split` |
| `[paths] library` | Path to your `.photoslibrary` bundle |
| `[paths] staging_dir` | Persistent export staging directory — **never clear this** |
| `[paths] dest_dir` | Where exported photos land on the processing host |
| `[paths] db_path` | SQLite database path on the processing host |
| `[ollama] model` | Gemma model for Phase 2 enrichment and Smart Search |
| `[video] model` | Vision-language model for Phase 2b video enrichment |
| `[video] max_minutes` | Wall-clock stop for a video run — keep below the unit's `TimeoutStartSec` |
| `[web] delete_token` | Shared secret for the photo-delete endpoint (blank to disable) |

The `[video]` section is optional; omit it entirely if you only care about
still images.

---

## Running

### Export (Mac)

Day to day you run the gated launcher, which does the gate → export → promote
sequence described above:

```bash
sh scripts/run_export.sh
```

The underlying pieces, if you want to drive them by hand:

```bash
# full sweep, all year windows (this is what the weekly backstop runs)
python3 src/photo_intel_export.py
# single year window only
python3 src/photo_intel_export.py --window 2024

# what would the gate flag right now?
python3 src/manifest_gate.py --changed-out /tmp/changed.tsv \
                             --manifest-out /tmp/manifest.tsv
# export just those
python3 src/photo_intel_export.py --changed-file /tmp/changed.tsv
```

Schedule both jobs with the launchd plists in `deploy/macos/launchd/`:
`com.photo-intel.export` (gated, every 2 h) and `com.photo-intel.export-full`
(full sweep, weekly), plus `com.photo-intel.export-watchdog`, which alerts when
the export stops running or wedges on its lock. The scripts in `scripts/` —
including `photo_intel_lib.sh`, which the launchers source — are expected to sit
alongside the Python in your install directory; see the plists for the layout.

The shared `.export.lock` records its owner (`pid timestamp label`), and a
launcher reclaims it when that process is gone. Earlier versions never did, so
one hung or killed export blocked every later run until the lock was removed by
hand. Set `PHOTO_INTEL_NOTIFY_CMD` in the watchdog plist to a script that
delivers alerts (subject as `$1`, body on stdin); without it they only reach
`watchdog.log`.

**First run:** seed a baseline manifest before enabling the gated job,
otherwise the first gated run flags your entire library:

```bash
python3 src/manifest_gate.py --manifest-out manifest/manifest.tsv \
                             --changed-out /dev/null
```

### Phase 1 — ingest sidecars (processing host)

```bash
python3 src/photo_intel_phase1.py --once
```

Schedule with the systemd units in `deploy/linux/systemd/`.

### Phase 2 — AI enrichment (processing host)

```bash
python3 src/photo_intel_phase2.py
# test run:
python3 src/photo_intel_phase2.py --limit 10 --dry-run
```

### Phase 2b — video enrichment (optional, processing host)

Phase 2 handles still images only; it parks every video row at "skip".
`photo_intel_video.py` fills that gap: it samples frames from each clip, sends
the ordered frameset to a vision-language model (Qwen-VL) via Ollama, and
writes the description, tags and location guess back into the **same** columns
Phase 2 uses — so videos become searchable with no schema change and no web-app
change.

```bash
python3 src/photo_intel_video.py
# test batch:
python3 src/photo_intel_video.py --limit 20 --dry-run
```

Requires `ffmpeg`/`ffprobe` on PATH and a `[video]` section in
`photo-intel.conf`. Schedule with `photo-intel-video.{service,timer}` in
`deploy/linux/systemd/system/`; check progress with `scripts/video_status.sh`.

It runs as a separate script rather than a Phase 2 flag because the VLM has
different VRAM behaviour and a different output profile than the Gemma model,
and coupling them would risk the still-image JSON stability. On a single-GPU
host the script unloads the Gemma model, runs, then warms Gemma back — always,
including on failure — so the next Phase 2 run hits a warm model.

Two settings are coupled: `[video] max_minutes` is a self-imposed wall-clock
stop, and it **must** stay below `TimeoutStartSec` in the service unit. If
systemd kills the run first it does so with SIGKILL, which skips the warm-back
and leaves the unit failed. Change one, change the other.

### Place names (recommended)

Phase 2 and 2b are handed the photo's GPS coordinates and asked, among other
things, to name the place. They are bad at it. A ballpark 50 miles outside a
capital city gets confidently labelled with the capital's famous stadium — at
"100% confidence", because the model is guessing from a landmark it recognises
rather than from the coordinates it was given.

The OS already knows the answer. Apple Photos reverse-geocodes every asset and
`osxphotos` exposes it, down to the venue (`place.name.area_of_interest`), so
the fix is to stop asking the model and read the metadata:

```bash
# on the Mac — dump uuid -> place for the whole library, rsync to the host
sh scripts/run_places.sh

# incremental: only assets added in the last 5h, merged into the existing file
sh scripts/run_places.sh 5h

# on the processing host — write the place_* columns
python3 src/photo_intel_places.py --config photo-intel.conf \
    --apply /srv/photo-intel/places.json
```

This adds eight `place_*` columns. To make them searchable, `photos_fts` must
be rebuilt — it is an external-content FTS5 table, so adding a column means
drop + recreate + `'rebuild'` + recreate the three sync triggers. There is no
`ALTER` for FTS5 columns:

```bash
sqlite3 photo-intel.db < migrations/2026-08-01-fts-add-place-name.sql
```

**Ordering is the part that bites.** A dump must land *before* the Phase 2
sweep that will consume the newly imported photos. A sweep that runs first
describes them from bare GPS, and because a row is never revisited once
`phase2_processed = 1`, that wrong description is permanent — along with the
GPU time that produced it. Run a dump ~30 min ahead of each sweep and an apply
~10 min ahead; the shipped unit files show one such arrangement.

Two rules that are easy to get wrong:

- **Incremental dumps merge, they do not replace.** `--apply` is a full
  re-apply of whatever file it is given, so a replacing incremental would
  leave a failed earlier apply unrecoverable — the later file would hold only
  that window's handful of photos. Merging keeps `places.json` complete, so
  every apply is idempotent and any one covers for an earlier failure.
- **Keep `places.json` out of a temp directory.** A merge whose base file has
  been pruned silently produces a partial file, and rsync ships it over the
  complete one. The database is safe either way (`--apply` only updates rows
  it matches and never clears a place), but the fallback property is not.

A daily full dump is still worth running alongside the incrementals:
`--added-in-last` only sees newly *added* assets, so a photo the OS
re-geocodes later would never show up in an incremental.

Photos with no GPS keep the old model guess as a fallback, so nothing is lost
for the parts of a library that predate geotagging.

#### Filling the gaps: `--nearby-fallback`

`--apply` cannot place every GPS row, for two different reasons that look
identical in the database:

1. **The photo is no longer in the Photos library** — culled as a
   near-duplicate, say. The dump reads the library, so there is nothing to
   carry for it, while the exported file and its row live on.
2. **The OS geotagged it but never reverse-geocoded it.** `osxphotos` returns
   an empty place for these, and the dump correctly drops them.

In both cases a neighbour usually knows the answer, because photos taken
seconds apart at one spot are common and at least one normally carries a
resolved place:

```bash
python3 src/photo_intel_places.py --config photo-intel.conf \
    --apply /srv/photo-intel/places.json --nearby-fallback
# widen if your library is sparse (default 100m):
#   --nearby-radius 250
```

On the library this was built against it took the gap from 245 rows to 8 —
the leftovers being places with nothing else photographed within 100m.

Three properties worth preserving if you modify it:

- **Borrowed rows are marked `place_source='nearby'`, never `'apple'`.** An
  inferred place must not be mistaken for an authoritative one, and it makes
  the whole thing undoable with a single `UPDATE … WHERE
  place_source='nearby'`.
- **It runs after `--apply`, never before**, so it only fills what is
  genuinely still empty. Since `--apply` writes `place_source='apple'`
  unconditionally, a row that later gets a real place overwrites the borrowed
  one — the fallback can fill a hole but never hold one open.
- **Keep the radius tight.** At a few hundred metres you start borrowing
  across venue boundaries and confidently mislabelling; a handful of
  unresolved rows is the better failure.

No FTS rebuild is needed — the sync triggers fire on `UPDATE`, so borrowed
rows are searchable immediately.

### Web app

```bash
python3 src/photo_intel_web.py
# open http://localhost:5052
```

### Thumbnail pre-warm (optional)

```bash
python3 src/photo_intel_thumbs.py
```

Generates missing 400px grid thumbnails in a multiprocessing pool. Idempotent; run nightly via the provided user-level systemd timer.

---

## Web app features

- **Voice** — speak a search query; Ollama parses it into filters; results are read back via speech synthesis. Chrome requires HTTPS for the microphone; Safari works on plain HTTP.
- **Search** — FTS5 full-text search across AI descriptions, tags, people, and location guesses. Smart Search (Ollama-backed) expands queries with synonyms and parses natural-language filters (year ranges, multiple people, scenes).
- **Library Stats** — photo counts by year, top scenes, most-photographed people. Bars are clickable and jump to a filtered search.
- **Map** — clustered GPS markers (Leaflet + MarkerCluster). Click a cluster to browse photos from that location in a side panel.
- **Video support** — videos appear in search results with ffmpeg-extracted poster thumbnails and stream in a `<video>` element with Range/seek support.
- **Share** — a button in the photo view hands the photo to the OS share sheet (Messages, Mail, AirDrop, Save to Photos). Photos go out as an upright 2048-px JPEG — HEIC is converted, so non-Apple recipients can open it — named by date (`2019-08-24-175034.jpg`) instead of a uuid; videos go out as the original file. The share sheet needs a secure origin (HTTPS or `localhost`) and in practice a phone; elsewhere the button downloads the file instead.

---

## Gotchas

**Never clear the staging directory.** It is the osxphotos `--update` anchor. Clearing it forces a full re-export of the entire library on the next run.

**The staging volume must stay mounted.** The export script calls `assert_volume_mounted()` and aborts before touching anything if the volume is absent. A missing volume is a clean failure, not a silent full-reexport.

**The export holds originals, not your edits.** `--skip-edited` means an edited
photo exports as its unedited original. Don't treat the exported tree as a
complete stand-in for the library — see *What it is not* above.

**Live Photos export two files per UUID.** A `.heic` still and a `.mov` clip share the same UUID stem. The pipeline always selects the still via `pick_for_index()`. If thumbnails render blank, delete the stale `.thumb_cache/<uuid>_*.jpg` entry to force regeneration.

**Chrome requires HTTPS for the Voice tab.** The Web Speech API is only available on secure origins in Chrome. Serving through a reverse proxy (nginx, Caddy) with a local cert resolves this.

**Smart Search has a runtime Ollama dependency.** If Ollama is unreachable or the model is loading, Smart Search falls back to a plain FTS5 keyword search automatically.

**`format="json"` degenerates Gemma.** Under grammar-constrained decoding, Gemma enters a repeating-whitespace loop when it wants a grammar-disallowed token. Phase 2 and Smart Search both parse JSON tolerantly without the `format` constraint, with `repair_json_quotes()` as a fallback for unescaped interior quotes.

**Seed the manifest before enabling the gated export.** With no baseline, the
first gated run flags the entire library as changed and exports all of it. Run
`manifest_gate.py --manifest-out manifest/manifest.tsv --changed-out /dev/null`
once first. Re-seeding mid-life has the same trap in reverse: the export DB can
lag the fresh manifest, which is one of the cases the weekly full sweep exists
to catch.

**Don't put `Requires=` on a systemd `.timer`.** A timer already triggers its
matching service through the implicit `Unit=` on the schedule. Adding
`Requires=` (or `Wants=`) to the timer's `[Unit]` *also* starts the service the
moment the timer is started, enabled, or booted — so every edit-and-restart
fires an unscheduled run. The units here deliberately omit it.

**`[video] max_minutes` and `TimeoutStartSec` move together.** The video script
self-stops at `max_minutes` and warms the Gemma model back in a `finally:`
block. If the unit's `TimeoutStartSec` is lower, systemd SIGKILLs the run first
— the warm-back never happens and the unit is left `failed (timeout)`.

**macOS ulimit and exiftool.** Default macOS `ulimit -n` is 256. With `--exiftool`, osxphotos forks one exiftool process per photo and hits `Too many open files` at scale. osxphotos 0.77.x also holds a `Photos.sqlite` + `-wal` descriptor pair open per photo and reclaims them only intermittently, so even 4096 fails thousands of photos in a large year window. `photo_intel_export.py` raises the soft limit to 32768 itself; use `ulimit -n 32768` for a manual `osxphotos export`.

---

## Legacy pipeline

The original `Photos.sqlite`-coupled pipeline (Phase 1 / Phase 2 / web UI, macOS-only, launchd-scheduled) is preserved in `legacy/`. It is no longer actively developed but is kept for reference and for single-Mac setups that prefer not to use the export-driven architecture.

The `v1-legacy` git tag marks the last commit before the v2 restructure.
