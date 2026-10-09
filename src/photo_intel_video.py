#!/usr/bin/env python3
"""
photo_intel_video.py — Phase 2b: Qwen-VL video enrichment for photo-intel.

Phase 2 (photo_intel_phase2.py) enriches STILL IMAGES only — it explicitly
marks every `media_type='video'` row phase2_processed=-1 ("skip"). This script
fills that gap: it samples frames from each clip, sends the ordered frameset to
a vision-language model (Qwen-VL) via Ollama, and writes a chronological
description + tags + location-guess back into the SAME gemma_* columns Phase 2
uses — so videos become searchable in the Flask web app / FTS5 index with NO
schema change and NO web-app change.

WHY A SEPARATE SCRIPT (not a Phase 2 flag)
  Qwen-VL is a different model with different VRAM behaviour and a different
  output profile than the pinned gemma4:12b-it-q8_0. Coupling it into Phase 2
  would risk the hard-won still-image JSON stability. Instead this runs as its
  own nightly timer (01:00, when the GPU is idle — after the 21:00 Phase 2 run
  and before another nightly Ollama job's 06:00-07:30 window), on the box's proven VRAM-swap
  pattern (see forge-run.sh / free-ollama-vram.sh):

    1. unload the pinned gemma model to free VRAM
    2. load Qwen-VL, describe every pending clip (kept warm between clips)
    3. WARM GEMMA BACK before exit — always, even on failure (finally:) —
       so that job at 06:00 and Phase 2 at 09:00 hit a warm model.

STATE MACHINE (reuses phase2_processed on video rows; no new columns)
    -1 or 0  → pending (Phase 2 parks videos at -1; freshly-ingested at 0)
     1       → described by this script (gemma_* columns populated)
     2       → permanently skipped (too short / no file / unreadable) — terminal,
               so an unprocessable clip is not retried every night.
  Phase 2 never touches videos at 1 or 2 (its mark_videos_skipped() only moves
  0→-1, and get_pending() selects images only), so these states are inert to it.

VRAM / concurrency note (OLLAMA_MAX_LOADED_MODELS=1 + KEEP_ALIVE=-1)
  With a single model slot, an interactive gemma query fired mid-run would evict
  Qwen-VL; our next frame call reloads it — thrash on the DDR3/PCIe path, not
  breakage. The flock (one run at a time) plus the 01:00 window makes this a
  non-issue in practice. keep_alive on the Qwen calls holds it loaded between
  clips so it is not reloaded per clip.

Usage:
    python3 photo_intel_video.py
    python3 photo_intel_video.py --config ~/photo-intel/photo-intel.conf
    python3 photo_intel_video.py --limit 20        # test batch
    python3 photo_intel_video.py --dry-run
    python3 photo_intel_video.py --no-swap         # don't touch gemma (manual
                                                   # runs while gemma is already
                                                   # unloaded, e.g. under vlm-run.sh)
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
import fcntl
import signal
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# Sibling module in this script's own directory — owns the place_* columns
# and the one shared definition of how a place reads.
from photo_intel_places import place_display

# Set by SIGTERM (systemd stop / timeout) so the clip loop breaks cleanly and
# the finally: block still warms gemma back — see the max_minutes note in main().
_STOP = False


def _on_sigterm(signum, frame):
    global _STOP
    _STOP = True

try:
    import ollama as _ollama
except ImportError:
    sys.exit("ERROR: ollama package not installed. Run: pip3 install ollama")

try:
    from PIL import Image
except ImportError:
    sys.exit("ERROR: Pillow not installed. Run: pip3 install pillow")

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mpg", ".mpeg"}

# Frames are token-heavy; keep each modest so a 12-frame clip stays within
# num_ctx. ~0.6 MP/frame is plenty for scene-level narration.
VIDEO_FRAME_MAX_PIXELS = 640 * 640

LOCK_PATH = "/tmp/photo-intel-video.lock"


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT — same JSON envelope as Phase 2 (so flatten_result() is reused verbatim
# and output shares one shape), but instructs a CHRONOLOGICAL reading of the
# ordered frameset rather than a single-image description.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a video metadata assistant. You are shown several
still frames sampled in chronological order from a single video clip. Reason
about what happens ACROSS the frames as a sequence, then respond ONLY with
valid JSON. Never include markdown, code blocks, or explanatory text — raw JSON
only. Confidence values are 0.0-1.0. Unknown values use null."""


