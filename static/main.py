"""
АЛЬБИОН: Локатор (Экспериментальная версия)
Версия: 4.0.0 (Добавлены: фильтр по СКДФ, федеральные трассы, Яндекс.Геокодер, скриншоты, сохранение настроек)
"""
from __future__ import annotations
import asyncio, io, math, os, re, difflib, base64, time, json
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

try:
    from roads import ROADS
    print(f"✅ База дорог загружена: {len(ROADS)} записей")
except ImportError:
    ROADS = {}
    print("️ Файл roads.py не найден!")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = 20.0
_llm_cache: dict[str, str | None] = {}

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
CATEGORY_MAP = {1: "Автомагистраль  ", 2: "I категория  ", 3: "II категория  ", 4: "III категория  ", 5: "IV категория  ", 6: "V категория  "}
COATING_MAP = {1: "Капитальный  ", 2: "Облегчённый  ", 3: "Переходный  ", 4: "Низший  "}

app = FastAPI(title="АЛЬБИОН: Локатор (Эксп.)", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# === НАСТРОЙКИ ===
SETTINGS_FILE = PROJECT_DIR / "settings.json"

def load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ Ошибка загрузки настроек: {e}")
    # Возвращаем дефолтные значения
    return {
        "screenshot": {
            "delay": 3,
            "save_path": str(PROJECT_DIR / "screenshots"),
            "auto_screenshot_enabled": False,
            "last_point": None
        }
    }

def save_settings(settings: dict):
    try:
        SETTINGS_FILE.parent.mkdir(exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения настроек: {e}")

# === СТРУКТУРА НАСТРОЕК ===
class ScreenshotSettings(BaseModel):
    delay: int = 3
    save_path: str = ""
    auto_screenshot_enabled: bool = False

class ScreenshotRequest(BaseModel):
    image: str
    filename: str | None = None
    save_path: str | None = None

class SettingsRequest(BaseModel):
    screenshot: ScreenshotSettings

class SettingsResponse(BaseModel):
    status: str
    settings: dict

@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings():
    settings = load_settings()
    return SettingsResponse(status="ok", settings=settings)

@app.post("/api/settings", response_model=SettingsResponse)
async def post_settings(req: SettingsRequest):
    current = load_settings()
    current["screenshot"]["delay"] = req.screenshot.delay
    if req.screenshot.save_path:
        current["screenshot"]["save_path"] = req.screenshot.save_path
    current["screenshot"]["auto_screenshot_enabled"] = req.screenshot.auto_screenshot_enabled
    save_settings(current)
    return SettingsResponse(status="ok", settings=current)

def _dms_to_dd(dms: tuple, ref: bytes) -> float:
    d, m, s = (float(Fraction(x[0], x[1])) for x in dms)
    return -(d + m / 60 + s / 3600) if ref in (b"S", b"W") else (d + m / 60 + s / 3600)

def extract_exif(data: bytes) -> dict[str, Any]:
    try:
        img = Image.open(io.BytesIO(data))
        ex = piexif.load(img.info.get("exif", b""))
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать EXIF. ({e}) ")
    gps = ex.get("GPS", {})
    if piexif.GPSIFD.GPSLatitude not in gps:
        raise HTTPException(400, "В EXIF нет GPS-координат. ")
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
    return (math.degrees(math.atan2(math.sin(dl)*math.cos(p2), math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl))) + 360) % 360

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
    best = min((_project_on_line(line, lon, lat) for line in candidates), key=lambda x: x["perpendicular_m"], default={"error": "no line"})
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

async def _wfs_getfeature(client, type_name, bbox_deg=None, max_features=50):
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": type_name, "outputFormat": "application/json", "srsname": "EPSG:4326", "maxFeatures": str(max_features)}
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
    if not coords: return None, None
    while isinstance(coords[0], (list, tuple)): coords = coords[0]
    try: return float(coords[0]), float(coords[1])
    except (ValueError, TypeError, IndexError): return None, None

def normalize_text(text: str) -> str:
    if not text: return ""
    text = text.lower().replace('"', '').replace("'", "").replace('“', '').replace('”', '').replace('«', '').replace('»', '').replace('–', '-').replace('—', '-')
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', text)).strip()

def find_road_id_by_name(road_name_from_output: str) -> str | None:
    if not road_name_from_output: return None
    norm_input = normalize_text(road_name_from_output)
    for road_id, name_in_db in ROADS.items():
        norm_db = normalize_text(name_in_db)
        if norm_input in norm_db or norm_db in norm_input: return road_id
    best_match, best_score = None, 0.0
    for road_id, name_in_db in ROADS.items():
        norm_db = normalize_text(name_in_db)
        score = difflib.SequenceMatcher(None, norm_input, norm_db).ratio()
        if score > best_score: best_score, best_match = score, road_id
    if best_score > 0.65:
        print(f"[DEBUG] ✅ difflib match: {best_match} (score: {best_score:.2f})")
        return best_match
    return None

def find_road_id_with_llm(road_name_from_skdf: str) -> str | None:
    if not road_name_from_skdf: return None
    cache_key = road_name_from_skdf.lower().strip()
    if cache_key in _llm_cache: return _llm_cache[cache_key]
    roads_list = "\n".join([f"{code}: {name}" for code, name in ROADS.items()])
    prompt = f"""Найди код дороги из справочника по названию. НАЗВАНИЕ: "{road_name_from_skdf}"\nСПРАВОЧНИК:\n{roads_list}\nПРАВИЛА: Сопоставляй по смыслу, игнорируй "автомобильная дорога", кавычки, опечатки. Ответь ТОЛЬКО кодом (например: 60К-11) или НЕ НАЙДЕНО"""
    try:
        response = httpx.post(f"{OLLAMA_HOST}/api/generate", json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 20}}, timeout=OLLAMA_TIMEOUT)
        response.raise_for_status()
        result = response.json().get("response", "").strip()
        match = re.search(r'(\d{2}[АКМРВНAКМРНB]-\d+(?:\.\d+)?)', result, re.IGNORECASE)
        if match:
            road_id = match.group(1).upper().replace('A', 'А').replace('K', 'К').replace('M', 'М').replace('P', 'Р').replace('H', 'Н').replace('B', 'В')
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
        match = re.search(r'(\d{2}[АКМРВНAКМРНB]-\d+(?:\.\d+)?)', re.sub(r'\s+', '', value_of_the_road).upper())
        if match: road_id = match.group(1)
    if not road_id and road_name_from_skdf: road_id = find_road_id_by_name(road_name_from_skdf)
    if not road_id and road_name_from_skdf: road_id = find_road_id_with_llm(road_name_from_skdf)
    full_name = f"{road_id}, {ROADS[road_id]}" if road_id and road_id in ROADS else (road_name_from_skdf or "Дорога без названия")
    return {"skdf_name": road_name_from_skdf or "Нет данных", "full_name": full_name}

