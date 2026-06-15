# Photo Intelligence

A local-first photo enrichment pipeline for Apple Photos libraries. Exports your library via [osxphotos](https://github.com/RhetTbull/osxphotos), enriches each photo with AI-generated descriptions using a locally-running Gemma vision model (via [Ollama](https://ollama.com)), and serves the results through a web app with full-text search, Smart Search, voice input, a map view, and library stats.

No cloud services. No API keys. Your photos stay on your hardware.

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
  photo_intel_export.py   — osxphotos export, chunked by year → staging/
                            rsync staging/ → processing host (split mode)

Processing host (Mac in local mode, or Linux/GPU in split mode)
  photo_intel_phase1.py   — ingest sidecar JSON → photo-intel.db
  photo_intel_phase2.py   — Gemma enrichment via Ollama → photo-intel.db
  photo_intel_web.py      — Flask web app, port 5052
  photo_intel_thumbs.py   — nightly thumbnail pre-warm (optional)
```

### Deployment modes

| Mode | Export | Phase 1/2 + web app | When to use |
|------|--------|---------------------|-------------|
| `local` | Mac | Same Mac | Single machine, CPU-only |
| `split` | Mac | Separate Linux/GPU host | Dedicated GPU machine for processing |

Set `mode = local` or `mode = split` in `photo-intel.conf`.

---

## Requirements

- **Export host**: macOS with Apple Photos and [osxphotos](https://github.com/RhetTbull/osxphotos)
- **Processing host**: Python 3.11+, [Ollama](https://ollama.com) with a Gemma 4 vision model
- **Recommended model**: `gemma4:12b-it-q8_0` (GPU); `gemma4:e4b` works on CPU

```bash
pip install -r requirements.txt
ollama pull gemma4:12b-it-q8_0
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
| `[web] delete_token` | Shared secret for the photo-delete endpoint (blank to disable) |

---

## Running

### Export (Mac)

```bash
python3 src/photo_intel_export.py
# single year window only:
python3 src/photo_intel_export.py --window 2024
```

Schedule with the provided launchd plist in `deploy/minim2/launchd/`.

### Phase 1 — ingest sidecars (processing host)

```bash
python3 src/photo_intel_phase1.py --once
```

Schedule with the systemd units in `deploy/lmstudio/systemd/`.

### Phase 2 — AI enrichment (processing host)

```bash
python3 src/photo_intel_phase2.py
# test run:
python3 src/photo_intel_phase2.py --limit 10 --dry-run
```

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

---

## Gotchas

**Never clear the staging directory.** It is the osxphotos `--update` anchor. Clearing it forces a full re-export of the entire library on the next run.

**The staging volume must stay mounted.** The export script calls `assert_volume_mounted()` and aborts before touching anything if the volume is absent. A missing volume is a clean failure, not a silent full-reexport.

**Live Photos export two files per UUID.** A `.heic` still and a `.mov` clip share the same UUID stem. The pipeline always selects the still via `pick_for_index()`. If thumbnails render blank, delete the stale `.thumb_cache/<uuid>_*.jpg` entry to force regeneration.

**Chrome requires HTTPS for the Voice tab.** The Web Speech API is only available on secure origins in Chrome. Serving through a reverse proxy (nginx, Caddy) with a local cert resolves this.

**Smart Search has a runtime Ollama dependency.** If Ollama is unreachable or the model is loading, Smart Search falls back to a plain FTS5 keyword search automatically.

**`format="json"` degenerates Gemma.** Under grammar-constrained decoding, Gemma enters a repeating-whitespace loop when it wants a grammar-disallowed token. Phase 2 and Smart Search both parse JSON tolerantly without the `format` constraint, with `repair_json_quotes()` as a fallback for unescaped interior quotes.

**macOS ulimit and exiftool.** Default macOS `ulimit -n` is 256. With `--exiftool`, osxphotos forks one exiftool process per photo and hits `Too many open files` at scale. Raise to `ulimit -n 4096` before running the export.

---

## Legacy pipeline

The original `Photos.sqlite`-coupled pipeline (Phase 1 / Phase 2 / web UI, macOS-only, launchd-scheduled) is preserved in `legacy/`. It is no longer actively developed but is kept for reference and for single-Mac setups that prefer not to use the export-driven architecture.

The `v1-legacy` git tag marks the last commit before the v2 restructure.
