# Set up Photo Intelligence (for Claude Code / Cowork)

**Hand this file to Claude.** Drop it into a Claude Code or Cowork session and say
*"set this up for me."* Claude: you are the installer. Read this whole file first,
then walk the user through standing up the **Photo Intelligence** pipeline on their
machine. Interview them where it says to, run the commands, verify each stage before
moving on, and explain what's happening as you go. Don't dump every command at once —
do it stage by stage and confirm each works.

---

## What this builds

A **local-first** AI search engine over the user's Apple Photos library. No cloud, no
API keys — every photo and every model stays on their hardware.

```
Apple Photos  ──osxphotos export──▶  staging/  ──▶  Phase 1 (ingest sidecar JSON)
                                                        │
                                                        ▼
                                              photo-intel.db  ◀── Phase 2 (Gemma
                                                        │          vision model
                                                        ▼          via Ollama:
                                              Flask web app          descriptions,
                                              (search, voice,        tags, OCR,
                                               map, stats)           location guess)
```

The end result is a web app (default port **5052**) with full-text search, an
Ollama-backed natural-language "Smart Search," a voice tab, a map of GPS-tagged
photos, and library stats.

**Source repo:** <https://github.com/itsluisf/photo-intelligence>
Everything below installs from that repo. The repo's `README.md` is the reference;
this file is the guided-install script.

---

## Step 0 — Interview the user first

Before touching anything, ask these and record the answers. Don't assume.

1. **Deployment mode.**
   - **`local`** — one Mac does everything (export + AI processing + web app).
     Simplest. AI enrichment runs on that Mac's CPU/GPU. On an Apple Silicon Mac
     this is fine; on CPU-only it's slow (~10–13 s/photo → a big library is a
     multi-day background job).
   - **`split`** — the Mac exports photos and rsyncs them to a **separate
     Linux/GPU box** that runs Phase 1/2 + the web app. Use this only if they
     actually have a second machine with a GPU. More moving parts (SSH, rsync).

   **Recommend `local` unless they explicitly have a GPU machine to offload to.**
   Most friends should start local.

2. **Library size.** Roughly how many photos? Sets expectations for the initial
   enrichment run (the slow part). Tell them: the *export + Phase 1* part is fast;
   *Phase 2* (the AI descriptions) is the long pole and can run unattended over days.

3. **Where is the Photos library?** Usually
   `~/Pictures/Photos Library.photoslibrary`. Confirm the path exists.

4. **Staging volume.** osxphotos needs a **persistent** staging directory that is
   never cleared (it's the `--update` anchor — clearing it forces a full re-export
   of the entire library). Can be on the internal disk or an external drive that
   **stays mounted**. Pick a path like `~/photo-intel/staging` (local) or a path on
   an always-mounted external drive.

5. **(split mode only)** The Linux host's SSH address, username, an SSH key that
   works passwordless, and the destination paths on that host.

---

## Step 1 — Prerequisites

Install what's missing. Check each before installing.

### macOS (export host — required in both modes)

```bash
# Homebrew (if not present): https://brew.sh
brew --version || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.11+ and Ollama
brew install python ollama

# osxphotos — the Apple Photos exporter (macOS only)
#   Prefer a pipx/global install so it's on PATH for scheduled jobs.
brew install pipx && pipx ensurepath && pipx install osxphotos
osxphotos --version
```

> **First-run permissions:** the very first `osxphotos` command will trigger a macOS
> prompt to grant the terminal **Full Disk Access** / Photos access. The user must
> approve it in System Settings → Privacy & Security, then re-run.

### Ollama + the vision model (on whichever host runs Phase 2)

```bash
# Start the Ollama service (macOS: `brew services start ollama` or just `ollama serve`)
ollama serve &        # or: brew services start ollama

# Pull the vision model. The repo default is gemma4:12b-it-q8_0 (~12 GB, needs a
# capable GPU or Apple Silicon with enough RAM). On a leaner machine, use a smaller
# Gemma vision variant and set it in the config in Step 3.
ollama pull gemma4:12b-it-q8_0
```

