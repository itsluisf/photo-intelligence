# Gotchas

Hard-won lessons. If you skip this file, you'll re-learn them the hard way.

## Ollama version matters

**Pin Ollama to 0.20.5.** Ollama 0.21.2 broke `gemma4:e4b` — the MoE model returns empty responses with no error. Symptoms: Phase 2 logs show responses arriving but `gemma_description` is blank or fails JSON parsing.

```bash
# If you're already on a newer version:
brew uninstall ollama
# then install 0.20.5 manually from the GitHub releases page
```

Check upstream before upgrading. As of this writing, the fix had not landed.

## Python `ollama` package version matters

**Pin `ollama==0.4.7` in requirements.txt.** Version 0.6.1 changed response objects from dicts to attribute access (`response['message']['content']` → `response.message.content`). If you upgrade without updating the code, every call breaks.

## Tune `num_predict` lower than you think

`num_predict: 600` was too tight — Gemma's JSON responses got truncated, producing unparseable output. Settled on **400**. If you change the prompt and expect longer output, retest.

## Bump `num_ctx`

Default context (8192) is too small once you include the prompt scaffolding plus image tokens. Set `num_ctx: 16384`.

## `people_count` is not always an integer

Gemma will sometimes return `"many"`, `"a few"`, or other strings for `people_count`. Don't `int()` it blindly. Either store as TEXT or normalize defensively.

## Don't write temp files to your boot volume

Photos.sqlite is huge, and its WAL file during exploration can balloon to many gigabytes. If your library lives on an external SSD, **make sure your temp files go there too**, or you'll fill the boot volume and crash macOS in interesting ways.

```python
# Good: temp files live next to the source
import tempfile
tempfile.tempdir = "/Volumes/Your SSD/tmp"
```

## Don't delete `phase2.log` while the process is running

Classic Unix gotcha. The process holds an open file descriptor to the deleted inode and silently writes into the void. You won't see new log lines but disk usage keeps climbing. Fix: full kill-and-restart of the worker.

```bash
# Rotate, don't delete:
mv phase2.log phase2.log.old
# then signal the process to reopen, or restart it
```

## The scene classification join

Apple's `Photos.sqlite` schema for scene labels is non-obvious:

```sql
ZSCENECLASSIFICATION
  → ZADDITIONALASSETATTRIBUTES (via Z_PK = ZASSETATTRIBUTES)
  → ZASSET

WHERE ZCLASSIFICATIONTYPE = 0
  AND ZCONFIDENCE >= 0.5
```

`ZCLASSIFICATIONTYPE = 0` is scenes. Other values are different classifier types (objects, etc.). Confidence threshold is your call; 0.5 is a reasonable floor.

## The album join table name varies

The asset↔album link table is named `Z_<N>ASSETS` where `<N>` is library-dependent (was `Z_33ASSETS` on the development library). **Probe for it at runtime** rather than hardcoding:

```python
cur.execute("""
    SELECT name FROM sqlite_master
    WHERE type='table' AND name LIKE 'Z_%ASSETS'
""")
```

The column names inside that table also vary — probe with `PRAGMA table_info()`.

## iCloud-shared photos won't have local files

Even with "Download Originals to this Mac" enabled, photos shared *to* you via iCloud Shared Albums or Shared Library generally don't download. On the dev library, ~57,000 of ~191,000 photos had no local file. They show up in `Photos.sqlite` but the file path resolves to nothing. Plan for this in Phase 2 — skip cleanly rather than erroring.

## Queue counts can be inflated

The "remaining" count from a naive `WHERE gemma_processed = 0` can drift higher than reality because of:

- Photos that fail repeatedly and never flip to `processed=1`
- Videos that should be excluded but weren't marked
- Photos with no local file that retry forever

Periodically audit:

```sql
SELECT
  COUNT(*) FILTER (WHERE gemma_processed = 1) AS done,
  COUNT(*) FILTER (WHERE gemma_processed = 0 AND phase1_error IS NULL) AS pending,
  COUNT(*) FILTER (WHERE phase1_error IS NOT NULL) AS errored
FROM photos;
```

## Videos should be excluded explicitly

`com.apple.quicktime-movie` and friends will choke or waste cycles. Mark them `gemma_processed = 1` (or add a separate skip flag) before Phase 2 starts so they never enter the queue.

## Column names

For reference (in case docs drift from code):

- The MIME-ish field is `uniform_type`, not `uti`
- There is no `last_attempted` column — if you want retry-with-backoff, add one
