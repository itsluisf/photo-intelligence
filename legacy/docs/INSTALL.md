# Installation

## Prerequisites

- macOS on Apple Silicon (Intel may work; untested)
- Homebrew
- Python 3.13 or 3.14
- ~20GB free disk for Ollama + model

## 1. Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Versions in `requirements.txt` are pinned for a reason — see [GOTCHAS.md](GOTCHAS.md).

## 2. Ollama

**Install version 0.20.5 specifically.** Newer versions break `gemma4:e4b`.

```bash
# If you have a newer version installed:
brew uninstall ollama

# Download 0.20.5 from:
# https://github.com/ollama/ollama/releases/tag/v0.20.5

# Verify:
ollama --version  # should report 0.20.5

# Pull the model
ollama pull gemma4:e4b
```

Start the Ollama server (it usually runs as a background service after install):

```bash
ollama serve
```

## 3. Configure

```bash
cp config.example.ini config.ini
```

Edit `config.ini` and set:

- `PHOTOS_LIBRARY` — path to your `.photoslibrary` bundle
- `DB_PATH` — where to put `photos_meta.db` (recommend the same volume as the library)
- `OLLAMA_MODEL` — defaults to `gemma4:e4b`

## 4. First run

Phase 1 is one-time and fast:

```bash
python src/phase1_extract.py
```

Phase 2 is long-running. Run it in the foreground first to confirm it works:

```bash
python src/phase2_gemma.py
```

You should see log lines for each photo. Ctrl-C to stop; it's resumable.

## 5. (Optional) Run Phase 2 continuously

See [launchd/README.md](../launchd/README.md) for the macOS background-service setup.
