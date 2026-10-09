# Upgrading the export Mac across a major macOS release

Written from the macOS 26.7 → 27 upgrade. The steps should carry over to later
releases; the specific failures are 27's.

## What goes wrong if you just upgrade

1. **Old osxphotos can't read the library.** macOS 27 changed the Photos schema.
   You need osxphotos **0.77.1 or later**
   ([osxphotos#2221](https://github.com/RhetTbull/osxphotos/issues/2221)). Upgrade
   it *before* you upgrade macOS, and check that a normal export still runs.
2. **Photos rebuilds its search index from scratch.** This happens in the
   background after the upgrade, and it takes hours to days. `labels` come back
   first. Activities and venue types come from a later analysis pass and can lag
   for over a week. On the library this was developed against, they sat 20–40%
   below the macOS 26.7 counts for six days, then started filling in again.
3. **The export writes that index into your files.** `photo_intel_export.py`
   writes `{label}`, `{searchinfo.activity}` and `{searchinfo.venue_type}` into
   each file's keywords. osxphotos `--update` re-exports a photo whenever that
   metadata differs from what the export DB stored last time, and the re-export is
   the whole file. So the first full sweep after the upgrade would have:
   - stripped activity and venue keywords (Theme Park, Museum, Baseball Stadium…)
     from **~15% of files**, and
   - rewritten and re-sent ~27% of all files, most of the library by size,
     because the changed labels fell disproportionately on videos.
4. **Re-analysis also wakes up the gate.** About a week of macOS 27 analysis
   changed face and scene counts on ~20% of old photos. The 2-hourly gated export
   flags all of those, so it is not only the weekly sweep that rewrites files.
5. **"Too many open files".** osxphotos 0.77.x holds a descriptor pair open per
   photo. `photo_intel_export.py` now raises its own limit to 32768. Older copies
   used 4096, which failed thousands of photos per sweep.

The fix for 2 and 3 is the **legacy keyword template**
(`src/photo_intel_legacy_kw.py`). It unions each file's pre-upgrade keywords back
in, so nothing is lost while the new index catches up. With it, the sweep
rewrote ~4% of files instead of ~27%, and no non-person keyword was lost.

## Before upgrading

1. **Upgrade osxphotos and check that an export runs**: `pipx upgrade osxphotos`,
   then one gated export.
2. **Take a baseline of the search index** with the Python inside the osxphotos
   install:
   ```bash
   ~/.local/pipx/venvs/osxphotos/bin/python tools/search_info_counts.py
   ```
   Note the `labels`, `activities` and `venue_types` counts.
3. **Pause every export job**, so nothing exports against a half-built index:
   ```bash
   for j in export export-full export-watchdog places places-incr; do
     launchctl bootout gui/$(id -u)/com.photo-intel.$j
     launchctl disable gui/$(id -u)/com.photo-intel.$j
   done
   ```
   Make sure `~/photo-intel/.export.lock` does not exist. Pause the watchdog too,
   or it will alert that the pipeline has gone dark.
4. **Freeze a copy of the export DB.** It is both your rollback point and the
   source of the legacy keyword map:
   ```bash
   cp -p ~/photo-intel/osxphotos_export.db /safe/place/osxphotos_export.pre-upgrade.db
   ```

## Upgrade, then wait

5. **Install macOS.** After the restart, finish Setup Assistant at the console.
   Until you do, login items don't start and external volumes stay unmounted.
6. **Let Photos migrate.** Opening Photos shows the migration progress
   ("Updating library… N%"). **Don't read `Photos.sqlite` while a large
   `Photos.sqlite-wal` is present.** An `immutable=1` read reports "malformed"
   mid-migration, which is expected and not corruption.
7. **Wait for the index rebuild.** Re-run `tools/search_info_counts.py` every
   so often. You're done when all three counts are close to the baseline **and**
   unchanged across two runs an hour apart. If activities and venue types stall
   well short of the baseline, that is the case the legacy map is for. Don't wait
   for them indefinitely.

## Before re-enabling anything

8. **Check that the gate still reads the schema.** Do a dry run into a throwaway
   manifest:
   ```bash
   python3 src/manifest_gate.py --manifest manifest/manifest.tsv \
       --manifest-out /tmp/m-new.tsv --changed-out /tmp/c-new.tsv
   wc -l /tmp/c-new.tsv
   ```
   A schema change fails loudly here. If the changed count is in the thousands,
   it is re-analysis (point 4 above). Build the map (step 9) before any export
   runs.
9. **Build the legacy keyword map** from the frozen DB and point the config at it:
   ```bash
   python3 src/build_legacy_keywords.py /safe/place/osxphotos_export.pre-upgrade.db \
       ~/photo-intel/legacy_keywords.json.gz
   ```
   ```ini
   [export]
   legacy_keywords = ~/photo-intel/legacy_keywords.json.gz
   ```
   Person names are excluded from the map, so person keywords keep following
   live face tags. The map is derived from your library. Keep it out of git
   (`*.json.gz` is ignored).
10. **Predict the rewrite** with the map in place:
    ```bash
    E=~/photo-intel/osxphotos_export.db
    sqlite3 -json "file:$E?immutable=1" \
        "select uuid, filepath, dest_size, exifdata from export_data where exifdata is not null;" \
        | gzip -1 > /tmp/exif_stored.json.gz
    ~/.local/pipx/venvs/osxphotos/bin/python tools/predict_update_reexports.py \
        /tmp/exif_stored.json.gz /tmp/pred.json --legacy-map ~/photo-intel/legacy_keywords.json.gz
    ```
    Write the number down. Re-run it without `--legacy-map` to see what the
    template is saving you.

## Re-enable and sweep

11. **Re-enable the jobs, then start the full sweep by hand**, early in a day you
    can watch:
    ```bash
    for j in export export-full export-watchdog places places-incr; do
      launchctl enable gui/$(id -u)/com.photo-intel.$j
      launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.photo-intel.$j.plist
    done
    launchctl kickstart gui/$(id -u)/com.photo-intel.export-full
    ```
    Use `kickstart` rather than running the script from a terminal. It keeps the
    job in the LaunchAgent context, which holds the Photos permission. Expect the
    watchdog's "lock held" alert during a long sweep.
12. **Stop if it goes past the prediction.** If the `Processed:` counts in
    `export-full.log` run well above step 10, stop the sweep:
    ```bash
    launchctl kill SIGTERM gui/$(id -u)/com.photo-intel.export-full
    ```
    Then investigate. Two kinds of `error:` are not a reason to stop:
    - `Too many open files` / `unable to open database file` means an old copy of
      `photo_intel_export.py` with the 4096 limit is running.
    - `Bad MakerNotes offset` is a few old camera JPEGs that exiftool won't
      rewrite. They keep their existing keywords.

## Verify

- `export-full.log` ends with "All windows complete." and shows `error: 0`.
- Re-running step 10 predicts close to zero: normal weekly drift is a fraction of
  a percent.
- The gated 2-hourly runs go back to "No changes".
- A sample of formerly at-risk files still carries its activity and venue
  keywords in both the JSON sidecar and the file's XMP `dc:subject`.

## Rollback

There is no practical macOS downgrade. If the rewrite fails partway, kickstart the
sweep again: `--update` resumes, because files already rewritten now match. If the
export DB is damaged, pause the jobs and copy the frozen DB back over
`osxphotos_export.db`, then run again.

## What this does not fix

Phase 1 reads a photo's sidecar only on first ingest. Rewritten keywords reach the
files and sidecars on the processing host, but the photo-intel database keeps the
labels it already had for existing photos. New photos get the new macOS labels.
