#!/bin/zsh
# run_phase2.sh — Run Gemma photo enrichment continuously until all photos are done.
# Launched by launchd; runs once, processes everything, exits when complete.
# Logs go to phase2.log in the repo root (also captured by launchd plist).

set -e

# Resolve repo root from this script's location.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── EDIT THESE ───────────────────────────────────────────────────────────────
DB_PATH="$HOME/photos_meta.db"
MODEL="gemma4:e4b"
# ─────────────────────────────────────────────────────────────────────────────

source "$REPO_ROOT/venv/bin/activate"
cd "$REPO_ROOT"

python3 src/phase2_gemma.py \
    --db "$DB_PATH" \
    --model "$MODEL"
