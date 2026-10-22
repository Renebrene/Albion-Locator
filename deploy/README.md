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

## Сеть: LAN, TLS и туннель

Приложение остаётся на `127.0.0.1:8021`; наружу его публикует reverse proxy
или туннель. Пример nginx с TLS для внутреннего имени — `deploy/nginx/albion-locator.conf`.
Он передаёт `Host`, `X-Forwarded-Host`, `X-Forwarded-Proto` и
`X-Forwarded-For`, ограничивает тело запроса 110 МБ и закрывает `/metrics`.

Для CloudPub (или другого туннеля) направьте туннель на
`http://127.0.0.1:8021` и убедитесь, что он:

- завершает TLS и передаёт `X-Forwarded-Proto: https`;
- сохраняет исходный `Host`/`X-Forwarded-Host`;
- передаёт `X-Forwarded-For` (иначе все клиенты будут выглядеть как один IP
  для rate-limit);
- не открывает `/metrics` и `/api/audit` наружу.

Проверено на `albion-locator.cloudpub.ru` (2026-10-22): CloudPub передаёт
публичные `Host`/`X-Forwarded-Host`, `X-Forwarded-Proto: https`, реальный
`X-Forwarded-For`/`X-Real-IP` и перезаписывает подменённые клиентом
forwarding-заголовки. Поэтому для этого туннеля достаточно
`ALBION_COOKIE_SECURE=auto`, а `ALBION_ALLOWED_ORIGINS` не требуется.

Если провайдер туннеля не передаёт `X-Forwarded-Proto`, включите
`ALBION_COOKIE_SECURE=always` — иначе cookie не получат флаг `Secure`.
При прямом HTTP-доступе к loopback оставьте `ALBION_COOKIE_SECURE=auto`.
Если `Host` переписывается, добавьте публичный origin в
`ALBION_ALLOWED_ORIGINS=https://<публичный-хост>`.

## Security preflight

```bash
python3 deploy/security_check.py --env-file .env
```

Скрипт проверяет режим `0600`, что `.env` игнорируется Git, и печатает только
факт наличия ключей (без значений). Он не ротирует ключи: это нужно сделать в
кабинете Яндекса, ограничив каждый ключ доменом и разделив dev/prod.

## Чек-лист перед публикацией

1. Cookie: `ALBION_COOKIE_SECURE=auto` (TLS-прокси передаёт `X-Forwarded-Proto`)
   или `always`; сессионная cookie — HttpOnly, CSRF-cookie — читаемая браузером.
2. CSRF: включён по умолчанию (`ALBION_CSRF=1`); API-клиенты должны присылать
   `X-CSRF-Token` из cookie `albion_csrf` для POST/PUT/PATCH/DELETE.
3. `/docs`, `/redoc`, `/openapi.json` закрыты; для локальной разработки —
   `ALBION_ENABLE_DOCS=1`.
4. Rate-limit и lockout: `ALBION_API_RATE_LIMIT`,
   `ALBION_LOGIN_RATE_LIMIT`, `ALBION_LOGIN_MAX_FAILURES`,
   `ALBION_LOGIN_LOCKOUT_SECONDS`; после сбоя процесса счётчики сбрасываются.
5. `.env` должен иметь режим `0600`; ключи Яндекса — отдельные для dev/prod,
   ограничены доменом в кабинете и ротированы после любого раскрытия.
6. `ALBION_METRICS_TOKEN` задан, если `/metrics` собирается через proxy;
   сам endpoint не публикуйте в интернет.
