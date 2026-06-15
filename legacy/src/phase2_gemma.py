#!/usr/bin/env python3
"""
phase2_gemma.py — Phase 2: Enrich photo DB with Gemma 4 via Ollama

Reads photos from the DB built by phase1_build_db.py, sends each image
to Gemma 4 running locally in Ollama, and writes structured descriptions
back to the DB. Designed to be resumable — safe to kill and restart.

Prerequisites:
    pip3 install ollama tqdm pillow
    ollama pull gemma4          # 9.6GB — or use gemma4:e2b for smaller/faster

Usage:
    python3 phase2_gemma.py
    python3 phase2_gemma.py --db ~/photos_meta.db
    python3 phase2_gemma.py --model gemma4:e2b --workers 2
    python3 phase2_gemma.py --limit 50             # test run
    python3 phase2_gemma.py --reprocess-errors     # retry failed photos
    python3 phase2_gemma.py --photos-only          # skip videos
    python3 phase2_gemma.py --min-year 2020        # only recent photos
"""

import sqlite3
import argparse
import json
import base64
import time
import traceback
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        def __init__(self, iterable=None, total=None, **kw):
            self._it = iterable; self.n = 0; self._total = total
        def __iter__(self):
            for x in self._it:
                yield x
        def update(self, n=1): self.n += n
        def set_postfix(self, **kw): pass
        def close(self): pass

try:
    import ollama as _ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False
    print("ERROR: ollama package not installed. Run: pip3 install ollama")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT DESIGN
# Returns structured JSON so we can store discrete fields, not just free text.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a photo metadata assistant. Analyze photos and respond ONLY with valid JSON.
Never include markdown, code blocks, or explanatory text — raw JSON only.
Be concise. Confidence values are 0.0–1.0. Unknown values use null."""

def build_prompt(photo: dict) -> str:
    """Build context-aware prompt using what we already know."""
    context_parts = []

    if photo.get("date_created"):
        context_parts.append(f"Date taken: {photo['date_created']}")
    if photo.get("camera_make") and photo.get("camera_model"):
        context_parts.append(f"Camera: {photo['camera_make']} {photo['camera_model']}")
    if photo.get("latitude") and photo.get("longitude"):
        context_parts.append(f"GPS coordinates: {photo['latitude']:.4f}, {photo['longitude']:.4f}")
    if photo.get("named_people"):
        try:
            people = json.loads(photo["named_people"])
            if people:
                context_parts.append(f"People identified by owner: {', '.join(people)}")
        except Exception:
            pass
    if photo.get("vision_classifications"):
        try:
            classes = json.loads(photo["vision_classifications"])
            top = [c["label"] for c in classes[:5]]
            context_parts.append(f"Apple Vision scene labels: {', '.join(top)}")
        except Exception:
            pass

    context = "\n".join(context_parts)

    return f"""Analyze this photo. I already know the following:
{context if context else "(no prior metadata)"}

