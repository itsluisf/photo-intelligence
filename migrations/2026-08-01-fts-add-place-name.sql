-- photo-intel: rebuild photos_fts to index place_name.
--
-- photos_fts is an external-content FTS5 table (content='photos'), so its
-- column names must match columns on photos and 'rebuild' repopulates it
-- straight from that table. Adding a column therefore means drop + recreate
-- + rebuild; there is no ALTER for FTS5 columns.
--
-- Run AFTER the place backfill, with the sync triggers already dropped, so
-- the bulk UPDATE does not fire 79k delete+insert pairs into an index that
-- is about to be discarded anyway.

DROP TRIGGER IF EXISTS photos_fts_insert;
DROP TRIGGER IF EXISTS photos_fts_update;
DROP TRIGGER IF EXISTS photos_fts_delete;

DROP TABLE IF EXISTS photos_fts;

CREATE VIRTUAL TABLE photos_fts USING fts5(
    uuid UNINDEXED,
    gemma_description,
    gemma_tags,
    persons,
    gemma_location_guess,
    place_name,
    content='photos',
    content_rowid='rowid'
);

INSERT INTO photos_fts(photos_fts) VALUES('rebuild');

CREATE TRIGGER photos_fts_insert AFTER INSERT ON photos BEGIN
    INSERT INTO photos_fts(rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES (new.rowid, new.uuid, new.gemma_description, new.gemma_tags, new.persons, new.gemma_location_guess, new.place_name);
END;

CREATE TRIGGER photos_fts_update AFTER UPDATE ON photos BEGIN
    INSERT INTO photos_fts(photos_fts, rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES ('delete', old.rowid, old.uuid, old.gemma_description, old.gemma_tags, old.persons, old.gemma_location_guess, old.place_name);
    INSERT INTO photos_fts(rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES (new.rowid, new.uuid, new.gemma_description, new.gemma_tags, new.persons, new.gemma_location_guess, new.place_name);
END;

CREATE TRIGGER photos_fts_delete AFTER DELETE ON photos BEGIN
    INSERT INTO photos_fts(photos_fts, rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES ('delete', old.rowid, old.uuid, old.gemma_description, old.gemma_tags, old.persons, old.gemma_location_guess, old.place_name);
END;

INSERT INTO photos_fts(photos_fts) VALUES('optimize');
