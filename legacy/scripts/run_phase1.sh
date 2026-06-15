#!/bin/zsh
# run_phase1.sh — One-time metadata extraction from Apple Photos library.
#
# Edit PHOTOS_LIBRARY and DB_PATH below to point at your library and desired
# output DB location. Activates the venv at ../venv/ relative to this script.

set -e

# Resolve repo root from this script's location.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── EDIT THESE ───────────────────────────────────────────────────────────────
PHOTOS_LIBRARY="$HOME/Pictures/Photos Library.photoslibrary"
DB_PATH="$HOME/photos_meta.db"
# If your library lives on an external volume, set TMP_DIR to a path on that
# same volume. Otherwise leave commented to use the system default temp dir.
# TMP_DIR="/Volumes/MySSD/.photosdb_tmp"
# ─────────────────────────────────────────────────────────────────────────────

source "$REPO_ROOT/venv/bin/activate"
cd "$REPO_ROOT"

EXTRA_ARGS=()
if [[ -n "${TMP_DIR:-}" ]]; then
  EXTRA_ARGS+=(--tmp-dir "$TMP_DIR")
fi

python3 src/phase1_build_db.py \
  --library "$PHOTOS_LIBRARY" \
  --db "$DB_PATH" \
  --resume \
  --photos-only \
  "${EXTRA_ARGS[@]}"
