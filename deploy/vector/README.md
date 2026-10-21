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