def build_prompt(row: dict, n_frames: int) -> str:
    context_parts = []
    if row.get("datetime_original"):
        context_parts.append(f"Date taken: {row['datetime_original']}")
    elif row.get("date"):
        context_parts.append(f"Date taken: {row['date']}")
    # See the matching note in photo_intel_phase2.build_prompt. Until
    # 2026-08-01 this branch never fired at all: video GPS was dropped at
    # ingest (EXIF-only read, no QuickTime fallback), so every video row had
    # gps_lat NULL and the model described clips with no location context.
    place = place_display(row)
    if place:
        context_parts.append(
            f"Location (from the camera's geotag — authoritative, "
            f"do not name a different place): {place}")
    elif row.get("gps_lat") is not None and row.get("gps_lon") is not None:
        context_parts.append(
            f"GPS coordinates: {row['gps_lat']:.4f}, {row['gps_lon']:.4f}")
    if row.get("persons"):
        try:
            people = json.loads(row["persons"])
            if people:
                context_parts.append(
                    f"People identified by owner: {', '.join(people)}")
        except Exception:
            pass
    context = "\n".join(context_parts)

    return f"""These are {n_frames} frames sampled in order from one video clip.
I already know:
{context if context else "(no prior metadata)"}

Describe what the clip shows as a sequence of events. Respond with ONLY this
JSON structure:
{{
  "description": "3-4 sentence chronological account of what happens in the clip, in order",
  "scene_type": "one of: landscape, cityscape, portrait, group, architecture, food, indoor, event, wildlife, macro, abstract, document, other",
  "setting": "specific environment: beach, forest, mountain, city_street, restaurant, home_interior, airport, museum, park, desert, ocean, farm, office, other",
  "primary_subject": "the main focus of the clip in 3-5 words",
  "mood": "one of: joyful, calm, dramatic, melancholy, energetic, formal, candid, other",
  "estimated_location": {{
    "country": null,
    "region_or_city": null,
    "specific_place": null,
    "confidence": 0.0,
    "reasoning": "brief explanation of location clues"
  }},
  "people": {{
    "count": 0,
    "age_groups": [],
    "activity": "what the people are doing over the clip"
  }},
  "tags": ["tag1", "tag2"],
  "text_in_image": null,
  "time_of_day": "one of: dawn, morning, midday, afternoon, evening, night, indoor, unknown",
  "weather": "one of: sunny, cloudy, overcast, rainy, foggy, snowy, unknown, indoor",
  "notable_features": [],
  "confidence_overall": 0.0
}}

IMPORTANT: "text_in_image" must be at most 200 characters — summarise, never
transcribe. Never repeat a word or phrase; write each point once."""


# ─────────────────────────────────────────────────────────────────────────────
# FRAME EXTRACTION (ffmpeg / ffprobe)
# ─────────────────────────────────────────────────────────────────────────────

