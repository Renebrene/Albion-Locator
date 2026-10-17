"""
АЛЬБИОН: Локатор-Навигатор (Автономная экспериментальная версия)
Версия: 4.2.1 — порт 8019, стабильная версия
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import io
import json
import math
import os
import re
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import piexif
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel, Field, FiniteFloat, field_validator
from batch import router as batch_router

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_REQUEST_BODY_BYTES = 110 * 1024 * 1024

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

_ROAD_TRANSLATION = str.maketrans({"A": "А", "K": "К", "M": "М", "P": "Р", "H": "Н", "B": "В"})

def canonical_road_code(value: str) -> str | None:
    if not value:
        return None
    normalized = value.upper().translate(_ROAD_TRANSLATION)
    match = re.search(r"(?<![\w])([0-9]{1,3}[А-Я]-[0-9]+(?:\.[0-9]+)?|[А-Я]-[0-9]+(?:\.[0-9]+)?)(?![\w])", normalized)
    return match.group(1) if match else None

def find_federal_road(name: str) -> dict | None:
    code = canonical_road_code(name)
    if not code:
        return None
    for road_code, data in FEDERAL_ROADS.items():
        if canonical_road_code(road_code) == code:
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
app.include_router(batch_router)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    safe_errors = [{"loc": error.get("loc", []), "msg": error.get("msg", "Invalid value"),
                    "type": error.get("type", "value_error")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": safe_errors})

class RequestBodyLimitMiddleware:
    def __init__(self, app, max_body_size: int):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_size:
                    await self._send_error(send, 413, "Request body too large")
                    return
            except ValueError:
                await self._send_error(send, 400, "Invalid Content-Length")
                return
        origin = headers.get(b"origin")
        method = scope.get("method", "GET")
        if method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            allowed_origins = {b"http://127.0.0.1:8019", b"http://localhost:8019"}
            if origin.rstrip(b"/") not in allowed_origins:
                await self._send_error(send, 403, "Cross-origin request denied")
                return
        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    raise RequestBodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                await self._send_error(send, 413, "Request body too large")

    async def _send_error(self, send, status_code: int, detail: str):
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({"type": "http.response.start", "status": status_code,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})

class RequestBodyTooLarge(Exception):
    pass

app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_REQUEST_BODY_BYTES)

# ========== ГЕОМЕТРИЯ ==========
def _dms_to_dd(dms: tuple, ref: bytes) -> float:
    if ref not in (b"N", b"S", b"E", b"W") or len(dms) != 3:
        raise ValueError("Некорректные GPS-координаты в EXIF")
    degrees, minutes, seconds = (float(Fraction(value[0], value[1])) for value in dms)
    max_degrees = 90 if ref in (b"N", b"S") else 180
    if (not all(math.isfinite(value) for value in (degrees, minutes, seconds))
            or degrees < 0 or minutes < 0 or minutes >= 60
            or seconds < 0 or seconds >= 60 or degrees > max_degrees
            or (degrees == max_degrees and (minutes != 0 or seconds != 0))):
        raise ValueError("Некорректный диапазон GPS в EXIF")
    result = degrees + minutes / 60 + seconds / 3600
    return -result if ref in (b"S", b"W") else result

def _valid_coordinates(lat: float, lon: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180

def extract_exif(data: bytes) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in {"JPEG", "TIFF"}:
                raise ValueError("Поддерживаются только JPEG и TIFF")
            if img.width * img.height > 40_000_000:
                raise ValueError("Image dimensions exceed the processing limit")
            ex = piexif.load(img.info.get("exif", b""))
        gps = ex.get("GPS", {})
        lat = _dms_to_dd(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef])
        lon = _dms_to_dd(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
    except Exception:
        raise HTTPException(400, "Не удалось извлечь GPS-координаты из JPEG/TIFF EXIF.")
    if not _valid_coordinates(lat, lon):
        raise HTTPException(400, "GPS-координаты выходят за допустимый диапазон.")
    return {"lat": lat, "lon": lon}

def _bbox_from_radius(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / 111100.0
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-6)
    dlon = min(180.0, radius_m / (111100.0 * cos_lat))
    return (max(-180.0, lon - dlon), max(-90.0, lat - dlat),
            min(180.0, lon + dlon), min(90.0, lat + dlat))

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
    try:
        points = [(float(c[0]), float(c[1])) for c in coords]
    except (TypeError, ValueError, IndexError):
        return None
    if len(points) < 2 or any(not _valid_coordinates(y, x) for x, y in points):
        return None
    projected = [_local_m(x, y, lon, lat) for x, y in points]
    lengths = [math.hypot(projected[i + 1][0] - projected[i][0], projected[i + 1][1] - projected[i][1])
               for i in range(len(projected) - 1)]
    best = None
    distance_before = 0.0
    for i, length in enumerate(lengths):
        x1, y1 = projected[i]
        x2, y2 = projected[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        t = max(0.0, min(1.0, (-x1 * dx - y1 * dy) / length_sq)) if length_sq else 0.0
        cx, cy = x1 + t * dx, y1 + t * dy
        dist_sq = cx * cx + cy * cy
        if best is None or dist_sq < best["dist_sq"]:
            best = {"dist_sq": dist_sq, "seg": i, "t": t, "cx": cx, "cy": cy,
                    "dx": dx, "dy": dy, "distance_before": distance_before, "length": length}
        distance_before += length
    if best is None:
        return None
    x1, y1 = points[best["seg"]]
    x2, y2 = points[best["seg"] + 1]
    cross = best["dx"] * (-best["cy"]) - best["dy"] * (-best["cx"])
    total_length = sum(lengths)
    return {
        "projected_lon": lon + math.degrees(best["cx"] / (6371000.0 * max(abs(math.cos(math.radians(lat))), 1e-6))),
        "projected_lat": lat + math.degrees(best["cy"] / 6371000.0),
        "perpendicular_m": round(math.sqrt(best["dist_sq"]), 1),
        "side_raw": 1 if cross > 0 else (-1 if cross < 0 else 0),
        "tangent_bearing": round(_bearing(x1, y1, x2, y2), 1),
        "t": (best["distance_before"] + best["length"] * best["t"]) / total_length if total_length else 0.0,
    }

def _road_side_info(road_feature, lon, lat):
    geom = road_feature.get("geometry", {}) or {}
    kind, coordinates = geom.get("type"), geom.get("coordinates") or []
    candidates = [coordinates] if kind == "LineString" else coordinates if kind == "MultiLineString" else []
    projected = [item for item in (_project_on_line(line, lon, lat) for line in candidates) if item]
    if not projected:
        return {"error": "no line"}
    best = min(projected, key=lambda item: item["perpendicular_m"])
    props = road_feature.get("properties", {}) or {}
    try:
        start_km = float(props.get("start_km") or 0)
        finish_km = float(props.get("finish_km") or 0)
    except (TypeError, ValueError):
        start_km = finish_km = 0.0
    best["side"] = -best["side_raw"] if start_km < finish_km else best["side_raw"]
    del best["side_raw"]
    best["distance_m"] = best["perpendicular_m"]
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
        features = r.json().get("features", [])
        if not isinstance(features, list):
            raise ValueError("Invalid WFS feature collection")
        return features
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Сервис дорожных данных временно недоступен") from exc

def get_first_point(geometry: dict) -> tuple[float | None, float | None]:
    coords = (geometry or {}).get("coordinates")
    try:
        while isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (list, tuple)):
            coords = coords[0]
        lon, lat = float(coords[0]), float(coords[1])
        return (lon, lat) if _valid_coordinates(lat, lon) else (None, None)
    except (ValueError, TypeError, IndexError):
        return None, None

def _project_feature(geometry: dict, lon: float, lat: float) -> dict | None:
    geometry = geometry or {}
    kind, coordinates = geometry.get("type"), geometry.get("coordinates") or []
    lines = [coordinates] if kind == "LineString" else coordinates if kind == "MultiLineString" else []
    projected = [result for result in (_project_on_line(line, lon, lat) for line in lines) if result]
    return min(projected, key=lambda item: item["perpendicular_m"], default=None)

# ========== ПОИСК ДОРОГ ==========
def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.upper().translate(_ROAD_TRANSLATION).lower()
    text = text.replace('"', '').replace("'", "").replace('«', '').replace('»', '')
    text = text.replace('–', '-').replace('—', '-')
    text = re.sub(r'[^\w\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def find_road_id_by_name(road_name: str) -> str | None:
    norm_input = normalize_text(road_name)
    if not norm_input:
        return None
    input_code = canonical_road_code(road_name)
    if input_code in ROADS:
        return input_code
    best_match, best_score = None, 0.0
    for road_id, name_in_db in ROADS.items():
        norm_db = normalize_text(name_in_db)
        if norm_db and (norm_input in norm_db or norm_db in norm_input):
            return road_id
        score = difflib.SequenceMatcher(None, norm_input, norm_db).ratio() if norm_db else 0.0
        if score > best_score:
            best_score, best_match = score, road_id
    return best_match if best_score > 0.65 else None

async def find_road_id_with_llm(road_name: str) -> str | None:
    if not road_name:
        return None
    cache_key = road_name.lower().strip()
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]
    roads_list = "\n".join(f"{code}: {name}" for code, name in ROADS.items())
    prompt = f"""Найди код дороги из справочника по названию.
