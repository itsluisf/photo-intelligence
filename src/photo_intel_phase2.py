#!/usr/bin/env python3
"""
photo_intel_phase2.py — Phase 2: Gemma 4 enrichment for the photo-intel pipeline.

Reads photo-intel.db (built by photo_intel_phase1.py from osxphotos sidecars),
sends each still image to Gemma 4 via Ollama on the processing host, and writes structured
description / tags / location-guess back to the DB.

Differences from the legacy phase2_gemma.py (photos_meta.db):
  - Reads photo-intel.conf instead of CLI defaults.
  - Targets the photo-intel schema: uuid / media_type / phase2_processed
    (tri-state 0/1/-1) / gemma_* columns. No gemma_processed, no kind/uniform_type.
  - HEIC handled via pillow-heif (Linux), not macOS `sips`.
  - Ollama 0.24.0: thinking disabled by default (think=False), configurable.
  - One pass then exit — no continuous loop. Re-run via a systemd timer.
  - No error state: a failed row is left at phase2_processed=0 and retried
    on the next run. phase2_processed=-1 means "skip" (videos).

The prompt and the output-flattening logic are carried over verbatim from the
legacy script so migrated legacy rows and newly-enriched rows share one format.

Usage:
    python3 photo_intel_phase2.py
    python3 photo_intel_phase2.py --config ~/photo-intel/photo-intel.conf
    python3 photo_intel_phase2.py --limit 50          # test batch
    python3 photo_intel_phase2.py --dry-run

CHANGE LOG (2026-05-25, debugging the 117 stuck-pending rows):
  - File index now prefers real image files over same-stem siblings such as
    a Live Photo's .mov clip. Previously dict.setdefault() kept whichever file
    rglob() happened to yield first; when the .mov won, encode_image() failed
    on it and the still was left pending forever. See pick_for_index().
  - Dominant Phase 2 failure was gemma4:e4b emitting structurally-correct JSON
    with UNESCAPED interior quotes in prose string values (e.g. a Rubbermaid
    "Untouchable" container), which json.loads rejects with "Expecting ','
    delimiter" / "Unterminated string". Fix: repair_json_quotes() escapes
    interior quotes; _generate_once() runs it as a fallback when the raw reply
    fails to parse. A no-op on already-valid JSON.
    (format="json" grammar-constrained decoding was tried first and removed —
    it made gemma4:e4b degenerate into a repeating-whitespace loop.)
  - call_gemma() retries once with a larger num_predict when parsing still
    fails — covers the genuine-truncation cases (model ran out of output
    budget). See num_predict_retry.
  - Repetition-loop fix: on text-dense images (museum plaques, foreign-language
    signs) gemma4:e4b would transcribe text into "text_in_image", lose the
    thread, and repeat a phrase to the num_predict ceiling — producing
    unterminated strings or trailing data after the JSON object. Two-part fix:
    options now set repeat_penalty=1.3 / repeat_last_n=256, and the prompt
    caps "text_in_image" at 200 chars (summary, not transcription). Note
    "text_in_image" is not persisted to the DB — the cap exists purely as a
    loop brake. Raising num_predict was NOT used here: it gives the loop more
    room, not less.
  - load_config(): num_predict fallback raised 400 -> 768; new optional
    [phase2] num_predict_retry (fallback 2048) used only for the retry pass.
  Nothing about the Ollama client object, its response API, or its version
  was changed.

CHANGE LOG (v2.3, after a 59/60 run was reported as a unit FAILURE):
  - _generate_once() now parses model replies with json.loads(..., strict=False),
    as does the balanced-span gate in _extract_json() so both use one predicate.
    This covers the "Invalid control character at: line N column M" family — a
    literal newline/tab inside a prose string value. repair_json_quotes() only
    escapes interior quotes and never addressed it, so the family burned the
    first attempt AND the larger-budget retry. strict=False relaxes only the
    control-character rule; genuine structural errors still fail, so this can
    turn a hard failure into a success and not the reverse.
  - Exit code: a run exits non-zero only when it had work in hand, completed
    none of it, and at least fail_floor (default 3) photos failed. Previously
    ANY per-photo failure hit sys.exit(1), so a single odd reply out of 60 put
    the unit in `failed` and fired its OnFailure alert. Same policy as
    photo_intel_video.py.
  - num_ctx is now [phase2] num_ctx (default 16384, the old hard-coded value).
    Set it to the context the model is already resident at — see the conf.
"""