def video_duration(path: Path) -> float | None:
    """Clip duration in seconds via ffprobe, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        val = out.stdout.strip()
        return float(val) if val and val != "N/A" else None
    except Exception:
        return None


def _encode_frame(raw_jpeg: bytes) -> str | None:
    """Downscale a raw JPEG frame and return base64, or None on failure."""
    try:
        img = Image.open(io.BytesIO(raw_jpeg))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        px = w * h
        if px > VIDEO_FRAME_MAX_PIXELS:
            scale = (VIDEO_FRAME_MAX_PIXELS / px) ** 0.5
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def extract_frames(path: Path, n: int, dur: float | None) -> list[str]:
    """Return up to n base64 JPEG frames, evenly spaced and chronological.

    Frames are sampled at the centre of n equal time slices (t=(k+0.5)/n * dur)
    so the first/last (often black/blurred) moments are avoided. If duration is
    unknown, fall back to `-vf fps` sampling.
    """
    frames: list[str] = []

    if dur and dur > 0:
        for k in range(n):
            t = dur * (k + 0.5) / n
            try:
                out = subprocess.run(
                    ["ffmpeg", "-nostdin", "-ss", f"{t:.3f}", "-i", str(path),
                     "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
                     "-loglevel", "error", "-"],
                    capture_output=True, timeout=60)
                if out.returncode == 0 and out.stdout:
                    enc = _encode_frame(out.stdout)
                    if enc:
                        frames.append(enc)
            except Exception:
                continue
        return frames

    # Duration unknown: sample a fixed rate, cap at n.
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", str(path), "-vf", "fps=1",
             "-frames:v", str(n), "-f", "image2pipe", "-vcodec", "mjpeg",
             "-loglevel", "error", "-"],
            capture_output=True, timeout=120)
        # image2pipe concatenates JPEGs; split on SOI marker (FFD8FFE0/E1...).
        data = out.stdout
        marker = b"\xff\xd8\xff"
        idxs = [i for i in range(len(data) - 2) if data[i:i + 3] == marker]
        for a, b in zip(idxs, idxs[1:] + [len(data)]):
            enc = _encode_frame(data[a:b])
            if enc:
                frames.append(enc)
            if len(frames) >= n:
                break
    except Exception:
        pass
    return frames


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CALL + JSON REPAIR (repair_json_quotes / _extract_json carried over
# from photo_intel_phase2.py verbatim — same failure modes apply to any VLM.)
# ─────────────────────────────────────────────────────────────────────────────

def repair_json_quotes(raw: str) -> str:
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
        if c == '\\':
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
                out.append(c)
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _balanced_json_span(s: str) -> str | None:
    """Return the first balanced {...} object in s, or None if none closes.
    String-aware, so braces inside description text don't skew the depth
    count. Kept identical to photo_intel_phase2._balanced_json_span."""
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
            json.loads(span)
            return span
        except json.JSONDecodeError:
            pass

    if not raw.startswith("{"):
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            raw = raw[first:last + 1]
    return raw.strip()


def _generate_once(client, model: str, prompt: str, frames: list[str],
                   enable_thinking: bool, num_predict: int, num_ctx: int,
                   keep_alive: str) -> dict:
    """One generate + parse attempt against the VLM with the ordered frameset."""
    try:
        response = client.generate(
            model=model,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            images=frames,
            think=enable_thinking,
            keep_alive=keep_alive,
            options={
                "temperature": 0.1,
                "num_predict": num_predict,
                "num_ctx": num_ctx,
                "repeat_penalty": 1.3,
                "repeat_last_n": 256,
            },
            stream=False,
        )
    except Exception as e:
        return {"_error": str(e)}

    raw = (response.response if hasattr(response, "response")
           else response.get("response", "")) or ""
    candidate = _extract_json(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            return json.loads(repair_json_quotes(candidate))
        except json.JSONDecodeError as e:
            return {"_parse_error": str(e), "_raw": raw[:500]}


def call_vlm(client, model: str, prompt: str, frames: list[str],
             enable_thinking: bool, num_predict: int, num_ctx: int,
             keep_alive: str, num_predict_retry: int | None = None) -> dict:
    """VLM generate with a one-shot larger-budget retry on parse failure.

    Validation (2026-07-11, qwen3-vl:8b, 20 clips) showed 9/20 parse failures,
    all truncation: video narrations run longer than the still-image ones and
    output length varies at temperature 0.1, so a fixed num_predict clips some
    replies mid-JSON ("Unterminated string", "Expecting value char 0"). Mirrors
    photo_intel_phase2.call_gemma: on a _parse_error (truncation), retry once
    with num_predict_retry. A transport _error is not retried.
    """
    result = _generate_once(client, model, prompt, frames,
                            enable_thinking, num_predict, num_ctx, keep_alive)
    if "_parse_error" not in result:
        return result
    if not num_predict_retry or num_predict_retry <= num_predict:
        return result
    return _generate_once(client, model, prompt, frames,
                          enable_thinking, num_predict_retry, num_ctx, keep_alive)


# ─────────────────────────────────────────────────────────────────────────────
# DB — reuses the gemma_* columns; flatten_result() is Phase 2's, verbatim.
# ─────────────────────────────────────────────────────────────────────────────

def get_pending(conn, limit: int | None) -> list:
    """Video rows not yet described (1) and not permanently skipped (2)."""
    sql = ("SELECT uuid, media_type, file_ext, datetime_original, date, "
           "       gps_lat, gps_lon, persons, "
           "       place_name, place_aoi, place_city, place_state, "
           "       place_country, place_country_code "
           "FROM photos "
           "WHERE media_type = 'video' AND phase2_processed NOT IN (1, 2) "
           "ORDER BY date DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _tag_str(val) -> str | None:
    """Coerce one tag-ish element to a scalar string, or None to drop it.
    qwen3-vl sometimes emits tags/notable_features as objects ({"tag": "beach"})
    or numbers rather than plain strings. An unhashable element reaching
    dict.fromkeys() below used to abort the whole nightly run (2026-07-25)."""
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


def flatten_result(data: dict) -> tuple:
    """Flatten the rich JSON into (description, tags_json, location_guess).
    Mirrors photo_intel_phase2.flatten_result so video and still rows share one
    output shape, with a 'video' provenance tag appended — but unlike Phase 2 it
    hardens every field against non-scalar VLM output (see _tag_str)."""
    desc = data.get("description")
    if desc is not None and not isinstance(desc, str):
        desc = _tag_str(desc) or json.dumps(desc, ensure_ascii=False)

    all_tags = _tag_list(data.get("tags"))
    for field in ("scene_type", "setting", "mood", "time_of_day", "weather"):
        val = _tag_str(data.get(field))
        if val and val not in ("other", "unknown", "indoor"):
            all_tags.append(val)

    people_info = data.get("people", {}) or {}
    if not isinstance(people_info, dict):
        people_info = {}
    try:
        people_count = int(str(people_info.get("count") or 0).split()[0])
    except (ValueError, TypeError, IndexError):
        people_count = 0
    if people_count > 0:
        all_tags.append(f"people:{people_count}")

    all_tags.extend(_tag_list(data.get("notable_features")))

    all_tags.append("video")  # provenance: distinguishes VLM video rows

    loc = data.get("estimated_location", {}) or {}
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

    tags_json = json.dumps(list(dict.fromkeys(all_tags)))
    return desc, tags_json, loc_guess


def write_success(conn, uuid: str, data: dict):
    desc, tags_json, loc_guess = flatten_result(data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE photos SET gemma_description=?, gemma_tags=?, "
        "gemma_location_guess=?, phase2_processed=1, phase2_error=NULL, "
        "phase2_processed_at=? WHERE uuid=?",
        (desc, tags_json, loc_guess, now, uuid))
    conn.commit()


def mark_skipped(conn, uuid: str, reason: str):
    """Terminal skip (phase2_processed=2): unreadable/too-short clip that must
    NOT be retried nightly. Reason recorded in phase2_error for visibility."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE photos SET phase2_processed=2, phase2_error=?, "
        "phase2_processed_at=? WHERE uuid=?",
        (str(reason)[:500], now, uuid))
    conn.commit()