> **Model sizing — ask before pulling.** `gemma4:12b-it-q8_0` is the user's tuned
> default and wants ~16 GB+ of VRAM/unified memory. If the user's machine is
> smaller, pull a smaller variant instead (e.g. `gemma4:e4b` / a 4B-class vision
> model) and put that name in `[ollama] model` in Step 3. Confirm the chosen model
> actually loads (`ollama run <model> "hi"`) before relying on it.

### Linux (split mode — processing host only)

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg rsync
# Install Ollama: https://ollama.com/download  (curl -fsSL https://ollama.com/install.sh | sh)
ollama pull gemma4:12b-it-q8_0
```
`ffmpeg` is needed for video poster thumbnails. HEIC decoding on Linux comes from
the `pillow-heif` Python package (installed in Step 2).

---

## Step 2 — Clone and create the Python environment

Pick a working directory (e.g. `~/photo-intel`). In **local** mode everything lives
here; in **split** mode the repo goes on *both* hosts (the Mac runs the export
script, the Linux box runs the rest).

```bash
git clone https://github.com/itsluisf/photo-intelligence.git ~/photo-intel-src
cd ~/photo-intel-src

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` covers Phase 1/2 and the web app (`pillow`, `pillow-heif`,
`ollama`, `Flask`, `waitress`, `reverse_geocoder`). **osxphotos is not in it** — it's
macOS-only and already installed via pipx in Step 1.

---

## Step 3 — Configuration

Copy the example config and edit it for the user's answers from Step 0.

```bash
cp photo-intel.conf.example photo-intel.conf
```

Fill in `photo-intel.conf`:

- `[general] mode` → `local` or `split`.
- `[paths] library` → the `.photoslibrary` path from Step 0.
- `[paths] staging_dir` → the persistent staging path. **Never cleared.**
- `[paths] exportdb` → osxphotos state DB, kept **outside** staging
  (e.g. `~/photo-intel/osxphotos_export.db`).
- `[paths] dest_dir` → where exported photos land. **Local mode:** same machine
  (e.g. `~/photo-intel/photos`). **Split mode:** the path on the Linux box.
- `[paths] db_path` → `~/photo-intel/photo-intel.db` (on the processing host).
- `[transfer]` → **local mode: leave `rsync_dest` blank.** Split mode: set
  `rsync_dest` (`user@host:/path/to/photos`) and `ssh_key`.
- `[ollama] url` → `http://localhost:11434` and `model` → the model pulled in Step 1.
- `[export] windows` → leave the year list as-is; add the current year each January.
- `[phase2]` → defaults are fine (`num_predict = 1024`). Lower it if using a
  smaller model.
- `[web] delete_token` → generate one so the photo-delete endpoint is usable but
  protected:
  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```
  Paste the result in. Leave blank to disable deletion entirely (safe default).

---

## Step 4 — Export from Apple Photos (Mac)

Raise the file-descriptor limit first — osxphotos with `--exiftool` forks one
exiftool per photo and macOS defaults to a low `ulimit` (`Too many open files`):

```bash
ulimit -n 4096
```

Then run the export. In local mode this populates `dest_dir`; in split mode it
stages locally and rsyncs to the Linux host.

```bash
cd ~/photo-intel-src
source venv/bin/activate
python3 src/photo_intel_export.py --config ~/photo-intel/photo-intel.conf
```

What to expect:
- The **first** run exports the whole library, chunked by year window — this takes a
  while and uses disk in `staging_dir`. Subsequent runs are incremental (`--update`,
  only new/changed photos).
- **Safety rule baked in:** the export uses `--not-shared`, so shared-album photos
  never leave the Mac. Don't remove that flag.

---

## Step 5 — Phase 1: ingest (processing host)

Builds/updates `photo-intel.db` from the per-photo JSON sidecars. Fast, idempotent.

```bash
python3 src/photo_intel_phase1.py --once --config ~/photo-intel/photo-intel.conf
```

Verify rows landed:
```bash
sqlite3 ~/photo-intel/photo-intel.db "SELECT COUNT(*) FROM photos;"
```

---

## Step 6 — Phase 2: AI enrichment (processing host)

This is the slow, valuable part: each image goes to the Gemma vision model and gets a
description, tags, a location guess, and OCR'd text written back to the DB. Make sure
Ollama is running and the model is pulled (Step 1).

```bash
python3 src/photo_intel_phase2.py --config ~/photo-intel/photo-intel.conf
```

Tell the user:
- It processes everything with `phase2_processed = 0` and **resumes** where it left
  off if interrupted — safe to stop/restart, run overnight, etc.
- Throughput depends entirely on the GPU. On Apple Silicon or a real GPU it's
  tolerable; CPU-only is slow. For a big library, kick it off and let it run in the
  background for hours/days.
- Videos are skipped for descriptions (they match on people/date only).

---

## Step 7 — Launch the web app (processing host)

```bash
python3 src/photo_intel_web.py --config ~/photo-intel/photo-intel.conf
```

Then open **http://localhost:5052** (or `http://<host>:5052` in split mode).

