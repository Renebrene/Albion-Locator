"""
АЛЬБИОН: Локатор-Навигатор (Автономная экспериментальная версия)
Версия: 4.2.1 — порт 8019, стабильная версия
"""
from __future__ import annotations
import asyncio, io, math, os, re, difflib, json, base64, time
from fractions import Fraction
from pathlib import Path
from typing import Any
import httpx, piexif
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

# --- СПРАВОЧНИК ДОРОГ ---
try:
    from roads import ROADS
    print(f"✅ База дорог загружена: {len(ROADS)} записей")
except ImportError:
    ROADS = {}
    print("⚠️ Файл roads.py не найден!")

# --- OLLAMA ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = 20.0
_llm_cache: dict[str, str | None] = {}

# --- ЯНДЕКС ГЕОКОДЕР ---
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")

FEDERAL_ROADS = {
    "М-4": {"skdf_name": "М-4 «Дон»", "full_name": "М-4 «Дон» Москва - Воронеж - Ростов-на-Дону - Краснодар - Новороссийск"},
    "Р-260": {"skdf_name": "Р-260 «Волгоград - Каменск-Шахтинский - Луганск»", "full_name": "Автомобильная дорога Р-260 Волгоград - Каменск-Шахтинский - Луганск"},
    "Р-280": {"skdf_name": "Р-280 «Новороссия»", "full_name": "Р-280 «Новороссия» Ростов-на-Дону – Мариуполь – Мелитополь – Симферополь"},
    "А-280": {"skdf_name": "А-280 «Ростов-на-Дону – Таганрог»", "full_name": "Автомобильная дорога А-280 Ростов-на-Дону – Таганрог"},
}

def find_federal_road(name: str) -> dict | None:
    if not name: return None
    upper = name.upper()
    for code, data in FEDERAL_ROADS.items():
        if code in upper or data["skdf_name"].upper() in upper:
            return data
    return None

WFS_BASE = "https://скдф.рф/api-geoserver/skdf_open/wfs"
WFS_TIMEOUT = 15.0
PROJECT_DIR = Path(__file__).parent
ROAD_LAYER = "skdf_open:lyr_road_by_value_line"
CATEGORY_MAP = {1: "Автомагистраль", 2: "I категория", 3: "II категория",
                4: "III категория", 5: "IV категория", 6: "V категория"}
COATING_MAP = {1: "Капитальный", 2: "Облегчённый", 3: "Переходный", 4: "Низший"}

app = FastAPI(title="АЛЬБИОН: Локатор-Навигатор (Autonom)", version="4.2.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ========== ГЕОМЕТРИЯ ==========
def _dms_to_dd(dms: tuple, ref: bytes) -> float:
    d, m, s = (float(Fraction(x[0], x[1])) for x in dms)
    return -(d + m / 60 + s / 3600) if ref in (b"S", b"W") else (d + m / 60 + s / 3600)

def extract_exif(data: bytes) -> dict[str, Any]:
    try:
        img = Image.open(io.BytesIO(data))
        ex = piexif.load(img.info.get("exif", b""))
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать EXIF. ({e})")
    gps = ex.get("GPS", {})
    if piexif.GPSIFD.GPSLatitude not in gps:
        raise HTTPException(400, "В EXIF нет GPS-координат.")
    return {
        "lat": _dms_to_dd(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef]),
        "lon": _dms_to_dd(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
    }

def _bbox_from_radius(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / 111100.0
    dlon = radius_m / (111100.0 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

def _haversine(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    return 2 * R * math.asin(math.sqrt(math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2))

def _bearing(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl)*math.cos(p2),
                                   math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl))) + 360) % 360

def _local_m(lon, lat, lon0, lat0):
    R = 6371000.0
    return math.radians(lon - lon0) * math.cos(math.radians(lat0)) * R, math.radians(lat - lat0) * R

def _project_on_line(coords, lon, lat):
    proj = [_local_m(c[0], c[1], lon, lat) for c in coords]
    best = {"dist": float("inf"), "seg": 0, "t": 0.0}
    for i in range(len(proj) - 1):
        x1, y1 = proj[i][0], proj[i][1]
        x2, y2 = proj[i+1][0], proj[i+1][1]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx*dx + dy*dy
        t = max(0.0, min(1.0, (-x1*dx - y1*dy) / L2)) if L2 > 0 else 0.0
        cx, cy = x1 + t*dx, y1 + t*dy
        d2 = cx*cx + cy*cy
        if d2 < best["dist"]:
            best = {"dist": d2, "seg": i, "t": t, "cx": cx, "cy": cy, "dx": dx, "dy": dy}
    perp_m = math.sqrt(best["dist"])
    cross = best["dx"] * (-best["cy"]) - best["dy"] * (-best["cx"])
    side_raw = 1 if cross > 0 else (-1 if cross < 0 else 0)
    x1, y1 = coords[best["seg"]][0], coords[best["seg"]][1]
    x2, y2 = coords[best["seg"]+1][0], coords[best["seg"]+1][1]
    return {
        "projected_lon": lon + math.degrees(best["cx"] / (6371000.0 * math.cos(math.radians(lat)))),
        "projected_lat": lat + math.degrees(best["cy"] / 6371000.0),
        "perpendicular_m": round(perp_m, 1),
        "side_raw": side_raw,
        "tangent_bearing": round(_bearing(x1, y1, x2, y2), 1),
        "t": best["t"]
    }