Respond with ONLY this JSON structure, filling in what you can determine from the image:
{{
  "description": "2-3 sentence natural language description of what is shown",
  "scene_type": "one of: landscape, cityscape, portrait, group, architecture, food, indoor, event, wildlife, macro, abstract, document, other",
  "setting": "specific environment: beach, forest, mountain, city_street, restaurant, home_interior, airport, museum, park, desert, ocean, farm, office, other",
  "primary_subject": "the main focus of the photo in 3-5 words",
  "mood": "one of: joyful, calm, dramatic, melancholy, energetic, formal, candid, other",
  "estimated_location": {{
    "country": null,
    "region_or_city": null,
    "specific_place": null,
    "confidence": 0.0,
    "reasoning": "brief explanation of location clues in the image"
  }},
  "people": {{
    "count": 0,
    "age_groups": [],
    "activity": null
  }},
  "tags": ["tag1", "tag2"],
  "text_in_image": null,
  "time_of_day": "one of: dawn, morning, midday, afternoon, evening, night, indoor, unknown",
  "weather": "one of: sunny, cloudy, overcast, rainy, foggy, snowy, unknown, indoor",
  "notable_features": [],
  "confidence_overall": 0.0
}}"""

# ─────────────────────────────────────────────────────────────────────────────
# IMAGE ENCODING
# ─────────────────────────────────────────────────────────────────────────────

MAX_PIXELS = 2_000_000   # ~1.4k x 1.4k — sufficient for Gemma 4, saves tokens

def encode_image(file_path: Path) -> str | None:
    """
    Load image, resize if needed, return base64 JPEG string.
    Handles HEIC via sips fallback on macOS.
    """
    import tempfile, subprocess, os

    path = file_path
    suffix = file_path.suffix.lower()

    # Convert HEIC to JPEG via sips (macOS built-in)
    if suffix in [".heic", ".heif"]:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        try:
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                 str(file_path), "--out", tmp.name],
                capture_output=True, timeout=30
            )
            if result.returncode == 0:
                path = Path(tmp.name)
            else:
                os.unlink(tmp.name)
                return None
        except Exception:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            return None

    try:
        from PIL import Image
        import io

        img = Image.open(path)

        # Auto-rotate based on EXIF
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Convert to RGB (handles RGBA, palette, etc.)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Resize if too large
        w, h = img.size
        pixels = w * h
        if pixels > MAX_PIXELS:
            scale = (MAX_PIXELS / pixels) ** 0.5
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

    except Exception as e:
        print(f"  Image encode error ({path.name}): {e}")
        return None
    finally:
        # Clean up temp HEIC conversion
        if suffix in [".heic", ".heif"] and path != file_path:
            try:
                path.unlink()
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CALL
# ─────────────────────────────────────────────────────────────────────────────

def call_gemma(model: str, prompt: str, image_b64: str, timeout: int = 120) -> dict:
    """Send image + prompt to Gemma 4 via Ollama. Returns parsed JSON or {}."""
    try:
        response = _ollama.generate(
            model=model,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            images=[image_b64],
            options={
                "temperature": 0.1,      # Low temp for consistent structured output
                "num_predict": 400,
                # Lower visual token budget for speed (sufficient for scene understanding)
                # Gemma 4 supports: 70, 140, 280, 560, 1120
                "num_ctx": 16384,
            },
            stream=False,
        )
        raw = (response.response if hasattr(response, "response") else response.get("response", "")).strip()

        # Strip markdown fences if model adds them despite instructions
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.strip().startswith("{"):
                    raw = part.strip()
                    break
        raw = raw.strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        raw_resp = response.response if hasattr(response, "response") else response.get("response","")
        return {"_parse_error": str(e), "_raw": raw_resp[:500]}
    except Exception as e:
        return {"_error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_pending(conn, args) -> list:
    """Return photos that need Gemma processing."""
    conditions = ["file_path IS NOT NULL", "kind = 0"]  # photos only

    if not args.reprocess_errors:
        conditions.append("gemma_processed = 0")
        conditions.append("uniform_type != 'com.apple.quicktime-movie'")
    else:
        conditions.append("(gemma_processed = 0 OR (gemma_processed = 2))")  # 2 = error

    if args.min_year:
        conditions.append(f"year >= {args.min_year}")

    if args.max_year:
        conditions.append(f"year <= {args.max_year}")

    where = " AND ".join(conditions)
    order = "ORDER BY date_created DESC"
    limit = f"LIMIT {args.limit}" if args.limit else ""

    sql = f"""
        SELECT id, photos_uuid, file_path, date_created, latitude, longitude,
               camera_make, camera_model, named_people, vision_classifications,
               albums, year
        FROM photos
        WHERE {where}
        {order}
        {limit}
    """
    cur = conn.execute(sql)
    return [dict(row) for row in cur.fetchall()]

def update_photo(conn, photo_id: int, gemma_data: dict, error: str = None):
    """Write Gemma results back to DB."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if error or "_error" in gemma_data or "_parse_error" in gemma_data:
        conn.execute("""
            UPDATE photos SET gemma_processed=2, gemma_processed_at=?,
            gemma_description=? WHERE id=?
        """, (now, error or gemma_data.get("_error","parse error"), photo_id))
        return

    # Extract fields from structured response
    desc = gemma_data.get("description")
    tags = gemma_data.get("tags", [])
    loc = gemma_data.get("estimated_location", {})
    loc_guess = None
    if loc:
        parts = [p for p in [
            loc.get("specific_place"),
            loc.get("region_or_city"),
            loc.get("country")
        ] if p]
        if parts:
            conf = loc.get("confidence", 0)
            loc_guess = f"{', '.join(parts)} (confidence: {conf:.0%})"

    # Merge tags from various fields
    all_tags = list(tags)
    for field in ["scene_type", "setting", "mood", "time_of_day", "weather"]:
        val = gemma_data.get(field)
        if val and val not in ["other", "unknown", "indoor"]:
            all_tags.append(val)

    people_info = gemma_data.get("people", {}) or {}
    try:
        people_count = int(str(people_info.get("count") or 0).split()[0])
    except (ValueError, TypeError):
        people_count = 0
    if people_count > 0:
        all_tags.append(f"people:{people_count}")

    for feat in gemma_data.get("notable_features", []):
        all_tags.append(feat)

    conn.execute("""
        UPDATE photos SET
            gemma_description=?,
            gemma_tags=?,
            gemma_location_guess=?,
            gemma_processed=1,
            gemma_processed_at=?
        WHERE id=?
    """, (
        desc,
        json.dumps(list(dict.fromkeys(all_tags))),  # deduplicate, preserve order
        loc_guess,
        now,
        photo_id
    ))

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def check_ollama(model: str):
    """Verify Ollama is running and model is available."""
    try:
        models = _ollama.list()
        available = [m.model for m in models.models]
        if not any(model in m for m in available):
            print(f"\nWARNING: Model '{model}' not found locally.")
            print(f"Available: {available}")
            print(f"Pull it with: ollama pull {model}\n")
            return False
        return True
    except Exception as e:
        print(f"\nERROR: Cannot connect to Ollama: {e}")
        print("Is Ollama running? Start with: ollama serve")
        return False

