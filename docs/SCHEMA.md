# Database Schema

The pipeline produces one SQLite file: `photos_meta.db`. The full DDL is in [`scripts/schema.sql`](../scripts/schema.sql).

## Tables

### `photos`

One row per asset from `Photos.sqlite`.

| Column | Type | Notes |
|---|---|---|
| `uuid` | TEXT PRIMARY KEY | Apple's photo UUID |
| `filename` | TEXT | Original filename |
| `uniform_type` | TEXT | UTI (e.g. `public.jpeg`, `com.apple.quicktime-movie`) |
| `kind` | TEXT | `photo` / `video` / `live` |
| `date_taken` | TEXT (ISO 8601) | Capture date |
| `date_added` | TEXT (ISO 8601) | When added to library |
| `latitude`, `longitude` | REAL | GPS if present |
| `width`, `height` | INTEGER | Pixel dimensions |
| `favorite` | INTEGER | 0/1 |
| `hidden` | INTEGER | 0/1 |
| `local_path` | TEXT | Resolved path on disk, NULL for iCloud-shared without local copy |
| `phase1_processed_at` | TEXT | When Phase 1 wrote this row |
| `phase1_error` | TEXT | NULL on success |
| `gemma_processed` | INTEGER | 0/1 |
| `gemma_processed_at` | TEXT | When Phase 2 completed this row |
| `gemma_description` | TEXT | AI-generated prose description |
| `gemma_tags` | TEXT (JSON array) | AI-generated tags |
| `gemma_location_guess` | TEXT | AI's guess at location/setting (not GPS) |
| `gemma_error` | TEXT | NULL on success |

### `albums`

| Column | Type | Notes |
|---|---|---|
| `uuid` | TEXT PRIMARY KEY | Album UUID |
| `name` | TEXT | Display name |
| `kind` | TEXT | `user` / `smart` / `system` |

### `photo_albums`

Many-to-many between photos and albums.

| Column | Type |
|---|---|
| `photo_uuid` | TEXT |
| `album_uuid` | TEXT |

### `scenes`

Apple's scene classifications (from `ZSCENECLASSIFICATION`, type 0, confidence ≥ 0.5).

| Column | Type | Notes |
|---|---|---|
| `photo_uuid` | TEXT | |
| `label` | TEXT | Human-readable where available; raw `scene_<id>` otherwise |
| `confidence` | REAL | |

### `faces`

| Column | Type |
|---|---|
| `photo_uuid` | TEXT |
| `person_name` | TEXT |
| `confidence` | REAL |

## Useful queries

```sql
-- Photos taken at night with people
SELECT p.filename, p.gemma_description
FROM photos p
WHERE p.gemma_tags LIKE '%night%'
  AND EXISTS (SELECT 1 FROM faces f WHERE f.photo_uuid = p.uuid);

-- Progress check
SELECT
  COUNT(*) FILTER (WHERE gemma_processed = 1) AS done,
  COUNT(*) FILTER (WHERE gemma_processed = 0) AS pending,
  COUNT(*) FILTER (WHERE gemma_error IS NOT NULL) AS errored
FROM photos;

-- Find photos by description
SELECT filename, date_taken, gemma_description
FROM photos
WHERE gemma_description LIKE '%golden retriever%'
ORDER BY date_taken DESC;
```