def _road_side_info(road_feature, lon, lat):
    geom = road_feature.get("geometry", {})
    if geom.get("type") not in ("LineString", "MultiLineString"):
        return {"error": "no line"}
    candidates = geom["coordinates"] if geom["type"] == "LineString" else geom["coordinates"]
    best = min((_project_on_line(line, lon, lat) for line in candidates),
               key=lambda x: x["perpendicular_m"], default={"error": "no line"})
    if "error" in best:
        return best
    props = road_feature.get("properties", {}) or {}
    start_km = float(props.get("start_km") or 0)
    finish_km = float(props.get("finish_km") or 0)
    if start_km < finish_km:
        best["side"] = -best["side_raw"]
    else:
        best["side"] = best["side_raw"]
    del best["side_raw"]
    return best

# ========== WFS ==========
async def _wfs_getfeature(client, type_name, bbox_deg=None, max_features=50):
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature",
              "typeName": type_name, "outputFormat": "application/json",
              "srsname": "EPSG:4326", "maxFeatures": str(max_features)}
    if bbox_deg:
        params["bbox"] = f"{bbox_deg[0]},{bbox_deg[1]},{bbox_deg[2]},{bbox_deg[3]},EPSG:4326"
    try:
        r = await client.get(WFS_BASE, params=params, timeout=WFS_TIMEOUT)
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception:
        return []

def get_first_point(geometry: dict) -> tuple[float | None, float | None]:
    coords = geometry.get("coordinates")
    if not coords:
        return None, None
    while isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    try:
        return float(coords[0]), float(coords[1])
    except (ValueError, TypeError, IndexError):
        return None, None

# ========== ПОИСК ДОРОГ ==========
def normalize_text(text: str) -> str:
    if not text: return ""
    text = text.lower()
    text = text.replace('"', '').replace("'", "").replace('«', '').replace('»', '')
    text = text.replace('–', '-').replace('—', '-')
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def find_road_id_by_name(road_name: str) -> str | None:
    if not road_name: return None
    norm_input = normalize_text(road_name)
    for road_id, name_in_db in ROADS.items():
        norm_db = normalize_text(name_in_db)
        if norm_input in norm_db or norm_db in norm_input:
            return road_id
    best_match, best_score = None, 0.0
    for road_id, name_in_db in ROADS.items():
        norm_db = normalize_text(name_in_db)
        score = difflib.SequenceMatcher(None, norm_input, norm_db).ratio()
        if score > best_score:
            best_score, best_match = score, road_id
    if best_score > 0.65:
        print(f"[DEBUG] ✅ difflib match: {best_match} ({best_score:.2f})")
        return best_match
    return None

def find_road_id_with_llm(road_name: str) -> str | None:
    if not road_name: return None
    cache_key = road_name.lower().strip()
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]
    roads_list = "\n".join([f"{code}: {name}" for code, name in ROADS.items()])
    prompt = f"""Найди код дороги из справочника по названию.
НАЗВАНИЕ: "{road_name}"
СПРАВОЧНИК:
{roads_list}
ПРАВИЛА:
- Сопоставляй по смыслу
- Игнорируй слова "автомобильная дорога", кавычки, опечатки
- Ответь ТОЛЬКО кодом (например: 60К-11) или НЕ НАЙДЕНО"""
    try:
        response = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.0, "num_predict": 20}},
            timeout=OLLAMA_TIMEOUT
        )
        response.raise_for_status()
        result = response.json().get("response", "").strip()
        match = re.search(r'(\d{2}[АКМРВНAКМРНB]-\d+(?:\.\d+)?)', result, re.IGNORECASE)
        if match:
            road_id = match.group(1).upper()
            road_id = road_id.replace('A','А').replace('K','К').replace('M','М')
            road_id = road_id.replace('P','Р').replace('H','Н').replace('B','В')
            if road_id in ROADS:
                _llm_cache[cache_key] = road_id
                return road_id
        _llm_cache[cache_key] = None
        return None
    except Exception as e:
        print(f"[LLM] ❌ {e}")
        return None