def main():
    parser = argparse.ArgumentParser(description="Phase 2: Gemma 4 photo enrichment")
    parser.add_argument("--db", default=str(Path.home() / "photos_meta.db"), help="SQLite DB from phase 1")
    parser.add_argument("--model", default="gemma4", help="Ollama model name (default: gemma4)")
    parser.add_argument("--limit", type=int, help="Process only N photos")
    parser.add_argument("--min-year", type=int, help="Only process photos from this year onwards")
    parser.add_argument("--max-year", type=int, help="Only process photos up to this year")
    parser.add_argument("--reprocess-errors", action="store_true", help="Retry previously errored photos")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed, don't call Ollama")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait between photos (throttle)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"ERROR: DB not found: {db_path}\nRun phase1_build_db.py first.")

    print(f"\nDatabase : {db_path}")
    print(f"Model    : {args.model}")

    if not args.dry_run:
        if not check_ollama(args.model):
            sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    pending = get_pending(conn, args)
    print(f"Pending  : {len(pending)} photos\n")

    if args.dry_run:
        for p in pending[:10]:
            print(f"  {p['file_path']}  ({p['date_created']})")
        if len(pending) > 10:
            print(f"  ... and {len(pending)-10} more")
        conn.close()
        return

    stats = {"ok": 0, "error": 0, "skip": 0}
    start = time.time()
    batch_size = 500  # Re-query DB every N photos to pick up any new additions

    print(f"Running continuously until all photos are processed. Ctrl+C to stop.\n")

    while True:
        # Re-fetch pending photos each iteration so we always have fresh queue
        pending = get_pending(conn, args)
        if not pending:
            break

        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {len(pending):,} photos remaining...")

        batch = pending[:batch_size]
        with tqdm(total=len(batch), unit="photo") as pbar:
            for photo in batch:
                file_path = Path(photo["file_path"])

                if not file_path.exists():
                    stats["skip"] += 1
                    conn.execute("UPDATE photos SET gemma_processed=2, gemma_description='file not found' WHERE id=?",
                                 (photo["id"],))
                    conn.commit()
                    pbar.update(1)
                    continue

                # Encode image
                img_b64 = encode_image(file_path)
                if not img_b64:
                    stats["error"] += 1
                    update_photo(conn, photo["id"], {}, error="image encode failed")
                    conn.commit()
                    pbar.update(1)
                    continue

                # Build prompt with available context
                prompt = build_prompt(photo)

                # Call Gemma
                t0 = time.time()
                result = call_gemma(args.model, prompt, img_b64)
                elapsed_call = time.time() - t0

                if "_error" in result or "_parse_error" in result:
                    stats["error"] += 1
                    update_photo(conn, photo["id"], result)
                else:
                    stats["ok"] += 1
                    update_photo(conn, photo["id"], result)

                conn.commit()
                pbar.update(1)
                pbar.set_postfix(
                    ok=stats["ok"],
                    err=stats["error"],
                    sec=f"{elapsed_call:.1f}s"
                )

                if args.delay > 0:
                    time.sleep(args.delay)

    elapsed = time.time() - start
    per_photo = elapsed / max(stats["ok"] + stats["error"], 1)

    print(f"\n{'='*50}")
    print(f"All done! {elapsed/3600:.1f} hours total ({per_photo:.1f}s/photo avg)")
    print(f"  Processed : {stats['ok']:,}")
    print(f"  Errors    : {stats['error']:,}")
    print(f"  Skipped   : {stats['skip']:,}")
    print(f"\nSearch your photos:")
    print(f"  sqlite3 {args.db}")
    print(f"  > SELECT filename, gemma_description FROM photos WHERE gemma_description LIKE '%beach%';")
    print(f"  > SELECT * FROM photos_fts WHERE photos_fts MATCH 'sunset ocean';")

    conn.close()

if __name__ == "__main__":
    main()