async def get_yandex_road_name(lat: float, lon: float) -> dict | None:
    if not YANDEX_API_KEY or YANDEX_API_KEY.strip() == "": return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://geocode-maps.yandex.ru/1.x/", params={"apikey": YANDEX_API_KEY, "geocode": f"{lon},{lat}", "format": "json", "kind": "street", "results": 1}, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                feature_member = data["response"]["GeoObjectCollection"]["featureMember"]
                if not feature_member: return None
                props = feature_member[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]
                return {"name": props.get("text", ""), "kind": props.get("kind", "")}
    except Exception as e:
        print(f"[YANDEX] ❌ {e}")
    return None

class LocateRequest(BaseModel):
    lat: float
    lon: float
    radius: int = 2000
    exclude_categories: list[int] = Field(default_factory=lambda: [6])

class LocateResponse(BaseModel):
    point: dict[str, float | None]
    road: dict[str, Any] | None = None
    side: dict[str, Any] | None = None

@app.get("/api/health")
async def health(): return {"status": "ok", "wfs": WFS_BASE, "ollama": OLLAMA_HOST, "model": OLLAMA_MODEL}

@app.post("/api/exif")
async def api_exif(photo: UploadFile = File(...)):
    data = await photo.read()
    if not data: raise HTTPException(400, "Пустой файл")
    return extract_exif(data)