import sqlite3
import argparse
import configparser
import json
import time
import sys
import os
import io
import base64
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Sibling module in this script's own directory — owns the place_* columns
# and the one shared definition of how a place reads.
from photo_intel_places import place_display

try:
    import ollama as _ollama
except ImportError:
    sys.exit("ERROR: ollama package not installed. Run: pip3 install ollama")

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("ERROR: Pillow not installed. Run: pip3 install pillow")

# pillow-heif registers HEIC/HEIF support with Pillow. Required on Linux —
# the legacy macOS path used `sips`, which does not exist here.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except ImportError:
    HEIF_OK = False

# Extensions Pillow (with pillow-heif) can actually open. Used to make sure
# the file index points at a still image and not a same-stem sibling such as
# a Live Photo's .mov clip.
IMAGE_EXTS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif",
    ".tif", ".tiff", ".webp", ".bmp",
}

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT DESIGN — carried over from legacy phase2_gemma.py unchanged so that
# migrated rows and Phase 2 output share an identical JSON shape.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a photo metadata assistant. Analyze photos and respond ONLY with valid JSON.
Never include markdown, code blocks, or explanatory text — raw JSON only.
Be concise. Confidence values are 0.0–1.0. Unknown values use null."""


def build_prompt(photo: dict) -> str:
    """Build a context-aware prompt from the photo-intel row.

    Context fed to the model: capture date and owner-identified face names.
    Apple scene_labels are deliberately NOT fed — they are unreliable
    (documented misclassification, e.g. liquor bottles tagged 'Ocean'),
    and parroting a wrong label degrades the description.
    """
    context_parts = []

    if photo.get("datetime_original"):
        context_parts.append(f"Date taken: {photo['datetime_original']}")
    elif photo.get("date"):
        context_parts.append(f"Date taken: {photo['date']}")

    # Prefer the OS reverse geocode over raw coordinates. Handing the model
    # bare lat/lon and asking it to name the place is what produced a capital
    # city's famous stadium, at "100% confidence", for a photo taken at a
    # minor-league ballpark 50 miles away — a 12B model cannot do coordinate
    # lookup from weights, and reports high confidence while failing at it.
    place = place_display(photo)
    if place:
        context_parts.append(
            f"Location (from the camera's geotag — authoritative, "
            f"do not name a different place): {place}"
        )
    elif photo.get("gps_lat") is not None and photo.get("gps_lon") is not None:
        context_parts.append(
            f"GPS coordinates: {photo['gps_lat']:.4f}, {photo['gps_lon']:.4f}"
        )

    if photo.get("persons"):
        try:
            people = json.loads(photo["persons"])
            if people:
                context_parts.append(
                    f"People identified by owner: {', '.join(people)}"
                )
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
}}

IMPORTANT: "text_in_image" must be at most 200 characters. If the image
contains more text than that, give a brief summary of it — do NOT attempt a
full transcription. Never repeat a word or phrase; write each point once.
This matters most for text-dense images such as plaques, signs, and documents,
where a full transcription causes the model to loop."""


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE ENCODING — Linux path. HEIC via pillow-heif (legacy used macOS `sips`).
# ─────────────────────────────────────────────────────────────────────────────

MAX_PIXELS = 2_000_000  # ~1.4k x 1.4k — sufficient for Gemma 4, saves tokens


