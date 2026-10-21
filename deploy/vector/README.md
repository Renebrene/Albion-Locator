# Векторная офлайн-подложка (MapLibre)

Растровый Leaflet-слой остаётся по умолчанию. Векторный режим включается в
«Настройках → Источник карты → Векторные тайлы (MapLibre)» и использует
`ALBION_VECTOR_MBTILES` (по умолчанию `$ALBION_GEO_DATA_DIR/vector.mbtiles`).

## Сборка тайлов из OSM PBF

Нужны `osmium` CLI и `tilemaker`. Пример:

```bash
# 1. Скачать регион Geofabrik (пример — ЮФО)
curl -L -o south-fed-district.osm.pbf \
  https://download.geofabrik.de/russia/south-fed-district-latest.osm.pbf

# 2. Вырезать Ростовскую область по контуру приложения
PYTHONPATH=packages/offline_geo/src python - <<'PY'
from offline_geo.osm_import import _poly_text
open('/tmp/rostov.poly','w',encoding='utf-8').write(
    _poly_text('static/rostov_boundary.geojson'))
PY
osmium extract --polygon /tmp/rostov.poly --strategy complete_ways \
  --overwrite -o /tmp/rostov.osm.pbf south-fed-district.osm.pbf

# 3. Собрать vector MBTiles (z5–14)
tilemaker --input /tmp/rostov.osm.pbf \
  --output "$ALBION_GEO_DATA_DIR/vector.mbtiles" \
  --config deploy/vector/config.json \
  --process deploy/vector/process.lua
```

Стиль: `static/vector-style.json`; шрифты подписей — `static/fonts/DejaVuSans`
(сгенерированы `fontnik build-glyphs` из DejaVu Sans, покрывают кириллицу).

## Локальный индекс поиска

Индекс адресов/дорог собирается из того же PBF:

```bash
# pyosmium + osmium CLI должны быть доступны
python -m pip install -e 'packages/offline_geo[osm]'
PYTHONPATH=packages/offline_geo/src python -m offline_geo import-osm-pbf \
  --pbf /tmp/rostov.osm.pbf \
  --boundary static/rostov_boundary.geojson \
  --database "$ALBION_GEO_DATA_DIR/offline_geo.sqlite3" \
  --source-date "$(date +%F)"
```

Для Ростовской области снимок 2026-10-21 дал:

- исходный PBF ЮФО — 302 МБ;
- вырезанный PBF — 79 МБ;
- `offline_geo.sqlite3` — 214 МБ (559 922 дома, 46 420 дорог, 4 465 населённых пунктов);
- `vector.mbtiles` — 122 МБ (z5–14, 56 348 тайлов).

В приложении выберите «Локальная база» и «Векторные тайлы (MapLibre)» —
после этого карта и поиск работают без внешних запросов.

## Обновление

```bash
# Если PBF содержит replication metadata Geofabrik:
python -m offline_geo update-osm-pbf --pbf /path/south-fed-district.osm.pbf
# затем повторить extract + import + tilemaker
```

Полный цикл обновления на тестовой машине занимает ~15 минут (импорт индекса —
основное время, сборка тайлов — ~10 секунд). Данные OSM — ODbL; при
распространении индекса/тайлов сохраняйте атрибуцию OpenStreetMap.