@app.post("/api/locate", response_model=LocateResponse)
async def api_locate(req: LocateRequest):
    lat, lon, radius = req.lat, req.lon, max(10, min(req.radius, 5000))
    bbox = _bbox_from_radius(lat, lon, max(radius, 150))
    async with httpx.AsyncClient(headers={"User-Agent": "albion/4.0"}) as client:
        results = await _wfs_getfeature(client, ROAD_LAYER, bbox_deg=bbox, max_features=50)
    
    all_candidates = []
    for f in results:
        geom = f.get("geometry", {})
        fx, fy = get_first_point(geom)
        if fx is None or fy is None: continue
        dist = round(_haversine(lon, lat, fx, fy), 1)
        if dist <= radius:
            props = f.get("properties", {}) or {}
            if props.get("category_gid") in req.exclude_categories: continue
            f["_distance_m"] = dist
            all_candidates.append(f)
    
    all_candidates.sort(key=lambda x: x["_distance_m"])
    skdf_candidates, fed_candidates = [], []
    
    for candidate in all_candidates:
        road_name = candidate.get("properties", {}).get("road_name", "")
        if find_federal_road(road_name):
            fed_candidates.append(candidate)
            continue
        road_id = find_road_id_by_name(road_name)
        if road_id is not None: skdf_candidates.append(candidate)
    
    final_candidates = fed_candidates + skdf_candidates
    road_info = final_candidates[0] if final_candidates else (all_candidates[0] if all_candidates else None)
    
    yandex_data = None
    if road_info or not all_candidates:
        yandex_data = await get_yandex_road_name(lat, lon)
    
    side_info = None
    if road_info:
        side_info = _road_side_info(road_info, lon, lat)
        props = road_info.get("properties", {}) or {}
        start_km, finish_km = float(props.get("start_km") or 0), float(props.get("finish_km") or 0)
        if finish_km > start_km and side_info and "t" in side_info:
            side_info.update({"km_estimate": round(start_km + (finish_km - start_km) * side_info["t"], 3), "km_start": start_km, "km_finish": finish_km})
        elif start_km > finish_km and side_info and "t" in side_info:
            side_info.update({"km_estimate": round(finish_km + (start_km - finish_km) * (1 - side_info["t"]), 3), "km_start": finish_km, "km_finish": start_km})
        
        federal = find_federal_road(props.get("road_name", ""))
        names = federal if federal else format_road_names(props)
        category_name = CATEGORY_MAP.get(props.get("category_gid"), "")
        coating_name = COATING_MAP.get(props.get("coating_gid"), "")
    else:
        names = {"skdf_name": yandex_data["name"], "full_name": yandex_data["name"]} if yandex_data else {"skdf_name": "Дорога не найдена", "full_name": "Дорога не найдена"}
        category_name, coating_name = "", ""
    
    return LocateResponse(
        point={"lat": lat, "lon": lon, "bearing": None},
        road={"skdf_name": names["skdf_name"], "full_name": names["full_name"], "distance_m": (road_info or {}).get("_distance_m"), "category": category_name, "coating": coating_name} if (road_info or yandex_data) else None,
        side=side_info
    )

# === СОХРАНЕНИЕ СКРИНШОТОВ ===
@app.post("/api/save-screenshot")
async def save_screenshot(payload: ScreenshotRequest):
    image_data = payload.image
    filename = payload.filename or f"albion_{int(time.time())}.png"
    
    if not image_data:
        raise HTTPException(400, "Нет данных изображения")
    
    if image_data.startswith("data:image/png;base64,"):
        image_data = image_data.split(",")[1]
    
    try:
        image_bytes = base64.b64decode(image_data)
        save_dir = Path(payload.save_path or load_settings()["screenshot"]["save_path"])
        save_dir.mkdir(exist_ok=True)
        file_path = save_dir / filename
        
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        return {"status": "ok", "path": str(file_path), "filename": filename}
    except Exception as e:
        raise HTTPException(500, f"Ошибка сохранения: {e}")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

@app.get("/")
async def root(): return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8017)