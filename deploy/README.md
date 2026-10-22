# Deployment helpers

## Backup (`backup_data.py`)

Creates a consistent, checksummed snapshot of `users.db` and the offline
geodata (`offline_geo.sqlite3`, `basemap.mbtiles`, `vector.mbtiles`). SQLite
files are copied through the SQLite backup API; each snapshot is staged and
renamed into place only after `manifest.json` is written.

```bash
# one snapshot, keep the last 14
python3 deploy/backup_data.py --dest /var/backups/albion-locator --keep 14

# verify the newest snapshot (sizes, SHA-256, PRAGMA integrity_check)
python3 deploy/backup_data.py --dest /var/backups/albion-locator --verify-latest
```

Paths can also be supplied through `ALBION_USERS_DB`, `ALBION_GEO_DATA_DIR` and
`ALBION_BACKUP_DIR`. A daily systemd timer template is in
`deploy/systemd/albion-backup.{service,timer}`; adjust the user, paths and
`ExecStart` before installing it.

Restore is intentionally manual: stop the service, copy the required files from
a verified snapshot back into place, start the service, then run
`--verify-latest` again.

## Offline data update (`update_offline_data.py`)

Rebuilds the local index and vector MBTiles from a Geofabrik PBF, verifies both
SQLite databases, saves the current files under
`$ALBION_GEO_DATA_DIR/update-backups/<UTC timestamp>/`, and atomically replaces
the live files. On any failure the previous files are restored and the staging
directory is removed. A file lock prevents concurrent updates.

```bash
# Download the current South Federal District extract and rebuild both artifacts
python3 deploy/update_offline_data.py --geo-dir /var/lib/albion/geo

# Reuse a downloaded PBF and rebuild only the vector tiles
python3 deploy/update_offline_data.py \
  --geo-dir /var/lib/albion/geo \
  --pbf /data/source/south-fed-district-latest.osm.pbf \
  --source-date 2026-10-22 --skip-index

# Inspect the commands without changing anything
python3 deploy/update_offline_data.py --dry-run
```

Requirements: `osmium` CLI, `tilemaker`, and the optional `osmium` Python
package (`pip install -e 'packages/offline_geo[osm]'`). The script records the
source, date, SHA-256 and rollback path in
`$ALBION_GEO_DATA_DIR/update-manifest.json`. Verify the ODbL attribution and
SKDF/OSM usage rights before distributing generated data.

The index importer and tilemaker both read the clipped Rostov extract; the
process normally takes about 15 minutes on the reference machine. Keep enough
free space for the staging copy (roughly 350 MB for the current dataset).