Verify, don't just assume:
- The grid loads and shows photos.
- **Search** returns results for a word you know is in the library (a person's name,
  a place, "beach").
- **Smart Search** (✨ toggle) parses a natural query — this calls Ollama live, so it
  confirms the model path works end to end.
- **Map** shows pins if any photos have GPS.

> **Voice tab note:** the browser Web Speech API needs HTTPS in Chrome (works on
> plain HTTP in Safari). Over `localhost` it's fine; if they later put it behind a
> reverse proxy, give it TLS for voice-in-Chrome to work.

---

## Step 8 — (Optional) Run it unattended

Once the manual runs work, automate the cycle so the DB stays current with the
library. The repo ships ready-made unit files under `deploy/` with **placeholder
paths and usernames (`youruser`, `/home/youruser`, `/your/media/drive`) that must
be edited** for the user's machine before installing:

- `deploy/macos/launchd/` — macOS launchd plist for the export job.
- `deploy/linux/systemd/` — Linux systemd service+timer units for Phase 1,
  Phase 2, the web app, and the nightly thumbnail pre-warm.

Help the user adapt these to their paths/user, or just set up simple `cron`/launchd
entries that call the three scripts on a schedule (export → phase1 → phase2, offset
so they don't overlap). The web app should run as a long-lived service.

Recommended cadence to suggest: export + Phase 1 a few times a day; Phase 2 on a
daytime window; web app always on; thumbnail pre-warm nightly.

---

## Gotchas to warn the user about

- **Never clear the staging directory.** It's the osxphotos `--update` anchor.
  Emptying it makes the next export re-export the *entire* library. Keep it on a
  volume that stays mounted.
- **The pipeline is additive-only.** Deleting a photo in Apple Photos does **not**
  remove it downstream — it lingers in staging, on the processing host, and in the
  DB. There's no auto-prune by design (Apple Photos is treated as a one-way source).
- **Shared-album photos stay on the Mac** (`--not-shared`). Don't disable that — and
  the web app's delete button only removes the *processing host's* copy, never Apple
  Photos.
- **macOS `ulimit -n`** must be raised (Step 4) or `--exiftool` hits "Too many open
  files."
- **HEIC on Linux** needs `pillow-heif` (in `requirements.txt`); macOS handles HEIC
  natively.
- **Model swaps require care.** The defaults (`num_predict`, model name) are tuned
  for `gemma4:12b`. A different model may need different settings — validate output
  quality on a small batch first.

---

## If something breaks

- **Smart Search / voice fails but plain search works** → Ollama is down or the model
  is cold. Check `ollama list` and `ollama ps`; the app falls back to keyword search
  on its own.
- **Web app 500s on every photo** → the photos directory / DB path isn't reachable
  (unmounted drive in split setups). Check `db_path` and `dest_dir` exist.
- **Export re-exports everything every run** → staging was cleared or the volume
  wasn't mounted. See the staging gotcha.
- **Blank image panels for some photos** → Live Photos export a `.mov` + a still on
  the same name; the current code prefers the still. If you see this on an old build,
  pull latest.

When in doubt, read the repo's `README.md` and the comments in each script — they
document the failure modes that were hit in production.