def format_road_names(props: dict) -> dict[str, str]:
    road_name_from_skdf = (props.get("road_name") or "").strip()
    value_of_the_road = (props.get("value_of_the_road") or "").strip()
    road_id = None
    if value_of_the_road:
        norm = re.sub(r'\s+', '', value_of_the_road).upper()
        match = re.search(r'(\d{2}[АКМРВНAКМРНB]-\d+(?:\.\d+)?)', norm)
        if match:
            road_id = match.group(1)
    if not road_id and road_name_from_skdf:
        road_id = find_road_id_by_name(road_name_from_skdf)
    if not road_id and road_name_from_skdf:
        road_id = find_road_id_with_llm(road_name_from_skdf)
    if road_id and road_id in ROADS:
        full_name = f"{road_id}, {ROADS[road_id]}"
    else:
        full_name = road_name_from_skdf or "Дорога без названия"
    return {"skdf_name": road_name_from_skdf or "Нет данных", "full_name": full_name}

async def get_yandex_road_name(lat: float, lon: float) -> dict | None:
    if not YANDEX_API_KEY:
        return None
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {"apikey": YANDEX_API_KEY, "geocode": f"{lon},{lat}",
              "format": "json", "kind": "street", "results": 1}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                fm = data["response"]["GeoObjectCollection"]["featureMember"]
                if not fm: return None
                props = fm[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]
                return {"name": props.get("text", ""), "kind": props.get("kind", "")}
    except Exception as e:
        print(f"[YANDEX] ❌ {e}")
    return None

# ========== МОДЕЛИ ==========
class LocateRequest(BaseModel):
    lat: float
    lon: float
    radius: int = 2000
    exclude_categories: list[int] = Field(default_factory=lambda: [6])

class LocateResponse(BaseModel):
    point: dict[str, float | None]
    road: dict[str, Any] | None = None
    side: dict[str, Any] | None = None

class ScreenshotSettings(BaseModel):
    delay: int = 3
    save_path: str = "screenshots"
    auto_enabled: bool = False
    local_boundary: bool = False

class SaveScreenshotRequest(BaseModel):
    image: str
    filename: str
    save_path: str = "screenshots"

class BookmarkCreate(BaseModel):
    name: str
    lat: float
    lon: float

# ========== НАСТРОЙКИ ==========
SETTINGS_FILE = PROJECT_DIR / "user_settings.json"
BOOKMARKS_FILE = PROJECT_DIR / "bookmarks.json"

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "delay": 3,
        "save_path": "screenshots",
        "auto_enabled": False,
        "local_boundary": False,
        "last_point": {"lat": 47.23, "lon": 39.72}
    }