def write_failure(conn, uuid: str, reason: str):
    """Transient failure (e.g. VLM transport error): leave state unchanged so
    the clip is retried next run, but record the reason."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE photos SET phase2_error=?, phase2_processed_at=? WHERE uuid=?",
        (str(reason)[:500], now, uuid))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# VRAM SWAP
# ─────────────────────────────────────────────────────────────────────────────

def ollama_stop(model: str):
    try:
        subprocess.run(["ollama", "stop", model], timeout=30,
                       capture_output=True)
    except Exception as e:
        print(f"  (warn) could not stop {model}: {e}")


def ollama_warm(client, model: str):
    """Warm the pinned gemma model back into VRAM with a trivial generate.

    keep_alive must be the INTEGER -1 (infinite), not the string "-1": the
    Ollama server parses a string keep_alive as a Go duration and rejects "-1"
    with 'missing unit in duration "-1"' (observed 2026-07-11), leaving gemma
    unloaded. -1 as a number is accepted and pins the model.
    """
    try:
        client.generate(model=model, prompt="ok", options={"num_predict": 1},
                        keep_alive=-1, stream=False)
        print(f"  warmed {model} back into VRAM")
    except Exception as e:
        print(f"  (warn) could not warm {model}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: config not found: {path}")
    cp = configparser.ConfigParser()
    cp.read(path)
    return {
        "db_path": cp.get("paths", "db_path"),
        "dest_dir": cp.get("paths", "dest_dir"),
        "ollama_url": cp.get("ollama", "url"),
        # [video] — all optional with defaults so the section can be minimal.
        "vlm_model": cp.get("video", "model", fallback="qwen3-vl:8b"),
        # The PINNED model to unload before / warm after the run. Taken from an
        # explicit [video] key (not [ollama] model) so the swap target is
        # unambiguous; both now read gemma4:12b-it-q8_0 (the box default).
        "gemma_model": cp.get("video", "gemma_model",
                              fallback="gemma4:12b-it-q8_0"),
        "frames": cp.getint("video", "frames", fallback=12),
        "min_duration": cp.getfloat("video", "min_duration", fallback=2.0),
        "num_ctx": cp.getint("video", "num_ctx", fallback=32768),
        "num_predict": cp.getint("video", "num_predict", fallback=2048),
        # Larger budget for the retry pass when the first reply truncates.
        "num_predict_retry": cp.getint("video", "num_predict_retry",
                                       fallback=3072),
        "request_timeout": cp.getint("video", "request_timeout", fallback=300),
        # Self-imposed wall-clock stop (minutes from start). At the 01:00 timer
        # this ends the run ~05:45, warming gemma back BEFORE another nightly
        # Ollama job's 06:00-07:30 window, so the two never contend.
        "max_minutes": cp.getint("video", "max_minutes", fallback=285),
        "keep_alive": cp.get("video", "keep_alive", fallback="15m"),
        # How many failures must accumulate before a zero-success run counts as
        # systemic — see the exit guard at the end of main(). Deliberately not
        # written into the shipped conf: one less key to drift out of sync.
        "fail_floor": cp.getint("video", "fail_floor", fallback=3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Phase 2b: Qwen-VL video enrichment for photo-intel")
    ap.add_argument("--config",
                    default=str(Path.home() / "photo-intel" / "photo-intel.conf"))
    ap.add_argument("--limit", type=int, help="Process at most N clips")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-swap", action="store_true",
                    help="Do not unload/warm gemma (gemma already unloaded)")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    # Graceful stop on SIGTERM (systemd stop/timeout): flip _STOP so the loop
    # breaks and the finally: block still warms gemma back.
    signal.signal(signal.SIGTERM, _on_sigterm)

    db_path = Path(cfg["db_path"])
    if not db_path.exists():
        sys.exit(f"ERROR: DB not found: {db_path}")

    # Single-run lock: never two of these fighting over the one model slot.
    lock_fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another photo_intel_video run holds the lock — exiting.")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] photo-intel Phase 2b (video) starting")
    print(f"  DB       : {db_path}")
    print(f"  VLM      : {cfg['vlm_model']}  frames={cfg['frames']}  "
          f"num_ctx={cfg['num_ctx']}")
    print(f"  Swap     : {'off (--no-swap)' if args.no_swap else cfg['gemma_model']}")
    print(f"  Deadline : stop after {cfg['max_minutes']} min "
          f"(warm gemma back before the 09:00 Phase 2 run)")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    pending = get_pending(conn, args.limit)
    print(f"  Pending  : {len(pending):,} clips\n")

    if args.dry_run:
        for r in pending[:10]:
            print(f"  {r['uuid']}  {r['file_ext']}  {r['date']}")
        if len(pending) > 10:
            print(f"  ... and {len(pending) - 10:,} more")
        conn.close()
        return

    if not pending:
        print("Nothing to do — no pending clips.")
        conn.close()
        return

    dest_dir = Path(cfg["dest_dir"])
    client = _ollama.Client(host=cfg["ollama_url"],
                            timeout=cfg["request_timeout"])
    stats = {"ok": 0, "skip": 0, "fail": 0}
    start = time.time()
    max_seconds = cfg["max_minutes"] * 60 if cfg["max_minutes"] else 0

    if not args.no_swap:
        print(f"  Freeing VRAM (unload {cfg['gemma_model']}) ...")
        ollama_stop(cfg["gemma_model"])

    try:
        # uuid -> exported video path
        index: dict[str, Path] = {}
        for p in dest_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                index[p.stem] = p

        for i, row in enumerate(pending, 1):
            # Stop cleanly on SIGTERM or when the wall-clock deadline is hit,
            # so the finally: block warms gemma back before 06:00. Remaining
            # clips stay pending and are picked up next run.
            if _STOP or (max_seconds and time.time() - start >= max_seconds):
                why = "SIGTERM" if _STOP else f"{cfg['max_minutes']}-min deadline"
                print(f"  Stopping ({why}) after {i - 1} clips this run.")
                break

            uuid = row["uuid"]
            path = index.get(uuid)
            if path is None or not path.exists():
                mark_skipped(conn, uuid, "video file not found")
                stats["skip"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  no file — skipped")
                continue

            dur = video_duration(path)
            if dur is not None and dur < cfg["min_duration"]:
                mark_skipped(conn, uuid, f"too short ({dur:.1f}s)")
                stats["skip"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  too short "
                      f"({dur:.1f}s) — skipped")
                continue

            frames = extract_frames(path, cfg["frames"], dur)
            if not frames:
                mark_skipped(conn, uuid, "no frames extracted")
                stats["skip"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  no frames — skipped")
                continue

            prompt = build_prompt(row, len(frames))
            t0 = time.time()
            result = call_vlm(client, cfg["vlm_model"], prompt, frames,
                              False, cfg["num_predict"], cfg["num_ctx"],
                              cfg["keep_alive"], cfg["num_predict_retry"])
            dt = time.time() - t0

            if "_error" in result:
                # transport error — retry next run
                write_failure(conn, uuid, result["_error"])
                stats["fail"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  VLM ERROR ({dt:.1f}s) "
                      f"{result['_error']} — will retry")
                continue
            if "_parse_error" in result:
                write_failure(conn, uuid, result["_parse_error"])
                stats["fail"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  PARSE FAIL ({dt:.1f}s) "
                      f"— will retry")
                continue

            try:
                write_success(conn, uuid, result)
            except Exception as exc:
                # Parseable JSON in an unexpected shape. Record and move on —
                # one odd payload must not end the night's window.
                write_failure(conn, uuid, f"flatten/write failed: {exc!r}")
                stats["fail"] += 1
                print(f"  [{i}/{len(pending)}] {uuid}  WRITE FAIL ({dt:.1f}s) "
                      f"{exc!r} — will retry")
                continue
            stats["ok"] += 1
            print(f"  [{i}/{len(pending)}] {uuid}  ok ({dt:.1f}s, "
                  f"{len(frames)} frames)")

    finally:
        conn.close()
        if not args.no_swap:
            ollama_warm(client, cfg["gemma_model"])
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()

    elapsed = time.time() - start
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{stamp}] Done — described: {stats['ok']}  skipped: "
          f"{stats['skip']}  failed: {stats['fail']}  ({elapsed/60:.1f} min)")
    # Per-clip failures are recorded (phase2_error) and left pending, so they are
    # retried on the next run — a few must NOT put the nightly oneshot into
    # `failed` state (self-healing noise). Only surface a systemic failure: work
    # was attempted but nothing at all succeeded.
    #
    # The zero-success test alone was written for backlog-sized runs, when a
    # run described hundreds of clips and "nothing succeeded" really was
    # systemic. Once the backlog is cleared a night is typically 1-4 new clips,
    # so two ordinary runaway-thinking failures ARE the whole run and routine
    # noise alerts. Hence the floor: a zero-success run is only systemic once
    # fail_floor failures have piled up. Because a failed clip stays pending and
    # the queue carries it forward, a real outage crosses the floor within a
    # night or two while a transient never does.
    if stats["ok"] == 0 and stats["fail"] >= cfg["fail_floor"]:
        sys.exit(1)
    if stats["ok"] == 0 and stats["fail"] > 0:
        print(f"  ({stats['fail']} failed, below the systemic floor of "
              f"{cfg['fail_floor']} — left pending for the next run)")


if __name__ == "__main__":
    main()