def encode_image(file_path: Path) -> str | None:
    """Load image, resize if large, return base64 JPEG string, or None on failure."""
    suffix = file_path.suffix.lower()
    if suffix in (".heic", ".heif") and not HEIF_OK:
        print(f"  HEIC support missing (pip3 install pillow-heif): {file_path.name}")
        return None

    try:
        img = Image.open(file_path)

        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

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
        print(f"  Image encode error ({file_path.name}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FILE INDEX
# ─────────────────────────────────────────────────────────────────────────────

def pick_for_index(existing: Path | None, candidate: Path) -> Path:
    """Decide which of two same-stem files the index should keep.

    An exported Live Photo produces two files sharing one UUID stem — the
    still (.heic/.jpg) and a .mov motion clip. The DB row is the still, so the
    index must point at the still. A plain setdefault() kept whichever file
    rglob() yielded first; when the .mov won, encode_image() failed on it and
    the row was left pending on every run.

    Rule: a real image extension always beats a non-image sibling. If both are
    images, or neither is, keep the first one seen (stable, arbitrary).
    """
    if existing is None:
        return candidate
    existing_is_img = existing.suffix.lower() in IMAGE_EXTS
    candidate_is_img = candidate.suffix.lower() in IMAGE_EXTS
    if candidate_is_img and not existing_is_img:
        return candidate
    return existing


def build_file_index(dest_dir: Path) -> dict:
    """Map UUID stem -> exported file path, preferring still images over
    same-stem siblings (see pick_for_index)."""
    file_index: dict[str, Path] = {}
    for p in dest_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() == ".json":
            continue
        stem = p.stem
        file_index[stem] = pick_for_index(file_index.get(stem), p)
    return file_index


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CALL
# ─────────────────────────────────────────────────────────────────────────────

def repair_json_quotes(raw: str) -> str:
    """Escape interior (prose) double-quotes inside JSON string values.

    gemma4:e4b reliably emits structurally-correct JSON — correct nesting,
    correct delimiters — but does NOT escape double-quote characters that
    appear inside prose string values, e.g.

        "description": "a Rubbermaid "Untouchable" container"
        "description": "...baseball legend John "Buck" O'Neil..."

    json.loads then rejects this with "Expecting ',' delimiter" (the parser
    thinks the string ended) or "Unterminated string". This walks the text
    tracking string state: a double-quote is STRUCTURAL — left alone — when it
    opens a string or when the next non-whitespace char is one of  : , } ]
    (i.e. it closes a value or key). Any other double-quote seen while inside
    a string is interior prose and is escaped to \\".

    On already-valid JSON this is a no-op (every quote is structural), so it
    is safe to apply unconditionally as a fallback.

    This replaced an earlier attempt to use Ollama's format="json" grammar
    constraint: on gemma4:e4b the constraint made the model degenerate into a
    repeating-whitespace loop when it hit a token the grammar disallowed, i.e.
    it produced worse output, not valid JSON.
    """
    out = []
    i = 0
    n = len(raw)
    in_string = False
    while i < n:
        c = raw[i]
        if not in_string:
            out.append(c)
            if c == '"':
                in_string = True
            i += 1
            continue
        # inside a string value
        if c == '\\':
            # keep an existing escape pair intact
            out.append(c)
            if i + 1 < n:
                out.append(raw[i + 1])
                i += 2
            else:
                i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and raw[j] in ' \t\r\n':
                j += 1
            nxt = raw[j] if j < n else ''
            if nxt in (':', ',', '}', ']', ''):
                out.append(c)            # structural close
                in_string = False
            else:
                out.append('\\"')        # interior prose quote -> escape
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _balanced_json_span(s: str) -> str | None:
    """Return the first balanced {...} object in s, or None if none closes.
    String-aware, so braces inside description text don't skew the depth
    count. Kept identical to photo_intel_video._balanced_json_span."""
    start = s.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None                  # never closed — a truncated reply


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a raw model reply: strip code fences, then
    isolate the outermost {...}."""
    raw = (raw or "").strip()

    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break
    raw = raw.strip()

    # Trailing content after a complete object ("Extra data: line 1 column
    # 688") — the model finishes the JSON and keeps talking, or emits a second
    # object. The first{..last} trim below never fired on these because the
    # reply already starts with '{'. Only accept the balanced span when it
    # actually parses, so every other reply shape reaches the caller's
    # repair_json_quotes path byte-identically to before — in particular a
    # truncated reply still surfaces as "Unterminated string" and still
    # triggers the larger-budget retry. (2026-07-27)
    span = _balanced_json_span(raw)
    if span is not None and span != raw:
        try:
            json.loads(span, strict=False)
            return span
        except json.JSONDecodeError:
            pass

    if not raw.startswith("{"):
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            raw = raw[first:last + 1]
    return raw.strip()


def _generate_once(client, model: str, prompt: str, image_b64: str,
                   enable_thinking: bool, num_predict: int,
                   num_ctx: int = 16384) -> dict:
    """Single Ollama generate + parse attempt. Returns parsed JSON, or a dict
    with _error / _parse_error on failure."""
    try:
        response = client.generate(
            model=model,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            images=[image_b64],
            think=enable_thinking,
            # NOTE: format="json" was tried and removed — see repair_json_quotes().
            # Grammar-constrained decoding made gemma4:e4b degenerate into a
            # repeating-whitespace loop. Instead the reply is parsed as-is and,
            # on failure, run through repair_json_quotes() once before retry.
            options={
                "temperature": 0.1,
                "num_predict": num_predict,
                # Must MATCH the context the model is already resident at. A
                # different explicit num_ctx makes Ollama restart the runner:
                # a hard-coded value that differs from the server default
                # costs a multi-minute reload on every run with work, and
                # leaves every other client of that model at the wrong context
                # until something reloads it. See [phase2] num_ctx.
                "num_ctx": num_ctx,
                # repeat_penalty / repeat_last_n brake the repetition loop
                # gemma4:e4b falls into on text-dense images (museum plaques,
                # foreign-language signs): it transcribes, loses the thread,
                # and repeats a phrase to the num_predict ceiling — producing
                # unterminated strings or trailing junk after the JSON object.
                # 1.3 stopped the loop on the known-degenerate test images.
                "repeat_penalty": 1.3,
                "repeat_last_n": 256,
            },
            stream=False,
        )
    except Exception as e:
        return {"_error": str(e)}

    # Ollama returns an object on newer clients (.response) and a dict on
    # older ones (["response"]). This guard handles both — no version change.
    raw = (response.response if hasattr(response, "response")
           else response.get("response", "")) or ""

    # strict=False permits raw control characters (a literal newline or tab)
    # inside string values. Gemma emits these in prose descriptions; strict
    # json.loads rejects them as "Invalid control character at: ..." and
    # repair_json_quotes() does not address them, so the family failed both
    # attempts and the larger-budget retry could only make it likelier.
    # Python still rejects every genuine structural error under strict=False.
    candidate = _extract_json(raw)
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        # gemma4:e4b emits structurally-sound JSON with unescaped interior
        # quotes. Escape them and try once more before declaring failure.
        try:
            return json.loads(repair_json_quotes(candidate), strict=False)
        except json.JSONDecodeError as e:
            return {"_parse_error": str(e), "_raw": raw[:500]}


def call_gemma(client, model: str, prompt: str, image_b64: str,
               enable_thinking: bool, num_predict: int,
               num_predict_retry: int | None = None,
               num_ctx: int = 16384) -> dict:
    """Send image + prompt to Gemma 4 via Ollama. Returns parsed JSON, or a
    dict with a _error / _parse_error key on failure.

    If the first attempt produces a parse error — almost always a JSON object
    truncated because the model hit its num_predict ceiling — retry once with
    a larger num_predict. A transport-level _error is NOT retried here; that
    row is simply left pending for the next scheduled run.
    """
    result = _generate_once(client, model, prompt, image_b64,
                            enable_thinking, num_predict, num_ctx)

    if "_parse_error" not in result:
        return result

    if not num_predict_retry or num_predict_retry <= num_predict:
        return result

    retry = _generate_once(client, model, prompt, image_b64,
                           enable_thinking, num_predict_retry, num_ctx)
    # If the retry also fails to parse, return it (its _raw reflects the
    # larger-budget attempt, which is more useful for debugging).
    return retry


# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def mark_videos_skipped(conn) -> int:
    """One-time: set video rows to phase2_processed=-1 so the pending count
    reflects only stills. Idempotent — only touches rows still at 0."""
    cur = conn.execute(
        "UPDATE photos SET phase2_processed=-1 "
        "WHERE media_type='video' AND phase2_processed=0"
    )
    conn.commit()
    return cur.rowcount


def get_pending(conn, limit: int | None) -> list:
    """Return still-image rows that still need enrichment."""
    sql = ("SELECT uuid, media_type, file_ext, datetime_original, date, "
           "       gps_lat, gps_lon, persons, "
           "       place_name, place_aoi, place_city, place_state, "
           "       place_country, place_country_code "
           "FROM photos "
           "WHERE phase2_processed = 0 AND media_type = 'image' "
           "ORDER BY date DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _tag_str(val) -> str | None:
    """Coerce one tag-ish element to a scalar string, or None to drop it.
    A VLM sometimes emits tags/notable_features as objects ({"tag": "beach"})
    or numbers rather than plain strings. An unhashable element reaching
    dict.fromkeys() below aborts the whole run — that is exactly how Phase 2b
    wedged on 2026-07-25. Kept identical to photo_intel_video._tag_str."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, dict):
        for key in ("tag", "name", "label", "value", "feature", "text"):
            if key in val:
                return _tag_str(val[key])
        return None          # unrecognised object — drop, don't stringify it
    return None


def _tag_list(raw) -> list:
    """Normalise a tags-ish field into a flat list of clean strings."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out = []
    for item in raw:
        if isinstance(item, (list, tuple)):      # nested list — flatten a level
            out.extend(t for t in (_tag_str(x) for x in item) if t)
        elif (t := _tag_str(item)):
            out.append(t)
    return out


def flatten_result(gemma_data: dict) -> tuple:
    """Carried over from legacy update_photo(): flatten the rich JSON object
    into (description, tags_json, location_guess) so the output shape matches
    the migrated legacy rows exactly. Hardened 2026-07-26 against non-scalar
    model output (see _tag_str); mirrors photo_intel_video.flatten_result minus
    its 'video' provenance tag."""
    desc = gemma_data.get("description")
    if desc is not None and not isinstance(desc, str):
        desc = _tag_str(desc) or json.dumps(desc, ensure_ascii=False)

    all_tags = _tag_list(gemma_data.get("tags"))
    for field in ("scene_type", "setting", "mood", "time_of_day", "weather"):
        val = _tag_str(gemma_data.get(field))
        if val and val not in ("other", "unknown", "indoor"):
            all_tags.append(val)

    people_info = gemma_data.get("people", {}) or {}
    if not isinstance(people_info, dict):
        people_info = {}
    try:
        people_count = int(str(people_info.get("count") or 0).split()[0])
    except (ValueError, TypeError, IndexError):
        people_count = 0
    if people_count > 0:
        all_tags.append(f"people:{people_count}")

    all_tags.extend(_tag_list(gemma_data.get("notable_features")))

    loc = gemma_data.get("estimated_location", {}) or {}
    if not isinstance(loc, dict):
        loc = {}
    loc_guess = None
    parts = [p for p in (_tag_str(loc.get("specific_place")),
                         _tag_str(loc.get("region_or_city")),
                         _tag_str(loc.get("country"))) if p]
    if parts:
        conf = loc.get("confidence", 0) or 0
        try:
            loc_guess = f"{', '.join(parts)} (confidence: {float(conf):.0%})"
        except (ValueError, TypeError):
            loc_guess = ", ".join(parts)

    tags_json = json.dumps(list(dict.fromkeys(all_tags)))  # dedupe, keep order
    return desc, tags_json, loc_guess


def write_success(conn, uuid: str, gemma_data: dict):
    desc, tags_json, loc_guess = flatten_result(gemma_data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE photos SET gemma_description=?, gemma_tags=?, "
        "gemma_location_guess=?, phase2_processed=1, phase2_error=NULL, "
        "phase2_processed_at=? "
        "WHERE uuid=?",
        (desc, tags_json, loc_guess, now, uuid),
    )
    conn.commit()


def write_failure(conn, uuid: str, reason: str):
    """Record a failure reason on the row. The row stays at
    phase2_processed=0 so it retries next run, but phase2_error makes a
    persistently-failing row visible instead of silent."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE photos SET phase2_error=?, phase2_processed_at=? WHERE uuid=?",
        (str(reason)[:500], now, uuid),
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: config not found: {path}")
    cp = configparser.ConfigParser()
    cp.read(path)

    cfg = {
        "db_path": cp.get("paths", "db_path"),
        "dest_dir": cp.get("paths", "dest_dir"),
        "ollama_url": cp.get("ollama", "url"),
        "ollama_model": cp.get("ollama", "model"),
        # [phase2] is optional; sane defaults if the section is absent.
        "enable_thinking": cp.getboolean("phase2", "enable_thinking",
                                         fallback=False),
        "num_predict": cp.getint("phase2", "num_predict", fallback=768),
        # Used only for the retry pass when the first reply truncates.
        "num_predict_retry": cp.getint("phase2", "num_predict_retry",
                                       fallback=2048),
        "request_timeout": cp.getint("phase2", "request_timeout", fallback=120),
        "num_ctx": cp.getint("phase2", "num_ctx", fallback=16384),
        # How many failures must accumulate before a zero-success run counts as
        # systemic — see the exit guard at the end of main(). Deliberately not
        # written into the shipped conf: one less key to drift out of sync.
        "fail_floor": cp.getint("phase2", "fail_floor", fallback=3),
    }
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Phase 2: Gemma 4 enrichment for photo-intel")
    parser.add_argument("--config",
                        default=str(Path.home() / "photo-intel" / "photo-intel.conf"),
                        help="Path to photo-intel.conf")
    parser.add_argument("--limit", type=int,
                        help="Process at most N photos this run (test batches)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed; no Ollama calls")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    db_path = Path(cfg["db_path"])
    if not db_path.exists():
        sys.exit(f"ERROR: DB not found: {db_path}\nRun photo_intel_phase1.py first.")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] photo-intel Phase 2 starting")
    print(f"  DB       : {db_path}")
    print(f"  Ollama   : {cfg['ollama_url']}  model={cfg['ollama_model']}")
    print(f"  Thinking : {'on' if cfg['enable_thinking'] else 'off'}  "
          f"num_predict={cfg['num_predict']}  retry={cfg['num_predict_retry']}")
    if not HEIF_OK:
        print("  WARNING  : pillow-heif not installed — HEIC photos will be skipped "
              "and left pending.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    skipped_videos = mark_videos_skipped(conn)
    if skipped_videos:
        print(f"  Marked {skipped_videos:,} video rows as skip (phase2_processed=-1)")

    pending = get_pending(conn, args.limit)
    print(f"  Pending  : {len(pending):,} still images\n")

    if args.dry_run:
        for p in pending[:10]:
            print(f"  {p['uuid']}  {p['file_ext']}  {p['date']}")
        if len(pending) > 10:
            print(f"  ... and {len(pending) - 10:,} more")
        conn.close()
        return

    if not pending:
        print("Nothing to do — queue empty.")
        conn.close()
        return

    client = _ollama.Client(host=cfg["ollama_url"],
                            timeout=cfg["request_timeout"])

    dest_dir = Path(cfg["dest_dir"])
    stats = {"ok": 0, "fail": 0, "retried_ok": 0}
    start = time.time()

    print(f"  Indexing files under {dest_dir} ...", flush=True)
    file_index = build_file_index(dest_dir)
    print(f"  Indexed {len(file_index)} files", flush=True)

    for i, photo in enumerate(pending, 1):
        uuid = photo["uuid"]
        ext = photo["file_ext"] or ""

        # locate the exported file via the prebuilt uuid->path index
        img_path = file_index.get(uuid)

        if img_path is None or not img_path.exists():
            stats["fail"] += 1
            write_failure(conn, uuid, "file not found")
            print(f"  [{i}/{len(pending)}] {uuid}  FILE NOT FOUND — left pending")
            continue

        # Defensive: if the only file on disk for this UUID is a non-image
        # sibling (e.g. a Live Photo .mov with no exported still), do not even
        # try to encode it — say so plainly and leave the row pending.
        if img_path.suffix.lower() not in IMAGE_EXTS:
            stats["fail"] += 1
            write_failure(conn, uuid,
                          f"no still image on disk (only {img_path.suffix})")
            print(f"  [{i}/{len(pending)}] {uuid}  no still image on disk "
                  f"(only {img_path.suffix}) — left pending")
            continue

        img_b64 = encode_image(img_path)
        if not img_b64:
            stats["fail"] += 1
            write_failure(conn, uuid, "encode failed")
            print(f"  [{i}/{len(pending)}] {uuid}  encode failed — left pending")
            continue

        prompt = build_prompt(photo)
        t0 = time.time()
        result = call_gemma(client, cfg["ollama_model"], prompt, img_b64,
                            cfg["enable_thinking"], cfg["num_predict"],
                            cfg["num_predict_retry"], cfg["num_ctx"])
        dt = time.time() - t0

        if "_error" in result or "_parse_error" in result:
            stats["fail"] += 1
            reason = result.get("_error") or result.get("_parse_error")
            write_failure(conn, uuid, reason)
            print(f"  [{i}/{len(pending)}] {uuid}  FAIL ({dt:.1f}s) "
                  f"{reason} — left pending")
            continue

        try:
            write_success(conn, uuid, result)
        except Exception as exc:
            # Parseable JSON in an unexpected shape. Record and move on — one
            # odd payload must not end the run (Phase 2b wedge, 2026-07-25).
            stats["fail"] += 1
            write_failure(conn, uuid, f"flatten/write failed: {exc!r}")
            print(f"  [{i}/{len(pending)}] {uuid}  WRITE FAIL ({dt:.1f}s) "
                  f"{exc!r} — left pending")
            continue
        stats["ok"] += 1
        if i % 25 == 0 or i == len(pending):
            rate = (stats["ok"] + stats["fail"]) / max(time.time() - start, 1)
            print(f"  [{i}/{len(pending)}] ok={stats['ok']} fail={stats['fail']} "
                  f"({rate:.2f}/s)")

    elapsed = time.time() - start
    done = stats["ok"] + stats["fail"]
    per = elapsed / max(done, 1)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{stamp}] Done — processed: {stats['ok']}  failed: {stats['fail']}  "
          f"({elapsed/60:.1f} min, {per:.1f}s/photo)")
    print("Failed rows remain at phase2_processed=0 and retry on the next run.")
    if stats["fail"]:
        n = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE phase2_error IS NOT NULL "
            "AND phase2_processed=0").fetchone()[0]
        print(f"WARNING: {n:,} row(s) carry a phase2_error — inspect with: "
              "SELECT uuid, phase2_error FROM photos "
              "WHERE phase2_error IS NOT NULL AND phase2_processed=0;")
    conn.close()
    # A run that enriched at least one photo did its job. Individual failures
    # stay at phase2_processed=0 and retry on the next scheduled run, so they
    # are not a unit failure — exiting 1 on them put the unit in `failed` and
    # fired an OnFailure alert for a run that was 59/60 successful. Exit
    # non-zero only when the run had work in hand and completed none of it:
    # that is a broken model or host, not one odd reply.
    #
    # The zero-success test alone is queue-size sensitive: on a quiet day the
    # queue is a handful of photos, so a couple of unlucky replies ARE the whole
    # run and routine self-clearing noise alerts. Hence the floor — a
    # zero-success run is only systemic once fail_floor failures have piled up.
    # Because a failed row stays at phase2_processed=0 and the queue carries it
    # forward, a real outage crosses the floor within a run or two while a
    # transient never does.
    if stats["fail"] and not stats["ok"] and stats["fail"] >= cfg["fail_floor"]:
        sys.exit(1)
    if stats["fail"] and not stats["ok"]:
        print(f"  ({stats['fail']} failed, below the systemic floor of "
              f"{cfg['fail_floor']} — left pending for the next run)")


if __name__ == "__main__":
    main()