def save_settings(data: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Ошибка сохранения настроек: {e}")

# ========== ЗАКЛАДКИ ==========
def load_bookmarks() -> list:
    if BOOKMARKS_FILE.exists():
        try:
            return json.loads(BOOKMARKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def save_bookmarks(data: list):
    try:
        BOOKMARKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Ошибка сохранения закладок: {e}")

# ========== ОЧИСТКА СТАРЫХ СКРИНШОТОВ ==========
def cleanup_old_screenshots(save_dir: Path, days: int = 7):
    if not save_dir.exists():
        return 0
    cutoff = time.time() - (days * 86400)
    deleted = 0
    try:
        for f in save_dir.glob("*.png"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
                print(f"🗑 Удалён старый скриншот: {f.name}")
        if deleted:
            print(f"✅ Очистка: удалено {deleted} файлов старше {days} дней")
    except Exception as e:
        print(f"⚠️ Ошибка очистки: {e}")
    return deleted

# ========== ЭНДПОИНТЫ ==========
@app.get("/api/health")
async def health():
    return {"status": "ok", "wfs": WFS_BASE, "ollama": OLLAMA_HOST, "model": OLLAMA_MODEL}

@app.get("/api/screenshot-settings")
async def get_screenshot_settings():
    return load_settings()

@app.post("/api/screenshot-settings")
async def set_screenshot_settings(payload: ScreenshotSettings):
    current = load_settings()
    current["delay"] = payload.delay
    current["save_path"] = payload.save_path
    current["auto_enabled"] = payload.auto_enabled
    current["local_boundary"] = payload.local_boundary
    save_settings(current)
    return {"status": "ok", "settings": current}

@app.post("/api/save-screenshot")
async def save_screenshot(req: SaveScreenshotRequest):
    try:
        image_data = req.image.split(',')[1] if req.image.startswith('data:image') else req.image
        image_bytes = base64.b64decode(image_data)
        screenshot_dir = PROJECT_DIR / req.save_path
        screenshot_dir.mkdir(exist_ok=True)
        
        cleanup_old_screenshots(screenshot_dir, days=7)
        
        base = req.filename.replace('.png', '')
        final = req.filename
        idx = 1
        while (screenshot_dir / final).exists():
            final = f"{base}_{idx:03d}.png"
            idx += 1
        
        (screenshot_dir / final).write_bytes(image_bytes)
        print(f"✅ Скриншот сохранён: {final}")
        return {"status": "ok", "filename": final}
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/exif")
async def api_exif(photo: UploadFile = File(...)):
    data = await photo.read()
    if not data:
        raise HTTPException(400, "Пустой файл")
    return extract_exif(data)

@app.post("/api/locate", response_model=LocateResponse)
async def api_locate(req: LocateRequest):
    lat, lon, radius = req.lat, req.lon, max(10, min(req.radius, 5000))
    
    settings = load_settings()
    settings["last_point"] = {"lat": lat, "lon": lon}
    save_settings(settings)
    
    bbox = _bbox_from_radius(lat, lon, max(radius, 150))
    async with httpx.AsyncClient(headers={"User-Agent": "albion/4.2.1"}) as client:
        results = await _wfs_getfeature(client, ROAD_LAYER, bbox_deg=bbox, max_features=50)
    
    all_candidates = []
    for f in results:
        geom = f.get("geometry", {})
        fx, fy = get_first_point(geom)
        if fx is None or fy is None: continue
        dist = round(_haversine(lon, lat, fx, fy), 1)
        if dist <= radius:
            props = f.get("properties", {}) or {}
            if props.get("category_gid") in req.exclude_categories:
                continue
            f["_distance_m"] = dist
            all_candidates.append(f)
    
    all_candidates.sort(key=lambda x: x["_distance_m"])
    
    skdf_candidates, fed_candidates = [], []
    for c in all_candidates:
        rn = (c.get("properties") or {}).get("road_name", "")
        if find_federal_road(rn):
            fed_candidates.append(c)
        elif find_road_id_by_name(rn) is not None:
            skdf_candidates.append(c)
    
    final = fed_candidates + skdf_candidates
    road_info = final[0] if final else (all_candidates[0] if all_candidates else None)
    
    yandex_data = None
    if road_info or not all_candidates:
        yandex_data = await get_yandex_road_name(lat, lon)
    
    side_info = None
    if road_info:
        side_info = _road_side_info(road_info, lon, lat)
        props = road_info.get("properties", {}) or {}
        sk, fk = float(props.get("start_km") or 0), float(props.get("finish_km") or 0)
        if "t" in side_info:
            if fk > sk:
                side_info["km_estimate"] = round(sk + (fk - sk) * side_info["t"], 3)
            elif sk > fk:
                side_info["km_estimate"] = round(fk + (sk - fk) * (1 - side_info["t"]), 3)
        
        rn = props.get("road_name", "")
        federal = find_federal_road(rn)
        names = federal if federal else format_road_names(props)
        cat = CATEGORY_MAP.get(props.get("category_gid"), "")
        coat = COATING_MAP.get(props.get("coating_gid"), "")
    else:
        names = {"skdf_name": yandex_data["name"] if yandex_data else "Дорога не найдена",
                 "full_name": yandex_data["name"] if yandex_data else "Дорога не найдена"}
        cat, coat = "", ""
    
    return LocateResponse(
        point={"lat": lat, "lon": lon, "bearing": None},
        road={"skdf_name": names["skdf_name"], "full_name": names["full_name"],
              "distance_m": (road_info or {}).get("_distance_m"),
              "category": cat, "coating": coat} if road_info else None,
        side=side_info
    )

# ========== ЗАКЛАДКИ: ЭНДПОИНТЫ ==========
@app.get("/api/bookmarks")
async def get_bookmarks():
    return load_bookmarks()

@app.post("/api/bookmarks")
async def add_bookmark(payload: BookmarkCreate):
    bookmarks = load_bookmarks()
    new_id = str(int(time.time() * 1000))
    new_bm = {
        "id": new_id,
        "name": payload.name.strip() or "Без имени",
        "lat": payload.lat,
        "lon": payload.lon,
        "created_at": time.strftime("%Y-%m-%d %H:%M")
    }
    bookmarks.insert(0, new_bm)
    bookmarks = bookmarks[:50]
    save_bookmarks(bookmarks)
    return {"status": "ok", "bookmark": new_bm}

@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: str):
    bookmarks = load_bookmarks()
    bookmarks = [b for b in bookmarks if b["id"] != bookmark_id]
    save_bookmarks(bookmarks)
    return {"status": "ok"}

# ========== СТАТИКА ==========
_STATIC_DIR = PROJECT_DIR.parent / "static"
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8019)