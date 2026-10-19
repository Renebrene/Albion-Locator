"""SQLite schema and schema-version metadata for the standalone offline index."""

SCHEMA_VERSION = 5
PACKAGE_VERSION = "0.4.0"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS settlements_name_idx ON settlements(normalized_name);
CREATE TABLE IF NOT EXISTS streets (
    id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS streets_settlement_name_idx
    ON streets(settlement_id, normalized_name);
CREATE TABLE IF NOT EXISTS houses (
    id TEXT PRIMARY KEY,
    street_id TEXT NOT NULL REFERENCES streets(id) ON DELETE CASCADE,
    house_number TEXT NOT NULL,
    normalized_house_number TEXT NOT NULL,
    lon REAL,
    lat REAL,
    location_precision TEXT NOT NULL DEFAULT 'unknown',
    settlement_assignment TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS houses_street_number_idx
    ON houses(street_id, normalized_house_number);
CREATE TABLE IF NOT EXISTS roads (
    id TEXT PRIMARY KEY,
    name TEXT,
    normalized_name TEXT NOT NULL,
    ref TEXT,
    normalized_ref TEXT NOT NULL,
    km_start REAL,
    km_end REAL,
    geometry_json TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    km_provenance TEXT NOT NULL DEFAULT 'estimated from geometry',
    source_ids_json TEXT,
    attributes_json TEXT
);
CREATE INDEX IF NOT EXISTS roads_name_idx ON roads(normalized_name);
CREATE INDEX IF NOT EXISTS roads_ref_idx ON roads(normalized_ref);
-- Segment-level geometry is needed for a cheap nearest-road prefilter.
-- The RTree stores only bounding boxes; exact projection still runs in Python.
CREATE TABLE IF NOT EXISTS road_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    road_id TEXT NOT NULL REFERENCES roads(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    lon1 REAL NOT NULL,
    lat1 REAL NOT NULL,
    lon2 REAL NOT NULL,
    lat2 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS road_segments_road_idx
    ON road_segments(road_id, segment_index);
CREATE VIRTUAL TABLE IF NOT EXISTS road_segments_rtree USING rtree(
    id, min_lon, max_lon, min_lat, max_lat
);
CREATE VIRTUAL TABLE IF NOT EXISTS settlements_fts USING fts5(
    name, content='settlements', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS streets_fts USING fts5(
    name, settlement_id UNINDEXED, content='streets', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS roads_fts USING fts5(
    name, ref, content='roads', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS settlements_ai AFTER INSERT ON settlements BEGIN
    INSERT INTO settlements_fts(rowid,name) VALUES(new.rowid,new.name);
END;
CREATE TRIGGER IF NOT EXISTS settlements_ad AFTER DELETE ON settlements BEGIN
    INSERT INTO settlements_fts(settlements_fts,rowid,name) VALUES('delete',old.rowid,old.name);
END;
CREATE TRIGGER IF NOT EXISTS settlements_au AFTER UPDATE ON settlements BEGIN
    INSERT INTO settlements_fts(settlements_fts,rowid,name) VALUES('delete',old.rowid,old.name);
    INSERT INTO settlements_fts(rowid,name) VALUES(new.rowid,new.name);
END;
CREATE TRIGGER IF NOT EXISTS streets_ai AFTER INSERT ON streets BEGIN
    INSERT INTO streets_fts(rowid,name,settlement_id) VALUES(new.rowid,new.name,new.settlement_id);
END;
CREATE TRIGGER IF NOT EXISTS streets_ad AFTER DELETE ON streets BEGIN
    INSERT INTO streets_fts(streets_fts,rowid,name,settlement_id)
    VALUES('delete',old.rowid,old.name,old.settlement_id);
END;
CREATE TRIGGER IF NOT EXISTS streets_au AFTER UPDATE ON streets BEGIN
    INSERT INTO streets_fts(streets_fts,rowid,name,settlement_id)
    VALUES('delete',old.rowid,old.name,old.settlement_id);
    INSERT INTO streets_fts(rowid,name,settlement_id) VALUES(new.rowid,new.name,new.settlement_id);
END;
CREATE TRIGGER IF NOT EXISTS roads_ai AFTER INSERT ON roads BEGIN
    INSERT INTO roads_fts(rowid,name,ref) VALUES(new.rowid,coalesce(new.name,''),coalesce(new.ref,''));
END;
CREATE TRIGGER IF NOT EXISTS roads_ad AFTER DELETE ON roads BEGIN
    INSERT INTO roads_fts(roads_fts,rowid,name,ref)
    VALUES('delete',old.rowid,coalesce(old.name,''),coalesce(old.ref,''));
END;
CREATE TRIGGER IF NOT EXISTS roads_au AFTER UPDATE ON roads BEGIN
    INSERT INTO roads_fts(roads_fts,rowid,name,ref)
    VALUES('delete',old.rowid,coalesce(old.name,''),coalesce(old.ref,''));
    INSERT INTO roads_fts(rowid,name,ref) VALUES(new.rowid,coalesce(new.name,''),coalesce(new.ref,''));
END;
"""