НАЗВАНИЕ: "{road_name[:300]}"
СПРАВОЧНИК:
{roads_list}
ПРАВИЛА:
- Сопоставляй по смыслу
- Игнорируй слова "автомобильная дорога", кавычки, опечатки
- Ответь ТОЛЬКО кодом (например: 60К-11) или НЕ НАЙДЕНО"""
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(f"{OLLAMA_HOST}/api/generate", json={
                "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.0, "num_predict": 20}})
        response.raise_for_status()
        code = canonical_road_code(response.json().get("response", ""))
        result = code if code in ROADS else None
    except (httpx.HTTPError, ValueError, TypeError):
        result = None
    if len(_llm_cache) >= 512:
        _llm_cache.pop(next(iter(_llm_cache)))
    _llm_cache[cache_key] = result
    return result

async def format_road_names(props: dict) -> dict[str, str]:
    road_name_from_skdf = (props.get("road_name") or "").strip()
    value_of_the_road = (props.get("value_of_the_road") or "").strip()
    road_id = canonical_road_code(value_of_the_road) or canonical_road_code(road_name_from_skdf)
    if road_id not in ROADS:
        road_id = find_road_id_by_name(road_name_from_skdf)
    if not road_id and road_name_from_skdf:
        road_id = await find_road_id_with_llm(road_name_from_skdf)
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
    lat: FiniteFloat = Field(ge=-90, le=90)
    lon: FiniteFloat = Field(ge=-180, le=180)
    radius: int = Field(default=2000, ge=10, le=5000)
    exclude_categories: list[int] = Field(default_factory=lambda: [6], max_length=10)

    @field_validator("exclude_categories")
    @classmethod
    def unique_categories(cls, values: list[int]) -> list[int]:
        if any(value not in CATEGORY_MAP for value in values):
            raise ValueError("Unknown road category")
        return list(dict.fromkeys(values))

class LocateResponse(BaseModel):
    point: dict[str, float | None]
    road: dict[str, Any] | None = None
    side: dict[str, Any] | None = None

class ScreenshotSettings(BaseModel):
    delay: int = Field(default=3, ge=0, le=60)
    save_path: str = "screenshots"
    auto_enabled: bool = False
    local_boundary: bool = True

class SaveScreenshotRequest(BaseModel):
    image: str = Field(max_length=8_000_000)
    filename: str = Field(min_length=1, max_length=120)

class BookmarkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    lat: FiniteFloat = Field(ge=-90, le=90)
    lon: FiniteFloat = Field(ge=-180, le=180)

# ========== НАСТРОЙКИ ==========
SETTINGS_FILE = PROJECT_DIR / "user_settings.json"
BOOKMARKS_FILE = PROJECT_DIR / "bookmarks.json"

def load_settings() -> dict:
    defaults = {"delay": 3, "save_path": "screenshots", "auto_enabled": False,
                "local_boundary": True, "last_point": {"lat": 47.23, "lon": 39.72}}
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, ValueError):
            pass
    defaults["save_path"] = "screenshots"
    return defaults

_SETTINGS_LOCK = asyncio.Lock()
_BOOKMARKS_LOCK = asyncio.Lock()

def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)

def save_settings(data: dict):
    _write_json_atomic(SETTINGS_FILE, data)

# ========== ЗАКЛАДКИ ==========
def load_bookmarks() -> list:
    if BOOKMARKS_FILE.exists():
        try:
            return json.loads(BOOKMARKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def save_bookmarks(data: list):
    _write_json_atomic(BOOKMARKS_FILE, data)

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
    return {"status": "ok"}

@app.get("/api/screenshot-settings")
async def get_screenshot_settings():
    return load_settings()

@app.post("/api/screenshot-settings")
async def set_screenshot_settings(payload: ScreenshotSettings):
    async with _SETTINGS_LOCK:
        current = load_settings()
        current.update({"delay": payload.delay, "save_path": "screenshots",
                        "auto_enabled": payload.auto_enabled, "local_boundary": payload.local_boundary})
        save_settings(current)
    return {"status": "ok", "settings": current}

def _decode_screenshot(value: str) -> bytes:
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or header != "data:image/png;base64":
            raise HTTPException(400, "Ожидается PNG data URL")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(400, "Некорректный base64")
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(415, "Переданные данные не являются PNG")
    if len(image_bytes) > 6_000_000:
        raise HTTPException(413, "Скриншот превышает 6 МБ")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format != "PNG":
                raise HTTPException(415, "Переданные данные не являются PNG")
            if image.width * image.height > 40_000_000:
                raise HTTPException(413, "Размеры изображения превышают допустимый лимит")
            image.verify()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Некорректное PNG-изображение")
    return image_bytes

@app.post("/api/save-screenshot")
async def save_screenshot(req: SaveScreenshotRequest):
    if not re.fullmatch(r"[\w-]{1,100}\.png", req.filename, re.IGNORECASE):
        raise HTTPException(422, "Некорректное имя PNG-файла")
    image_bytes = _decode_screenshot(req.image)
    screenshot_dir = PROJECT_DIR / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_screenshots(screenshot_dir, days=7)
    base = req.filename[:-4]
    for idx in range(1000):
        final = req.filename if idx == 0 else f"{base}_{idx:03d}.png"
        try:
            with (screenshot_dir / final).open("xb") as image_file:
                image_file.write(image_bytes)
            return {"status": "ok", "filename": final}
        except FileExistsError:
            continue
    raise HTTPException(409, "Не удалось подобрать свободное имя файла")

async def _read_upload_limited(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Файл превышает лимит {limit // (1024 * 1024)} МБ")
    return data

@app.post("/api/exif")
async def api_exif(photo: UploadFile = File(...)):
    try:
        data = await _read_upload_limited(photo, 12 * 1024 * 1024)
        if not data:
            raise HTTPException(400, "Пустой файл")
        return extract_exif(data)
    finally:
        await photo.close()

@app.post("/api/locate", response_model=LocateResponse)
async def api_locate(req: LocateRequest):
    lat, lon, radius = req.lat, req.lon, req.radius

    async with _SETTINGS_LOCK:
        settings = load_settings()
        settings["last_point"] = {"lat": lat, "lon": lon}
        save_settings(settings)

    bbox = _bbox_from_radius(lat, lon, max(radius, 150))
    async with httpx.AsyncClient(headers={"User-Agent": "albion/4.2.1"}) as client:
        results = await _wfs_getfeature(client, ROAD_LAYER, bbox_deg=bbox, max_features=50)

    all_candidates = []
    for feature in results:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        projection = _project_feature(geometry, lon, lat)
        if projection is None or projection["perpendicular_m"] > radius:
            continue
        props = feature.get("properties", {}) or {}
        try:
            category_id = int(props.get("category_gid")) if props.get("category_gid") is not None else None
        except (TypeError, ValueError):
            category_id = None
        if category_id in req.exclude_categories:
            continue
        feature["_projection"] = projection
        feature["_distance_m"] = projection["perpendicular_m"]
        all_candidates.append(feature)

    all_candidates.sort(key=lambda item: item["_distance_m"])
    road_info = all_candidates[0] if all_candidates else None
    yandex_data = await get_yandex_road_name(lat, lon) if not road_info else None
    
    side_info = None
    if road_info:
        side_info = road_info.get("_projection") or {"error": "no line"}
        props = road_info.get("properties", {}) or {}
        try:
            sk, fk = float(props.get("start_km") or 0), float(props.get("finish_km") or 0)
        except (TypeError, ValueError):
            sk = fk = 0.0
        if "side_raw" in side_info:
            side_info["side"] = -side_info["side_raw"] if sk < fk else side_info["side_raw"]
            del side_info["side_raw"]
        if "t" in side_info and sk != fk:
            side_info["km_estimate"] = round(sk + (fk - sk) * side_info["t"], 3)

        rn = props.get("road_name", "")
        federal = find_federal_road(rn)
        names = federal if federal else await format_road_names(props)
        try:
            category_id = int(props.get("category_gid")) if props.get("category_gid") is not None else None
            coating_id = int(props.get("coating_gid")) if props.get("coating_gid") is not None else None
        except (TypeError, ValueError):
            category_id = coating_id = None
        cat = CATEGORY_MAP.get(category_id, "")
        coat = COATING_MAP.get(coating_id, "")
    else:
        names = {"skdf_name": yandex_data["name"] if yandex_data else "Дорога не найдена",
                 "full_name": yandex_data["name"] if yandex_data else "Дорога не найдена"}
        cat, coat = "", ""
    
    return LocateResponse(
        point={"lat": lat, "lon": lon, "bearing": None},
        road={"skdf_name": names["skdf_name"], "full_name": names["full_name"],
              "distance_m": (road_info or {}).get("_distance_m"),
              "category": cat, "coating": coat,
              "start_km": sk if road_info else None,
              "finish_km": fk if road_info else None,
              "geometry": road_info.get("geometry")} if road_info else None,
        side=side_info
    )

# ========== ЗАКЛАДКИ: ЭНДПОИНТЫ ==========
@app.get("/api/bookmarks")
async def get_bookmarks():
    return load_bookmarks()

@app.post("/api/bookmarks")
async def add_bookmark(payload: BookmarkCreate):
    new_bm = {"id": uuid.uuid4().hex, "name": payload.name.strip() or "Без имени",
              "lat": payload.lat, "lon": payload.lon,
              "created_at": time.strftime("%Y-%m-%d %H:%M")}
    async with _BOOKMARKS_LOCK:
        bookmarks = load_bookmarks()
        bookmarks.insert(0, new_bm)
        save_bookmarks(bookmarks[:50])
    return {"status": "ok", "bookmark": new_bm}

@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: str):
    async with _BOOKMARKS_LOCK:
        bookmarks = load_bookmarks()
        save_bookmarks([bookmark for bookmark in bookmarks if bookmark.get("id") != bookmark_id])
    return {"status": "ok"}

# ========== СТАТИКА ==========
_STATIC_DIR = PROJECT_DIR.parent / "static"

@app.get("/static/rostov_boundary.geojson")
async def boundary_geojson():
    return FileResponse(_STATIC_DIR / "rostov_boundary.geojson", media_type="application/geo+json")

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(_STATIC_DIR / "index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8019)
